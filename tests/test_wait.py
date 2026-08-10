"""Tests for the wait module — auto-waiting helpers."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ricibrowser.cdp_client import CDPError
from ricibrowser.wait import _wait_network_idle, wait_for_content_stable


@pytest.mark.asyncio
async def test_wait_ready_state_complete():
    """ReadyState 'complete' returns immediately."""
    cdp = MagicMock()
    result = MagicMock()
    result.get.return_value = {"value": "complete"}
    cdp.send = AsyncMock(return_value=result)

    await wait_for_content_stable(cdp, mode="load", timeout=1.0)
    # Should have sent at least one Runtime.evaluate
    cdp.send.assert_called()


@pytest.mark.asyncio
async def test_wait_ready_state_interactive():
    """Domcontentloaded mode accepts 'interactive'."""
    cdp = MagicMock()
    result = MagicMock()
    result.get.return_value = {"value": "interactive"}
    cdp.send = AsyncMock(return_value=result)

    await wait_for_content_stable(cdp, mode="domcontentloaded", timeout=1.0)


@pytest.mark.asyncio
async def test_wait_timeout():
    """If readyState never reaches target, we timeout gracefully."""
    cdp = MagicMock()
    result = MagicMock()
    result.get.return_value = {"value": "loading"}
    cdp.send = AsyncMock(return_value=result)

    # Should not raise — just log and return
    await wait_for_content_stable(cdp, mode="load", timeout=0.5)


@pytest.mark.asyncio
async def test_wait_cdp_error_handled():
    """CDP errors during wait don't crash."""
    cdp = MagicMock()
    cdp.send = AsyncMock(side_effect=CDPError("Runtime.evaluate", -1, "noop"))

    await wait_for_content_stable(cdp, mode="load", timeout=0.5)


def _net_client():
    """Minimal CDP double exposing the event-handler registry."""
    cdp = MagicMock()
    cdp._event_handlers = {}
    cdp.send = AsyncMock(return_value={})
    return cdp


def _emit(cdp, event, params):
    for cb in list(cdp._event_handlers.get(event, [])):
        cb(params)


@pytest.mark.asyncio
async def test_network_idle_uses_events_not_js_injection():
    """Idle is decided from Network events; no JS is evaluated in the page.

    The old implementation monkeypatched window.fetch / XMLHttpRequest in the
    MAIN world — observable from page script and it mutated site globals.
    """
    cdp = _net_client()
    await _wait_network_idle(cdp, timeout=2.0, quiet_period=0.1)

    methods = [call.args[0] for call in cdp.send.await_args_list]
    assert "Network.enable" in methods
    assert "Runtime.evaluate" not in methods


@pytest.mark.asyncio
async def test_network_idle_waits_for_inflight_request_to_finish():
    """An open request keeps us waiting; finishing it releases the wait."""
    cdp = _net_client()

    async def _traffic():
        await asyncio.sleep(0.05)
        _emit(cdp, "Network.requestWillBeSent", {"requestId": "1"})
        await asyncio.sleep(0.2)
        _emit(cdp, "Network.loadingFinished", {"requestId": "1"})

    task = asyncio.create_task(_traffic())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _wait_network_idle(cdp, timeout=3.0, quiet_period=0.1)
    elapsed = loop.time() - started
    await task

    # Must have waited past the request's lifetime (0.05 + 0.2) plus quiet period.
    assert elapsed >= 0.3
    # Handlers are unregistered on exit — no leak into later navigations.
    assert all(not handlers for handlers in cdp._event_handlers.values())


@pytest.mark.asyncio
async def test_network_idle_redirect_hops_do_not_unbalance_counter():
    """A redirect re-fires requestWillBeSent for the same requestId. Counting
    it twice would leave the counter permanently non-zero and always time out."""
    cdp = _net_client()

    async def _traffic():
        await asyncio.sleep(0.02)
        _emit(cdp, "Network.requestWillBeSent", {"requestId": "1"})
        _emit(cdp, "Network.requestWillBeSent", {"requestId": "1"})  # redirect hop
        _emit(cdp, "Network.loadingFinished", {"requestId": "1"})

    task = asyncio.create_task(_traffic())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _wait_network_idle(cdp, timeout=3.0, quiet_period=0.1)
    elapsed = loop.time() - started
    await task

    assert elapsed < 2.0  # returned on idle, not on timeout


@pytest.mark.asyncio
async def test_network_idle_times_out_on_never_ending_request():
    """A request that never completes (streaming/long-poll) bounds out at the
    timeout rather than hanging."""
    cdp = _net_client()
    _wait = _wait_network_idle(cdp, timeout=0.3, quiet_period=0.1)

    async def _traffic():
        await asyncio.sleep(0.01)
        _emit(cdp, "Network.requestWillBeSent", {"requestId": "stream"})

    task = asyncio.create_task(_traffic())
    await asyncio.wait_for(_wait, timeout=2.0)
    await task


@pytest.mark.asyncio
async def test_network_idle_returns_when_network_domain_unavailable():
    """Network.enable failing (engine without the domain) is a no-op, not an
    exception."""
    cdp = _net_client()
    cdp.send = AsyncMock(side_effect=CDPError("Network.enable", -1, "unsupported"))
    await _wait_network_idle(cdp, timeout=0.3, quiet_period=0.1)


@pytest.mark.asyncio
async def test_network_idle_failed_request_counts_as_finished():
    """loadingFailed must close the request; otherwise a blocked/aborted
    request stalls the wait for the full timeout."""
    cdp = _net_client()

    async def _traffic():
        await asyncio.sleep(0.02)
        _emit(cdp, "Network.requestWillBeSent", {"requestId": "1"})
        _emit(cdp, "Network.loadingFailed", {"requestId": "1"})

    task = asyncio.create_task(_traffic())
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _wait_network_idle(cdp, timeout=3.0, quiet_period=0.1)
    elapsed = loop.time() - started
    await task

    assert elapsed < 2.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
