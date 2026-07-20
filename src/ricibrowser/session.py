"""Session and Page abstractions — shared interface for both engines.

A :class:`Session` wraps a CDP target and provides high-level methods
(navigate, evaluate, screenshot, click, fill, get_dom). Both the Lightpanda
fast path and the CDP-Chrome thorough path produce the same :class:`Session`
interface so callers can swap engines seamlessly.

A :class:`Page` is the immutable result of a browse/navigate operation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

from ricibrowser.cdp_client import CDPClient, CDPError
from ricibrowser.utils import detect_cloudflare, extract_links, strip_html, truncate, validate_url

logger = logging.getLogger(__name__)


@dataclass
class Page:
    """Result of a browse/navigate operation.

    Immutable snapshot of the page state after navigation.
    """

    url: str
    """The URL requested."""
    final_url: str
    """The URL after redirects (may differ from url)."""
    status_code: int
    """HTTP status code (0 if unknown)."""
    title: str
    """Page <title>."""
    text: str
    """Rendered body text (post-JS, HTML-stripped)."""
    html: str
    """Full rendered DOM HTML (post-JS)."""
    links: list[dict[str, str]] = field(default_factory=list)
    """Extracted links [{text, href}, ...]."""
    cookies: list[dict] = field(default_factory=list)
    """Cookies from the browser context."""
    truncated: bool = False
    """Whether text/html was truncated."""
    cloudflare_challenge: bool = False
    """Whether a Cloudflare/anti-bot challenge was detected."""
    cloudflare_type: str | None = None
    """Challenge type ('cloudflare', 'generic_captcha', or None)."""
    screenshot_path: str | None = None
    """Path to screenshot PNG (None if not taken)."""
    engine: str = "unknown"
    """Which engine produced this page ('lightpanda' or 'cdp_chrome')."""

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict matching the existing tool_browse return format."""
        return {
            "status": "ok",
            "tool": "browse",
            "url": self.final_url,
            "http_status": self.status_code,
            "title": self.title,
            "text": self.text,
            "html": self.html,
            "links": self.links,
            "link_count": len(self.links),
            "truncated": self.truncated,
            "cookies": self.cookies,
            "stealth": True,
            "anti_bot_detected": self.cloudflare_challenge,
            "anti_bot_type": self.cloudflare_type,
            "screenshot_path": self.screenshot_path,
            "engine": self.engine,
        }


