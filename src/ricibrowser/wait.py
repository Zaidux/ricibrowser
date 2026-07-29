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
            "domcontentloaded" — fastest, least reliable.
            "networkidle" — poll for DOM stability + check for outstanding
                network requests (most reliable, but requires Network.enable
                which is a detection vector).
            "domstable" — alias for DOM-stability-only (no network check).
        timeout: Max seconds to wait.
    """
    if mode == "domcontentloaded":
        await _wait_ready_state(cdp, "interactive", timeout)
    elif mode == "networkidle":
        await _wait_ready_state(cdp, "interactive", timeout)
        await _wait_dom_stable(cdp, timeout)
        # Best-effort network idle check: poll for pending requests via
        # CDP. If Network domain isn't enabled, this is a no-op.
        await _wait_network_idle(cdp, timeout=5.0)
    elif mode == "domstable":
        await _wait_ready_state(cdp, "interactive", timeout)
        await _wait_dom_stable(cdp, timeout)
    else:  # "load" (default)
        await _wait_ready_state(cdp, "complete", timeout)


async def _wait_ready_state(cdp: CDPClient, target: str, timeout: float) -> None:
    """Poll document.readyState until it reaches the target state.

    Note: callers in :mod:`ricibrowser.session` now await the real
    ``Page.loadEventFired`` signal *before* invoking this, so by the time we
    poll, ``readyState`` belongs to the committed target document — not the
    stale about:blank / previous page that used to answer 'complete'
    immediately and cause blank captures.
    """
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
        await asyncio.sleep(0.1)
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


async def _wait_network_idle(cdp: CDPClient, timeout: float = 5.0) -> None:
    """Best-effort network-idle check using Network domain CDP API.

    Since session.py now enables Network.enable by default (required for
    SSO redirect cookie capture), we can check for inflight network requests
    via a JS counter that tracks XMLHttpRequest and fetch() in progress.
    """
    _NET_IDLE_JS = """
    (function() {
        if (window.__ricibrowser_net_idle_active === true) return (window.__ricibrowser_net_outstanding || 0);
        window.__ricibrowser_net_idle_active = true;
        window.__ricibrowser_net_outstanding = 0;
        var _origFetch = window.fetch;
        window.fetch = function() {
            window.__ricibrowser_net_outstanding++;
            var p = _origFetch.apply(this, arguments);
            p.finally(function() { window.__ricibrowser_net_outstanding--; });
            return p;
        };
        var _origXHROpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function() {
            this.addEventListener('loadend', function() { window.__ricibrowser_net_outstanding--; });
            window.__ricibrowser_net_outstanding++;
            return _origXHROpen.apply(this, arguments);
        };
        return 0;
    })()
    """
    import time
    deadline = time.monotonic() + timeout

    # Inject the counter on first call (idempotent)
    try:
        await cdp.send("Runtime.evaluate", {
            "expression": _NET_IDLE_JS,
            "returnByValue": True,
        })
    except CDPError:
        return

    while time.monotonic() < deadline:
        try:
            result = await cdp.send("Runtime.evaluate", {
                "expression": "window.__ricibrowser_net_outstanding || 0",
                "returnByValue": True,
            })
            value = result.get("result", {}).get("value", 0)
            if not value or int(value) == 0:
                return
        except CDPError:
            return
        await asyncio.sleep(0.3)
