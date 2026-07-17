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

    @staticmethod
    async def is_available(ws_url: str = "ws://127.0.0.1:9222") -> bool:
        """Check if a Lightpanda instance is reachable at the given URL.

        Tries the HTTP /json/version endpoint that Lightpanda exposes.
        """
        http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
        try:
            async with httpx.AsyncClient(timeout=3.0) as http:
                resp = await http.get(f"{http_url}/json/version")
                if resp.status_code == 200:
                    data = resp.json()
                    # Lightpanda identifies itself in the Browser field
                    browser = str(data.get("Browser", "")).lower()
                    return "lightpanda" in browser or "Browser" in data
        except Exception:
            pass
        return False

    @property
    def http_url(self) -> str:
        """HTTP discovery URL (derived from ws_url)."""
        return self.ws_url.replace("ws://", "http://").replace("wss://", "https://")

    async def start(self) -> None:
        """Connect to the Lightpanda CDP endpoint."""
        if self._started:
            return
        # Lightpanda exposes the CDP WS endpoint at the root path (not
        # /devtools/browser/<id> like Chrome). Connect directly.
        self._client = await CDPClient.connect(self.ws_url)
        self._started = True
        logger.info("Lightpanda engine connected to %s", self.ws_url)

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
        title = await self._eval_isolated(client, "document.title", frame_id)
        html = await self._eval_isolated(client, "document.documentElement.outerHTML", frame_id)
        text_raw = await self._eval_isolated(client, "document.body ? document.body.innerText : ''", frame_id)

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
        truncated_text, was_truncated = truncate(text_str, max_chars)

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
            status_code=200,  # Lightpanda doesn't expose HTTP status via CDP
            title=title_str,
            text=truncated_text,
            html=html_str,
            links=links,
            cookies=cookies,
            truncated=was_truncated,
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
            except CDPError:
                pass  # Fall through to non-isolated eval

        # Fallback: evaluate without isolated world (if createIsolatedWorld not supported)
        return await client.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
        })
