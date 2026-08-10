"""Auto-waiting — poll for network idle / DOM stability before navigation complete.

The browser waits for dynamic content to settle before returning, so the model
gets a fully-rendered page instead of a half-loaded SPA.

Three modes:
  - "load" — wait for Page.loadEventFired (basic, unreliable for SPAs).
  - "domcontentloaded" — wait for DOMContentLoaded (fastest, least reliable).
  - "networkidle" — wait for in-flight requests (tracked via CDP Network
    events) to drain + DOM size to stop changing (most reliable, requires
    Network.enable which is a detection vector — only used when explicitly
    requested).
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
        # Event-driven network idle: counts in-flight requests from CDP
        # Network events (no page instrumentation).
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


async def _wait_network_idle(cdp: CDPClient, timeout: float = 5.0,
                             quiet_period: float = 0.5) -> None:
    """Wait until no network request has been in flight for *quiet_period*.

    In-flight requests are counted from CDP ``Network`` events —
    ``requestWillBeSent`` to open, ``loadingFinished``/``loadingFailed`` to
    close. The previous implementation injected a ``fetch``/``XMLHttpRequest``
    wrapper into the page, which was both a detection vector (a patched
    ``window.fetch`` is trivially observable from page script, and it was
    injected into the MAIN world where site code can read it) and a
    correctness problem (it ignored images, scripts, stylesheets and
    navigations, and permanently mutated the page's globals).

    Requests already in flight before this function subscribes are invisible to
    it — CDP has no "list pending requests" call. That only shortens the wait,
    never hangs it, and the DOM-stability check that runs alongside covers the
    common case.
    """
    # The Network domain must be on for the events to arrive. session.navigate()
    # already enables it, but this helper is also reachable directly.
    try:
        await cdp.send("Network.enable")
    except CDPError:
        return

    inflight: set[str] = set()
    # Redirect hops re-fire requestWillBeSent for the SAME requestId; counting
    # them again would leave a permanently unbalanced counter.
    seen: set[str] = set()

    def _on_request(params: dict) -> None:
        request_id = params.get("requestId")
        if not request_id or request_id in seen:
            return
        seen.add(request_id)
        inflight.add(request_id)

    def _on_done(params: dict) -> None:
        request_id = params.get("requestId")
        if request_id:
            inflight.discard(request_id)

    handlers = (
        ("Network.requestWillBeSent", _on_request),
        ("Network.loadingFinished", _on_done),
        ("Network.loadingFailed", _on_done),
    )
    for name, cb in handlers:
        cdp._event_handlers.setdefault(name, []).append(cb)

    try:
        deadline = time.monotonic() + timeout
        quiet_since: float | None = time.monotonic()
        while time.monotonic() < deadline:
            if inflight:
                quiet_since = None
            else:
                now = time.monotonic()
                if quiet_since is None:
                    quiet_since = now
                elif now - quiet_since >= quiet_period:
                    return
            await asyncio.sleep(0.05)
        logger.debug(
            "wait_network_idle timeout after %.1fs (%d request(s) still in flight)",
            timeout, len(inflight),
        )
    finally:
        for name, cb in handlers:
            registered = cdp._event_handlers.get(name)
            if registered and cb in registered:
                registered.remove(cb)
