"""Lightpanda engine — fast path browser via CDP at ws://127.0.0.1:9222.

Lightpanda is a Zig-based headless browser that speaks CDP natively. It has
no rendering engine (no screenshots), but full JS execution via real V8.
~16× less memory and ~9× faster than Chromium for batch browsing.

Usage::

    from ricibrowser.lightpanda import LightpandaEngine

    engine = LightpandaEngine("ws://127.0.0.1:9222")
    await engine.start()
    page = await engine.browse("https://example.com")
    print(page.title)
    await engine.stop()

    # Or check if it's available without starting:
    if await LightpandaEngine.is_available("ws://127.0.0.1:9222"):
        ...
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ricibrowser.cdp_client import CDPClient, CDPError
from ricibrowser.session import Page
from ricibrowser.utils import (
    detect_cloudflare,
    extract_links,
    strip_html,
    truncate,
    validate_url,
)

logger = logging.getLogger(__name__)


class LightpandaEngine:
    """Fast-path browser engine backed by Lightpanda.

    Connects to a running Lightpanda instance via CDP WebSocket. If Lightpanda
    is not running, the caller should fall back to CDPChromeEngine.
    """

    def __init__(self, ws_url: str = "ws://127.0.0.1:9222", timeout: float = 30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._client: CDPClient | None = None
        self._started = False
        self._target_id: str = ""  # created in start()

    @staticmethod
    async def is_available(ws_url: str = "ws://127.0.0.1:9222") -> bool:
        """Check if a Lightpanda instance is reachable at the given URL.

        Requires Lightpanda to identify itself in the Browser field.
        Does NOT return True for non-Lightpanda CDP servers (Chrome, etc.).
        """
        http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(f"{http_url}/json/version")
                if resp.status_code == 200:
                    data = resp.json()
                    browser = str(data.get("Browser", "")).lower()
                    # Only return True for actual Lightpanda instances.
                    # Chrome identifies as "Chrome/xxx", not "lightpanda".
                    return "lightpanda" in browser
        except Exception:
            pass
        return False

    @property
    def http_url(self) -> str:
        """HTTP discovery URL (derived from ws_url)."""
        return self.ws_url.replace("ws://", "http://").replace("wss://", "https://")

    async def start(self) -> None:
        """Connect to the Lightpanda CDP endpoint and create a browser context.

        Lightpanda requires an explicit Target.createTarget + attachToTarget
        before Page.navigate works (unlike Chrome, which auto-creates a page
        target on connect). We create one on start so browse() can navigate.
        """
        if self._started:
            return
        # Lightpanda exposes the CDP WS endpoint at the root path (not
        # /devtools/browser/<id> like Chrome). Connect directly.
        self._client = await CDPClient.connect(self.ws_url)
        self._started = True

        # Create a browser context — Lightpanda requires this before Page.navigate
        try:
            target_result = await self._client.send("Target.createTarget", {"url": "about:blank"})
            self._target_id = target_result.get("targetId", "")
            await self._client.send("Target.attachToTarget", {
                "targetId": self._target_id,
                "flatten": True,
            })
            logger.info("Lightpanda engine connected to %s (target=%s)", self.ws_url, self._target_id)
        except Exception as exc:
            logger.warning("Lightpanda Target.createTarget failed (may be older version): %s", exc)

    async def stop(self) -> None:
        """Disconnect from Lightpanda."""
        if self._client and not self._client._closed:
            await self._client.close()
        self._client = None
        self._started = False

    async def browse(
        self,
        url: str,
        max_chars: int = 6000,
        wait_for: str | None = None,
    ) -> Page:
        """Browse a URL via Lightpanda and return a Page with content.

        This is the fast path — no screenshots available (Lightpanda has no
        rendering engine). If a screenshot is needed, the caller should fall
        back to CDPChromeEngine.
        """
        url = validate_url(url)
        client = self._ensure_client()

        # Enable Page domain for navigation
        try:
            await client.send("Page.enable")
        except CDPError:
            pass  # Some CDP servers auto-enable Page

        # Navigate
        nav_result = await client.send("Page.navigate", {"url": url})
        frame_id = nav_result.get("frameId", "")

        # Wait for the page to settle (Lightpanda fires Page.loadEventFired)
        # but for simplicity, poll Runtime.evaluate for document.readyState.
        await self._wait_for_ready(client, timeout=self.timeout)

        # Optional: wait for a specific selector
        if wait_for:
            await self._wait_for_selector(client, wait_for, timeout=self.timeout)

        # Extract content via isolated-world evaluation
        context_id = await self._create_isolated_context(client, frame_id)
        title, html, text_raw = await asyncio.gather(
            self._eval_in_context(client, "document.title", context_id),
            self._eval_in_context(client, "document.documentElement.outerHTML", context_id),
            self._eval_in_context(client, "document.body ? document.body.innerText : ''", context_id),
        )

        title_str = str(title.get("result", {}).get("value", "")) if title else ""
        html_str = str(html.get("result", {}).get("value", "")) if html else ""
        text_str = str(text_raw.get("result", {}).get("value", "")) if text_raw else ""

        # If innerText is empty (Lightpanda may not support it), strip HTML
        if not text_str and html_str:
            text_str = strip_html(html_str)

        # Extract links
        links = extract_links(html_str, url)

        # Detect Cloudflare challenge
        is_cf, cf_type = detect_cloudflare(html_str, title_str)

        # Truncate text
        truncated_text, text_truncated = truncate(text_str, max_chars)
        truncated_html, html_truncated = truncate(html_str, max(max_chars * 4, 20_000))

        # Get cookies
        cookies: list[dict] = []
        try:
            cookie_result = await client.send("Network.getCookies")
            cookies = cookie_result.get("cookies", [])
        except CDPError:
            pass

        return Page(
            url=url,
            final_url=url,  # Lightpanda doesn't expose final URL reliably
            status_code=0,  # Unknown: Lightpanda does not expose response status here.
            title=title_str,
            text=truncated_text,
            html=truncated_html,
            links=links,
            cookies=cookies,
            truncated=text_truncated or html_truncated,
            cloudflare_challenge=is_cf,
            cloudflare_type=cf_type,
            screenshot_path=None,
            engine="lightpanda",
        )

    def _ensure_client(self) -> CDPClient:
        if not self._client or self._client._closed:
            raise RuntimeError("Lightpanda engine not started. Call start() first.")
        return self._client

    async def _wait_for_ready(self, client: CDPClient, timeout: float = 10.0) -> None:
        """Poll document.readyState until 'complete' or timeout."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = await client.send("Runtime.evaluate", {
                    "expression": "document.readyState",
                    "returnByValue": True,
                })
                value = result.get("result", {}).get("value", "")
                if value == "complete" or value == "interactive":
                    return
            except CDPError:
                pass
            await asyncio.sleep(0.2)
        logger.debug("Lightpanda: timeout waiting for readyState after %.1fs", timeout)

    async def _wait_for_selector(self, client: CDPClient, selector: str, timeout: float = 10.0) -> None:
        """Poll for a CSS selector to appear in the document."""
        import time
        deadline = time.monotonic() + timeout
        expr = f"document.querySelector({selector!r}) !== null"
        while time.monotonic() < deadline:
            try:
                result = await client.send("Runtime.evaluate", {
                    "expression": expr,
                    "returnByValue": True,
                })
                if result.get("result", {}).get("value") is True:
                    return
            except CDPError:
                pass
            await asyncio.sleep(0.3)

    async def _eval_isolated(self, client: CDPClient, expression: str, frame_id: str = "") -> dict:
        """Evaluate JS in an isolated execution context.

        Per CDP spec: Page.createIsolatedWorld creates a new isolated world.
        We NEVER call Runtime.enable on the main world — all JS evaluation
        goes through isolated worlds to avoid detection.
        """
        if frame_id:
            try:
                world_result = await client.send("Page.createIsolatedWorld", {
                    "frameId": frame_id,
                    "worldName": "ricibrowser_eval",
                })
                context_id = world_result.get("executionContextId")
                if context_id:
                    return await client.send("Runtime.evaluate", {
                        "expression": expression,
                        "contextId": context_id,
                        "returnByValue": True,
                    })
            except CDPError as exc:
                raise CDPError(
                    "Runtime.evaluate", exc.code,
                    "Isolated execution context unavailable",
                ) from exc
        raise CDPError("Runtime.evaluate", -1, "Missing frame for isolated evaluation")

    async def _create_isolated_context(self, client: CDPClient, frame_id: str) -> int | None:
        if not frame_id:
            return None
        try:
            result = await client.send("Page.createIsolatedWorld", {
                "frameId": frame_id,
                "worldName": "ricibrowser_eval",
            })
            return result.get("executionContextId")
        except CDPError:
            return None

    async def _eval_in_context(
        self, client: CDPClient, expression: str, context_id: int | None,
    ) -> dict:
        params: dict[str, Any] = {"expression": expression, "returnByValue": True}
        if context_id is not None:
            params["contextId"] = context_id
        return await client.send("Runtime.evaluate", params)