class Session:
    """High-level browser session wrapping a CDP target.

    Provides navigate/evaluate/screenshot/click/fill methods. Maintains
    isolated-world JS execution (never calls Runtime.enable on the main world).
    """

    def __init__(self, cdp: CDPClient, engine_name: str = "cdp_chrome"):
        self._cdp = cdp
        self._engine_name = engine_name
        self._frame_id: str = ""
        self._isolated_context_id: int | None = None
        self._page_enabled = False
        self._current_url: str = ""
        # Register for frame navigation events so we invalidate the isolated
        # context when the frame changes (link clicks, SPA navigations, etc.).
        self._setup_frame_listener()

    def _setup_frame_listener(self) -> None:
        """Register a CDP event handler that invalidates the isolated context
        when the main frame navigates (even page-initiated navigations).

        Without this, the cached _isolated_context_id points at a destroyed
        execution context after a SPA navigation or link click, and
        evaluate() silently fails with a stale contextId error.
        """
        def _on_frame_navigated(params: dict) -> None:
            # The main frame's id changes on navigation
            new_frame_id = params.get("frame", {}).get("id", "")
            if new_frame_id and new_frame_id != self._frame_id:
                self._frame_id = new_frame_id
                self._isolated_context_id = None  # Force recreation
                logger.debug("Frame navigated, isolated context invalidated")

        # Register the callback (sync — the CDPClient handles async dispatch)
        self._cdp._event_handlers.setdefault("Page.frameNavigated", []).append(_on_frame_navigated)

    async def _ensure_page_enabled(self) -> None:
        """Enable the Page domain (needed for navigation events)."""
        if not self._page_enabled:
            try:
                await self._cdp.send("Page.enable")
                self._page_enabled = True
            except CDPError:
                pass

    async def _get_or_create_isolated_world(self) -> int | None:
        """Get or create an isolated execution context for JS evaluation.

        Per the CDP spec: Page.createIsolatedWorld creates a new isolated
        world for the given frame. We NEVER call Runtime.enable on the main
        context — all JS evaluation goes through isolated worlds.
        """
        if self._isolated_context_id is not None:
            return self._isolated_context_id
        if not self._frame_id:
            return None
        try:
            result = await self._cdp.send("Page.createIsolatedWorld", {
                "frameId": self._frame_id,
                "worldName": "ricibrowser_isolated",
            })
            self._isolated_context_id = result.get("executionContextId")
            return self._isolated_context_id
        except CDPError as exc:
            logger.warning("Could not create isolated world: %s", exc)
            return None

    async def navigate(self, url: str, wait_until: str = "load", max_chars: int = 10_000) -> Page:
        """Navigate to a URL and wait for the page to settle.

        Args:
            url: The URL to navigate to.
            wait_until: When to consider navigation complete.
                "load" — wait for the load event.
                "domcontentloaded" — wait for DOMContentLoaded.
                "networkidle" — wait for network to be idle (requires Network.enabled).

        Returns:
            A Page with the current state.
        """
        url = validate_url(url)
        await self._ensure_page_enabled()

        # Enable Network domain only if we want network-idle waiting
        # or debug capture. Otherwise leave it off (detection vector).
        if wait_until == "networkidle":
            try:
                await self._cdp.send("Network.enable")
            except CDPError:
                pass

        result = await self._cdp.send("Page.navigate", {"url": url})
        self._frame_id = result.get("frameId", "")
        self._current_url = url

        # Reset the isolated world (frame changed)
        self._isolated_context_id = None

        # Wait for content
        from ricibrowser.wait import wait_for_content_stable
        await wait_for_content_stable(self._cdp, self._frame_id, mode=wait_until)

        return await self._capture_page(url, max_chars=max_chars)

    async def _capture_page(self, url: str, max_chars: int = 10_000) -> Page:
        """Capture the current page state into a Page object."""
        snapshot = await self.evaluate_value("""({
            title: document.title || '',
            html: document.documentElement ? document.documentElement.outerHTML : '',
            text: document.body ? document.body.innerText : '',
            url: location.href
        })""")
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        title = str(snapshot.get("title", ""))
        html = str(snapshot.get("html", ""))
        text = str(snapshot.get("text", ""))
        final_url = str(snapshot.get("url", "") or self._current_url)

        if not text and html:
            text = strip_html(html)

        links = extract_links(html, url)

        # Get HTTP status (not always available via CDP)
        status_code = 0

        # Get cookies
        cookies = await self.get_cookies()

        # Detect Cloudflare
        is_cf, cf_type = detect_cloudflare(html, title)

        # Truncate
        truncated_text, text_truncated = truncate(text, max_chars)
        truncated_html, html_truncated = truncate(html, max(max_chars * 4, 20_000))

        return Page(
            url=url,
            final_url=final_url,
            status_code=status_code,
            title=title,
            text=truncated_text,
            html=truncated_html,
            links=links,
            cookies=cookies,
            truncated=text_truncated or html_truncated,
            cloudflare_challenge=is_cf,
            cloudflare_type=cf_type,
            engine=self._engine_name,
        )

    async def evaluate(self, expression: str) -> str | None:
        """Evaluate JavaScript in an isolated world and return the result.

        NEVER calls Runtime.enable on the main world — uses
        Page.createIsolatedWorld to create a separate execution context.
        If the isolated context is unavailable (before first navigate, or
        after an unobserved frame change), returns None with a warning
        rather than silently falling back to the main world.
        """
        value = await self.evaluate_value(expression)
        return str(value) if value is not None else None

    async def evaluate_value(self, expression: str) -> Any:
        """Evaluate in the isolated world and preserve JSON-compatible types."""
        context_id = await self._get_or_create_isolated_world()
        if context_id is None:
            logger.warning("JS evaluation skipped: isolated context unavailable")
            return None
        params: dict[str, Any] = {
            "expression": expression,
            "returnByValue": True,
        }
        params["contextId"] = context_id

        try:
            result = await self._cdp.send("Runtime.evaluate", params)
            return result.get("result", {}).get("value")
        except CDPError as exc:
            # If the contextId was stale (frame changed underneath us), reset
            # and retry once with a fresh isolated world.
            if "context" in exc.message.lower():
                self._isolated_context_id = None
                retry_context = await self._get_or_create_isolated_world()
                if retry_context is not None:
                    params["contextId"] = retry_context
                    try:
                        result = await self._cdp.send("Runtime.evaluate", params)
                        return result.get("result", {}).get("value")
                    except CDPError:
                        pass
            logger.warning("JS evaluation failed: %s", exc)
            return None

    async def evaluate_bool(self, expression: str) -> bool | None:
        """Evaluate a JS expression that returns a boolean.

        Returns True/False, or None if evaluation failed or returned
        a non-boolean value. Centralizes the str(bool) comparison so
        callers don't need magic tuples.
        """
        result = await self.evaluate(expression)
        if result is None:
            return None
        # CDP returns Python bool → str(True) = "True"
        if result in ("true", "True"):
            return True
        if result in ("false", "False"):
            return False
        return None

    async def screenshot(self, path: str | None = None, full_page: bool = False) -> str:
        """Take a screenshot and save to a PNG file.

        Note: screenshots require a rendering engine. Lightpanda does NOT
        support this — the caller should use CDPChromeEngine for screenshots.

        Returns the path to the saved PNG.
        """
        if path is None:
            fd, path = tempfile.mkstemp(suffix=".png", prefix="ricibrowser_")
            os.close(fd)

        params: dict[str, Any] = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True

        try:
            result = await self._cdp.send("Page.captureScreenshot", params)
            data_b64 = result.get("data", "")
            if data_b64:
                with open(path, "wb") as f:
                    f.write(base64.b64decode(data_b64))
                return path
        except CDPError as exc:
            logger.warning("Screenshot failed: %s", exc)

        return path

    async def click(self, selector: str, timeout: float = 5.0) -> bool:
        """Click an element matching a CSS selector.
        Returns True if the click succeeded."""
        selector_json = json.dumps(selector)
        js = f"""
        (function() {{
            const el = document.querySelector({selector_json});
            if (!el) return false;
            el.click();
            return true;
        }})()
        """
        result = await self.evaluate(js)
        # CDP returns Python bool → str(True) = "True", not "true"
        return result in ("true", "True") or result is True

    async def fill(self, selector: str, value: str, timeout: float = 5.0) -> bool:
        """Fill an input element with a value.
        Returns True if the fill succeeded."""
        # Escape the value for JS string injection
        selector_json = json.dumps(selector)
        value_json = json.dumps(value)
        js = f"""
        (function() {{
            const el = document.querySelector({selector_json});
            if (!el) return false;
            el.value = {value_json};
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }})()
        """
        result = await self.evaluate(js)
        return result in ("true", "True") or result is True

    async def get_dom(self) -> str:
        """Return the full rendered DOM HTML."""
        return await self.evaluate("document.documentElement.outerHTML") or ""

    async def get_cookies(self) -> list[dict]:
        """Get all cookies from the browser context."""
        try:
            result = await self._cdp.send("Network.getCookies")
            return result.get("cookies", [])
        except CDPError:
            return []

    async def set_cookies(self, cookies: list[dict]) -> None:
        """Set cookies in the browser context."""
        try:
            await self._cdp.send("Network.setCookies", {"cookies": cookies})
        except CDPError as exc:
            logger.warning("set_cookies failed: %s", exc)

    async def close(self) -> None:
        """Close the session and its CDP connection."""
        if self._cdp and not self._cdp._closed:
            await self._cdp.close()
