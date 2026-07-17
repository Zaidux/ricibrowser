"""CAPTCHA detection, Cloudflare auto-resolve, and solver hook framework.

Detection: identifies which CAPTCHA/anti-bot system is blocking navigation
(Cloudflare JS challenge, Cloudflare Turnstile, reCAPTCHA v2/v3, hCaptcha).

Auto-resolve: for Cloudflare JS challenges ("Just a moment..."), waits for
the challenge to auto-resolve in real Chrome (the JavaScript runs natively
and often passes within 5-10 seconds). Captures the ``cf_clearance`` cookie
for persistence.

Solver hooks: for CAPTCHAs that can't be auto-resolved (Turnstile,
reCAPTCHA, hCaptcha), provides a hook interface for external solver services.
The operator registers a ``CaptchaSolver`` implementation and ricibrowser
calls it when a CAPTCHA is detected.

Usage::

    from ricibrowser.captcha import CaptCHAHandler, CloudflareAutoSolver

    handler = CaptchaHandler(auto_solver=CloudflareAutoSolver())
    result = await handler.detect_and_solve(session)
    if result.solved:
        print("Challenge resolved!")  # cf_clearance now in cookie jar
    elif result.captcha_type:
        print(f"Unsupported: {result.captcha_type} — needs external solver")
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ricibrowser.cdp_client import CDPClient, CDPError

logger = logging.getLogger(__name__)


class CaptchaType(Enum):
    """Detected CAPTCHA / anti-bot challenge type."""

    NONE = "none"
    """No CAPTCHA detected."""

    CLOUDFLARE_JS = "cloudflare_js"
    """Cloudflare JS challenge ("Just a moment..."). Often auto-resolves."""

    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    """Cloudflare Turnstile widget. Needs external solver."""

    RECAPTCHA_V2 = "recaptcha_v2"
    """Google reCAPTCHA v2 (checkbox or invisible). Needs external solver."""

    RECAPTCHA_V3 = "recaptcha_v3"
    """Google reCAPTCHA v3 (score-based, invisible). Needs external solver."""

    HCAPTCHA = "hcaptcha"
    """hCaptcha widget. Needs external solver."""

    GENERIC = "generic"
    """Unknown anti-bot challenge."""


@dataclass
class CaptchaResult:
    """Result of a CAPTCHA detection / solve attempt."""

    captcha_type: CaptchaType
    solved: bool
    cookie_name: str | None = None
    """The cookie that proves the challenge was solved (e.g. cf_clearance)."""
    solver_used: str | None = None
    """Which solver was used (e.g. 'cloudflare_auto', 'external')."""
    error: str | None = None
    duration_seconds: float = 0.0


@runtime_checkable
class CaptchaSolver(Protocol):
    """Protocol for external CAPTCHA solver implementations.

    Operators implement this to integrate third-party solver services
    (2captcha, anti-captcha, capsolver, etc.) for CAPTCHAs that can't
    be auto-resolved.

    Example implementation::

        class TwoCaptchaSolver:
            async def solve(self, captcha_type, site_key, url):
                # Call the 2captcha API
                token = await call_service(site_key, url)
                return CaptchaToken(token=token)

        handler = CaptCHAHandler(solver=TwoCaptchaSolver())
    """

    async def solve(
        self,
        captcha_type: CaptchaType,
        site_key: str | None,
        url: str,
    ) -> str | None:
        """Solve a CAPTCHA and return the token.

        Args:
            captcha_type: The type of CAPTCHA detected.
            site_key: The site key (for reCAPTCHA/hCaptcha/Turnstile).
            url: The page URL where the CAPTCHA appears.

        Returns:
            The solver token, or None if the solver can't handle this type.
        """
        ...


@dataclass
class CaptchaToken:
    """A solved CAPTCHA token."""
    token: str
    captcha_type: CaptchaType


# ── Detection ──────────────────────────────────────────────────────────


async def detect_captcha(session) -> CaptchaType:
    """Detect what type of CAPTCHA or anti-bot challenge is on the current page.

    Checks the DOM for known CAPTCHA widget signatures and the page text
    for challenge messages.
    """
    # Check for Cloudflare JS challenge ("Just a moment...")
    title = await session.evaluate("document.title") or ""
    body_text = await session.evaluate(
        "document.body ? document.body.innerText.substring(0, 500) : ''"
    ) or ""

    title_lower = title.lower()
    text_lower = (body_text or "").lower()

    if any(p in title_lower or p in text_lower for p in(
        "just a moment", "checking your browser", "attention required",
        "cf-challenge", "challenge-platform",
    )):
        return CaptchaType.CLOUDFLARE_JS

    # Check for Cloudflare Turnstile widget
    turnstile = await session.evaluate_bool(
        "document.querySelector('.cf-turnstile, [data-sitekey]') !== null"
    )
    if turnstile:
        # Verify it's actually a Turnstile widget (not reCAPTCHA)
        is_turnstile = await session.evaluate_bool(
            "document.querySelector('script[src*=\"challenges.cloudflare.com/turnstile\"]') !== null"
        )
        if is_turnstile:
            return CaptchaType.CLOUDFLARE_TURNSTILE

    # Check for reCAPTCHA
    recaptcha = await session.evaluate_bool(
        "document.querySelector('.g-recaptcha, [data-sitekey], "
        "iframe[src*=\"recaptcha\"]') !== null"
    )
    if recaptcha:
        # Check if it's v2 (visible checkbox) or v3 (invisible)
        visible = await session.evaluate_bool(
            "document.querySelector('.g-recaptcha') && "
            "getComputedStyle(document.querySelector('.g-recaptcha')).display !== 'none'"
        )
        if visible:
            return CaptchaType.RECAPTCHA_V2
        return CaptchaType.RECAPTCHA_V3

    # Check for hCaptcha
    hcaptcha = await session.evaluate_bool(
        "document.querySelector('.h-captcha, iframe[src*=\"hcaptcha\"]') !== null"
    )
    if hcaptcha:
        return CaptchaType.HCAPTCHA

    # Generic anti-bot check
    if any(p in text_lower for p in(
        "enable javascript and cookies", "please complete the security check",
        "verify you are human", "are you a robot",
    )):
        return CaptchaType.GENERIC

    return CaptchaType.NONE


# ── Cloudflare Auto-Solver ────────────────────────────────────────────


class CloudflareAutoSolver:
    """Auto-resolves Cloudflare JS challenges by waiting for native JS execution.

    Cloudflare's "Just a moment..." challenge runs JavaScript that computes
    a token and auto-submits a form. In a real Chrome browser with real V8
    engine (which ricibrowser uses), this JavaScript executes natively and
    the challenge resolves automatically — usually within 5-10 seconds.

    This solver:
      1. Waits for the challenge page to load
      2. Polls every 1s for the challenge to clear (body text changes,
         challenge elements disappear, content loads)
      3. Checks for the ``cf_clearance`` cookie which proves the challenge passed
      4. Returns the result with the cookie name

    Limitations:
      - Only works with real Chrome (not Lightpanda — Lightpanda's V8 may
        not handle the obfuscated CF JS)
      - Doesn't work if the CF challenge requires a Turnstile click
      - If the IP address or User-Agent is too suspicious, CF may loop forever
    """

    def __init__(self, max_wait: float = 15.0, poll_interval: float = 1.0):
        self.max_wait = max_wait
        self.poll_interval = poll_interval

    async def solve(self, session) -> CaptchaResult:
        """Wait for the Cloudflare JS challenge to auto-resolve.

        Args:
            session: A ricibrowser Session on the challenge page.

        Returns:
            CaptchaResult with solved=True if the challenge cleared.
        """
        start = time.monotonic()

        for _ in range(int(self.max_wait / self.poll_interval)):
            elapsed = time.monotonic() - start

            # Check if the challenge has cleared
            body_text = await session.evaluate(
                "document.body ? document.body.innerText.substring(0, 200) : ''"
            )

            # If evaluate returned None (context not ready, CDP error),
            # treat as "unknown — keep waiting" rather than "cleared".
            if body_text is None:
                await asyncio.sleep(self.poll_interval)
                continue

            text_lower = body_text.lower()

            # Challenge cleared if the "Just a moment" / "Checking" text is gone
            # AND there is some actual body content (not empty).
            challenge_cleared = (
                bool(body_text.strip())
                and not any(p in text_lower for p in(
                    "just a moment", "checking your browser", "attention required",
                    "enable javascript and cookies",
                ))
            )

            if challenge_cleared:
                # Verify cf_clearance cookie was set
                cookies = await session.get_cookies()
                cf_clearance = [c for c in cookies if c.get("name") == "cf_clearance"]
                if cf_clearance:
                    return CaptchaResult(
                        captcha_type=CaptchaType.CLOUDFLARE_JS,
                        solved=True,
                        cookie_name="cf_clearance",
                        solver_used="cloudflare_auto",
                        duration_seconds=elapsed,
                    )
                # Challenge cleared but no cookie — might be a lighter check
                return CaptchaResult(
                    captcha_type=CaptchaType.CLOUDFLARE_JS,
                    solved=True,
                    cookie_name=None,
                    solver_used="cloudflare_auto",
                    duration_seconds=elapsed,
                )

            await asyncio.sleep(self.poll_interval)

        # Timed out
        return CaptchaResult(
            captcha_type=CaptchaType.CLOUDFLARE_JS,
            solved=False,
            error=f"Cloudflare challenge did not auto-resolve within {self.max_wait}s. "
                  "The IP may be flagged or a Turnstile click may be required.",
            duration_seconds=time.monotonic() - start,
        )


# ── Handler ───────────────────────────────────────────────────────────


class CaptchaHandler:
    """Detect and attempt to solve CAPTCHAs automatically.

    Priority:
      1. Cloudflare JS challenge → CloudflareAutoSolver (native JS execution)
      2. Turnstile / reCAPTCHA / hCaptcha → external solver (if registered)
      3. Generic → report and suggest manual intervention

    Usage::

        handler = CaptchaHandler(
            auto_solver=CloudflareAutoSolver(),
            external_solver=MySolver(),  # optional
        )
        result = await handler.detect_and_solve(session)
    """

    def __init__(
        self,
        auto_solver: CloudflareAutoSolver | None = None,
        external_solver: CaptchaSolver | None = None,
    ):
        self.auto_solver = auto_solver or CloudflareAutoSolver()
        self.external_solver = external_solver

    async def detect_and_solve(self, session) -> CaptchaResult:
        """Detect the CAPTCHA type and attempt resolution.

        Returns a CaptchaResult. If solved, the session is ready to continue.
        If not solved, the result contains the type and an error message.
        """
        start = time.monotonic()

        # ── Detect ──────────────────────────────────────────────────
        captcha_type = await detect_captcha(session)

        if captcha_type == CaptchaType.NONE:
            return CaptchaResult(
                captcha_type=CaptchaType.NONE,
                solved=True,
                duration_seconds=time.monotonic() - start,
            )

        logger.info("CAPTCHA detected: %s", captcha_type.value)

        # ── Cloudflare JS challenge → auto-solve ───────────────────
        if captcha_type == CaptchaType.CLOUDFLARE_JS:
            result = await self.auto_solver.solve(session)
            return result

        # ── External solver for reCAPTCHA/hCaptcha/Turnstile ───────
        if self.external_solver and captcha_type in(
            CaptchaType.RECAPTCHA_V2,
            CaptchaType.RECAPTCHA_V3,
            CaptchaType.HCAPTCHA,
            CaptchaType.CLOUDFLARE_TURNSTILE,
        ):
            # Extract the site key
            site_key = await session.evaluate(
                "document.querySelector('[data-sitekey]')?.getAttribute('data-sitekey') || "
                "document.querySelector('iframe[src*=\"recaptcha\"]')?.src.match(/render=([^&]+)/)?.[1] || "
                "document.querySelector('iframe[src*=\"hcaptcha\"]')?.src.match(/sitekey=([^&]+)/)?.[1] || ''"
            ) or ""

            url = session._current_url

            try:
                token = await self.external_solver.solve(captcha_type, site_key or None, url)
                if token:
                    # Inject the token into the page
                    await session.evaluate(
                        f"document.getElementById('g-recaptcha-response').value = '{token}';"
                        if captcha_type == CaptchaType.RECAPTCHA_V2 else
                        f"window.__captcha_token__ = '{token}';"
                    )
                    return CaptchaResult(
                        captcha_type=captcha_type,
                        solved=True,
                        solver_used="external",
                        duration_seconds=time.monotonic() - start,
                    )
            except Exception as exc:
                logger.warning("External solver failed: %s", exc)
                return CaptchaResult(
                    captcha_type=captcha_type,
                    solved=False,
                    error=f"External solver error: {exc}",
                    duration_seconds=time.monotonic() - start,
                )

        # ── No solver available for this type ──────────────────────
        solver_name = "external" if self.external_solver else "none"
        return CaptchaResult(
            captcha_type=captcha_type,
            solved=False,
            error=f"No solver available for {captcha_type.value} "
                  f"(registered: {solver_name}). For Cloudflare JS challenges, "
                  f"ensure real Chrome is used (not Lightpanda).",
            duration_seconds=time.monotonic() - start,
        )
