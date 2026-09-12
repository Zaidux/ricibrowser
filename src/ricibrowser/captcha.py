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
import base64
import io
import json
import logging
import random
import time
from dataclasses import dataclass, field
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

    SLIDER = "slider"
    """Drag-the-handle slider puzzle (Aliyun/geetest-style). Solved
    in-engine: gap detection + trusted human-like drag."""

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
    detail: dict = field(default_factory=dict)
    """Solver-specific diagnostics (attempts, detected gap offset, ...)."""


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

    # Slider puzzle (Aliyun/geetest-style): a drag handle next to a puzzle
    # image. Checked before the generic text probe because the widget is
    # often present without any challenge message in the body text.
    slider = await session.evaluate_bool(_SLIDER_PRESENT_JS)
    if slider:
        return CaptchaType.SLIDER

    # Generic anti-bot check
    if any(p in text_lower for p in(
        "enable javascript and cookies", "please complete the security check",
        "verify you are human", "are you a robot",
    )):
        return CaptchaType.GENERIC

    return CaptchaType.NONE


# ── Slider puzzle solver (Aliyun / geetest style) ─────────────────────

# Common widget roots and handle selectors across the major slider CAPTCHAs.
_SLIDER_HANDLE_SELECTORS = (
    ".nc_iconfont", "#nc_1_n1z", ".nc-container .btn_slide",
    ".geetest_slider_button", ".geetest_slide", ".verify-move-block",
    "[class*='slider-btn']", "[class*='sliderBtn']", "[class*='slide-btn']",
    "[class*='slideBlock']", "[class*='sliderIcon']",
)
_SLIDER_CONTAINER_SELECTORS = (
    ".nc-container", ".nc_wrapper", ".geetest_holder", ".geetest_panel",
    "[class*='slide-verify']", "[class*='sliderCaptcha']", "[class*='slider-captcha']",
)
_SLIDER_TEXT_HINTS = (
    "drag to complete the puzzle", "drag the slider", "slide to verify",
    "swipe to verify", "slide right", "拖动滑块", "按住滑块", "请完成安全验证",
    "滑动验证", "向右滑动", "完成拼图",
)

_SLIDER_PRESENT_JS = """
(function() {
    // Known captcha signatures are trusted outright — these class names
    // only exist on Aliyun/geetest widgets, never on plain UI sliders.
    var known = ['.nc-container', '.nc_wrapper', '.nc_iconfont',
                 '.geetest_holder', '.geetest_panel', '.geetest_slider_button',
                 '#nc_1_n1z'];
    for (var i = 0; i < known.length; i++) {
        if (document.querySelector(known[i])) return true;
    }
    // Body-text hints (zh + en) are strong signals on their own.
    var text = (document.body ? document.body.innerText : '').toLowerCase();
    var hints = %s;
    for (var h = 0; h < hints.length; h++) {
        if (text.indexOf(hints[h]) !== -1) return true;
    }
    // Generic slider-looking containers only count when their own label
    // reads like a challenge — otherwise a plain range-slider UI component
    // would be mistaken for a captcha (and dragged!).
    var generic = ["[class*='slide-verify']", "[class*='sliderCaptcha']",
                   "[class*='slider-captcha']", "[class*='slider-canvas']"];
    for (var g = 0; g < generic.length; g++) {
        var el = document.querySelector(generic[g]);
        if (!el) continue;
        var t = (el.innerText || '').toLowerCase();
        var labels = ['验证', 'verify', '拖', 'drag', 'slide', 'puzzle',
                      'security', '安全'];
        for (var l = 0; l < labels.length; l++) {
            if (t.indexOf(labels[l]) !== -1) return true;
        }
    }
    return false;
})()
""" % json.dumps(list(_SLIDER_TEXT_HINTS))


def find_gap_offset(
    image_bytes: bytes,
    handle_center_x: float,
    min_ratio: float = 0.25,
) -> tuple[float | None, dict]:
    """Locate the puzzle notch in a slider background image.

    Pure function (unit-testable): column-edge-energy heuristic — the notch
    has strong vertical edges, so summing per-column intensity deltas and
    taking the strongest peak right of the handle's start gives the gap.
    Returns ``(dx, info)`` where dx is the horizontal drag distance from the
    handle's centre, or ``(None, info)`` when the image can't be analysed.
    """
    info: dict = {}
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a declared dependency
        return None, {"error": "Pillow unavailable for gap analysis"}
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception as exc:
        return None, {"error": f"cannot decode capture: {exc}"}
    w, h = img.size
    info["image_width"] = w
    info["image_height"] = h
    if w < 20 or h < 10:
        return None, {**info, "error": "capture too small"}
    px = img.load()
    profile: list[int] = []
    for x in range(1, w):
        energy = 0
        for y in range(0, h, 2):
            d = px[x, y] - px[x - 1, y]
            energy += d if d >= 0 else -d
        profile.append(energy)
    start = int(w * min_ratio)
    if start >= len(profile) - 1:
        start = 1
    peak_index = max(range(start, len(profile)), key=lambda i: profile[i])
    peak_x = peak_index + 1
    info["peak_x"] = peak_x
    info["peak_energy"] = int(profile[peak_index])
    info["profile_avg"] = int(sum(profile) / max(1, len(profile)))
    dx = float(peak_x) - float(handle_center_x)
    return dx, info


