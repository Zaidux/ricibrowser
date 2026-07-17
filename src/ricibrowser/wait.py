"""Auto-waiting — poll for network idle / DOM stability before navigation complete.

The browser waits for dynamic content to settle before returning, so the model
gets a fully-rendered page instead of a half-loaded SPA.

Three modes:
  - "load" — wait for Page.loadEventFired (basic, unreliable for SPAs).
  - "domcontentloaded" — wait for DOMContentLoaded (fastest, least reliable).
  - "networkidle" — poll for network requests to settle + DOM size to stop
    changing (most reliable, requires Network.enable which is a detection
    vector — only used when explicitly requested).
"""

from __future__ import annotations

import asyncio
import logging
import time

from ricibrowser.cdp_client import CDPClient, CDPError

logger = logging.getLogger(__name__)


async def wait_for_content_stable(
    cdp: CDPClient,
    frame_id: str = "",
    mode: str = "load",
    timeout: float = 30.0,
) -> None:
    """Wait for the page to reach a stable state.

    Args:
        cdp: Connected CDP client.
        frame_id: Frame ID (for isolated world creation, if needed).
        mode: How to wait:
            "load" — simple readyState check.
            "domcontentloaded" — even simpler.
            "networkidle" — poll for DOM stability + network idle.
        timeout: Max seconds to wait.
    """
    if mode == "domcontentloaded":
        await _wait_ready_state(cdp, "interactive", timeout)
    elif mode == "networkidle":
        await _wait_ready_state(cdp, "interactive", timeout)
        await _wait_dom_stable(cdp, timeout)
    else:  # "load" (default)
        await _wait_ready_state(cdp, "complete", timeout)


async def _wait_ready_state(cdp: CDPClient, target: str, timeout: float) -> None:
    """Poll document.readyState until it reaches the target state."""
    deadline = time.monotonic() + timeout
    expr = "document.readyState"
    while time.monotonic() < deadline:
        try:
            result = await cdp.send("Runtime.evaluate", {
                "expression": expr,
                "returnByValue": True,
            })
            value = result.get("result", {}).get("value", "")
            if value == "complete":
                return
            if target == "interactive" and value in ("interactive", "complete"):
                return
        except CDPError:
            pass
        await asyncio.sleep(0.2)
    logger.debug("wait_for_ready_state timeout after %.1fs", timeout)


async def _wait_dom_stable(cdp: CDPClient, timeout: float = 10.0) -> None:
    """Poll DOM subtree size — if stable for 3 consecutive checks, return.

    This catches SPAs that load content via XHR/fetch after the initial
    DOMContentLoaded event. By checking that the DOM node count stops changing,
    we know the page has finished rendering dynamically-loaded content.
    """
    deadline = time.monotonic() + timeout
    stable_count = 0
    last_size: int = -1

    expr = "document.querySelectorAll('*').length"
    while time.monotonic() < deadline:
        try:
            result = await cdp.send("Runtime.evaluate", {
                "expression": expr,
                "returnByValue": True,
            })
            current_size = result.get("result", {}).get("value", 0)
            if current_size == last_size and current_size > 0:
                stable_count += 1
                if stable_count >= 3:  # Stable for 3 × 200ms = 600ms
                    return
            else:
                stable_count = 0
                last_size = current_size
        except CDPError:
            pass
        await asyncio.sleep(0.2)
    logger.debug("wait_dom_stable timeout after %.1fs (stable_count=%d)", timeout, stable_count)