async def _capture_clip(session, rect: dict[str, float]) -> bytes | None:
    """Screenshot a page region via CDP (no navigation, no canvas taint issues)."""
    cdp = getattr(session, "_cdp", None)
    if cdp is None:
        return None
    try:
        result = await cdp.send("Page.captureScreenshot", {
            "format": "png",
            "clip": {
                "x": max(0.0, float(rect.get("x", 0))),
                "y": max(0.0, float(rect.get("y", 0))),
                "width": max(1.0, float(rect.get("width", 1))),
                "height": max(1.0, float(rect.get("height", 1))),
                "scale": 1,
            },
            "captureBeyondViewport": False,
        })
    except Exception as exc:
        logger.debug("slider capture failed: %s", exc)
        return None
    data = (result or {}).get("data") if isinstance(result, dict) else None
    if not data:
        return None
    try:
        return base64.b64decode(data)
    except Exception:
        return None


_SLIDER_GEOMETRY_JS = f"""
(function() {{
    var handleSels = {json.dumps(list(_SLIDER_HANDLE_SELECTORS))};
    var rootSels = {json.dumps(list(_SLIDER_CONTAINER_SELECTORS))};
    var handle = null;
    for (var i = 0; i < handleSels.length; i++) {{
        var el = document.querySelector(handleSels[i]);
        if (el) {{
            var r = el.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) {{ handle = el; break; }}
        }}
    }}
    if (!handle) return JSON.stringify(null);
    // Bring the whole widget into the viewport BEFORE measuring: the
    // background is captured via a CDP viewport clip, so an off-screen
    // widget would screenshot the wrong region of the page.
    if (typeof handle.scrollIntoView === 'function') {{
        handle.scrollIntoView({{block: 'center', inline: 'nearest'}});
    }}
    // Tag the handle so the mouse layer gets a stable, unique selector.
    handle.setAttribute('data-riciplay-slider', '1');
    var hr = handle.getBoundingClientRect();
    // Track: nearest ancestor wide enough to be the slider rail.
    var track = handle.parentElement, guard = 0;
    while (track && guard++ < 6) {{
        var tr = track.getBoundingClientRect();
        if (tr.width > hr.width * 2) break;
        track = track.parentElement;
    }}
    if (!track) track = handle.parentElement || handle;
    var trr = track.getBoundingClientRect();
    // Puzzle image: largest img/canvas inside the widget root, else the
    // region of the track above the handle (the classic stacked layout).
    var root = null;
    for (var j = 0; j < rootSels.length; j++) {{
        var cand = document.querySelector(rootSels[j]);
        if (cand && cand.contains(handle)) {{ root = cand; break; }}
    }}
    if (!root) root = track;
    var rr = root.getBoundingClientRect();
    var img = null, imgArea = 0;
    var media = root.querySelectorAll('img, canvas');
    for (var m = 0; m < media.length; m++) {{
        var mr = media[m].getBoundingClientRect();
        var area = mr.width * mr.height;
        if (area > imgArea && mr.width > 20 && mr.height > 20) {{ img = media[m]; imgArea = area; }}
    }}
    var ir = img ? img.getBoundingClientRect() : {{
        x: rr.x, y: rr.y, width: rr.width, top: rr.y,
        height: Math.max(10, calcTop(rr, hr)),
    }};
    function calcTop(rr2, hr2) {{
        var above = hr2.y - rr2.y;
        return above > 10 ? above : rr2.height;
    }}
    return JSON.stringify({{
        handle_selector: '[data-riciplay-slider="1"]',
        handle: {{x: hr.x, y: hr.y, width: hr.width, height: hr.height}},
        track: {{x: trr.x, y: trr.y, width: trr.width, height: trr.height}},
        image: {{x: ir.x, y: ir.y, width: ir.width, height: ir.height}},
        root: {{x: rr.x, y: rr.y, width: rr.width, height: rr.height}},
    }});
}})()
"""

_SLIDER_STATE_JS = """
(function() {
    // Success detection, strongest signal first:
    //   1. widget removed from the document or zero-sized
    //   2. the WIDGET's own text reads solved (scoped — never scan the
    //      whole body, where 'success' as a substring matches
    //      'unsuccessful' and any unrelated success message)
    var roots = ['.nc-container', '.geetest_slider', '[class*="slide-verify"]',
                 '[class*="sliderCaptcha"]', '[class*="slider-captcha"]',
                 '.geetest_holder'];
    var widget = null;
    for (var j = 0; j < roots.length; j++) {
        var el = document.querySelector(roots[j]);
        if (el) { widget = el; break; }
    }
    if (!widget) return 'success';
    if (!widget.isConnected) return 'success';
    var wr = widget.getBoundingClientRect();
    if (wr.width === 0 && wr.height === 0) return 'success';
    var t = (widget.innerText || '').toLowerCase();
    var ok = ['验证通过', '验证成功', '通过验证', '安全验证通过',
              'verified', 'verification complete'];
    for (var i = 0; i < ok.length; i++) {
        if (t.indexOf(ok[i]) !== -1) return 'success';
    }
    return 'pending';
})()
"""


async def _resolve_slider(session) -> dict | None:
    try:
        raw = await session.evaluate(_SLIDER_GEOMETRY_JS)
    except Exception as exc:
        logger.debug("slider geometry resolve failed: %s", exc)
        return None
    if not raw:
        return None
    try:
        geo = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(geo, dict) or "handle" not in geo:
        return None
    return geo


class SliderCaptchaSolver:
    """Solves drag-handle slider puzzles (Aliyun/geetest-style) in-engine.

    Pipeline:
      1. Resolve the handle + track + puzzle-image geometry from the DOM.
      2. Screenshot the puzzle via CDP ``Page.captureScreenshot`` with a clip
         (no navigation, and no canvas-taint/CORS problems that broke the
         in-page image reads).
      3. Edge-energy gap detection (pure function) for the drag distance.
      4. Trusted human-like drag through ``HumanMouse`` — the same
         ``Input.dispatchMouseEvent`` channel a real user drives, so bot
         heuristics see natural motion.
      5. Verify success and retry with jitter when the target rejects.
    """

    def __init__(self, attempts: int = 3, settle_s: float = 0.6):
        self.attempts = max(1, attempts)
        self.settle_s = settle_s

    async def solve(self, session) -> CaptchaResult:
        start = time.monotonic()

        from ricibrowser.input import HumanMouse

        cdp = getattr(session, "_cdp", None)
        mouse = HumanMouse(cdp) if cdp is not None else None
        if mouse is None:
            return CaptchaResult(
                captcha_type=CaptchaType.SLIDER, solved=False,
                error="Slider solving needs the trusted-input mouse (CDP engine); "
                      "the active session has no CDP channel.",
                duration_seconds=time.monotonic() - start,
            )

        last_detail: dict = {}
        for attempt in range(self.attempts):
            geo = await _resolve_slider(session)
            if geo is None:
                return CaptchaResult(
                    captcha_type=CaptchaType.SLIDER, solved=False,
                    error="No slider widget found on the current page.",
                    detail=last_detail,
                    duration_seconds=time.monotonic() - start,
                )

            handle = geo["handle"]
            container = geo.get("root") or geo.get("track") or handle
            image_rect = geo.get("image") or container
            handle_center_x = (
                handle["x"] + handle["width"] / 2.0 - image_rect["x"]
            )

            image = await _capture_clip(session, image_rect)
            dx: float | None = None
            gap_info: dict = {}
            if image:
                dx, gap_info = find_gap_offset(image, handle_center_x)
            if dx is None:
                return CaptchaResult(
                    captcha_type=CaptchaType.SLIDER, solved=False,
                    error=f"Could not analyse the slider image: {gap_info.get('error', 'capture unavailable')}",
                    detail={"attempt": attempt + 1, **gap_info},
                    duration_seconds=time.monotonic() - start,
                )

            # Keep the drag inside the rail; jitter retries for calibration.
            max_dx = max(4.0, float(geo["track"]["width"]) - float(handle["width"]))
            dx = max(4.0, min(float(dx), max_dx))
            if attempt:
                dx = max(4.0, min(dx + random.uniform(-10, 10), max_dx))

            last_detail = {
                "attempt": attempt + 1,
                "drag_dx": round(dx, 1),
                "handle_center_x": round(handle_center_x, 1),
                **gap_info,
            }
            logger.info("Slider attempt %d: dragging dx=%.1f", attempt + 1, dx)

            ok = await mouse.drag_element(
                session, geo["handle_selector"], dx=dx, dy=0.0,
            )
            if not ok:
                last_detail["error"] = "drag dispatch failed"
                continue

            await asyncio.sleep(self.settle_s)
            state = await session.evaluate(_SLIDER_STATE_JS)
            last_detail["state"] = state
            if state == "success":
                return CaptchaResult(
                    captcha_type=CaptchaType.SLIDER, solved=True,
                    solver_used="slider_gap_analysis",
                    detail=last_detail,
                    duration_seconds=time.monotonic() - start,
                )

        return CaptchaResult(
            captcha_type=CaptchaType.SLIDER, solved=False,
            error=f"Slider verification did not pass after {self.attempts} attempt(s).",
            detail=last_detail,
            duration_seconds=time.monotonic() - start,
        )


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
        slider_solver: SliderCaptchaSolver | None = None,
    ):
        self.auto_solver = auto_solver or CloudflareAutoSolver()
        self.external_solver = external_solver
        self.slider_solver = slider_solver or SliderCaptchaSolver()

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

        # ── Slider puzzle → in-engine gap analysis + trusted drag ──
        if captcha_type == CaptchaType.SLIDER:
            return await self.slider_solver.solve(session)

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
                        f"document.getElementById('g-recaptcha-response').value = {json.dumps(token)};"
                        if captcha_type == CaptchaType.RECAPTCHA_V2 else
                        f"window.__captcha_token__ = {json.dumps(token)};"
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
