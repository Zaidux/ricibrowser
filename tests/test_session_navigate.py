"""Tests for Session.navigate — load-event gating that fixes flaky/empty pages.

Regression coverage for the race where Page.navigate was followed by an
immediate document.readyState poll: a fresh about:blank tab (or a prior
fully-loaded page) answered readyState 'complete' before Chrome committed the
new navigation, so a blank/stale page was captured. navigate() now awaits the
real Page.loadEventFired (registered BEFORE Page.navigate) before capturing.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ricibrowser.session import Session


def _make_client():
    client = MagicMock()
    client._closed = False
    client._event_handlers = {}
    client._pending = {}
    return client


@pytest.mark.asyncio
async def test_navigate_waits_for_load_event_before_capture():
    """navigate registers a loadEventFired waiter before Page.navigate and
    only captures the page after the load event fires."""
    client = _make_client()

    order: list[str] = []
    # The page content the isolated-world snapshot returns AFTER load.
    snapshot = {
        "title": "loaded",
        "html": "<html><body>hi</body></html>",
        "text": "hi",
        "url": "http://t/",
    }

    async def fake_send(method, params=None):
        if method == "Page.navigate":
            order.append("navigate")
            # A real Chrome fires loadEventFired shortly after navigate. Here
            # we fire it on the next loop tick so navigate() must be awaiting it.
            async def _fire():
                await asyncio.sleep(0.02)
                order.append("load_fired")
                for cb in list(client._event_handlers.get("Page.loadEventFired", [])):
                    cb({"timestamp": 1})
            asyncio.create_task(_fire())
            return {"frameId": "F1"}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            expr = (params or {}).get("expression", "")
            if "document.title" in expr and "html" in expr:
                order.append("capture")
                return {"result": {"value": dict(snapshot)}}
            if expr == "location.href":
                return {"result": {"value": "http://t/"}}
            if "readyState" in expr:
                return {"result": {"value": "complete"}}
            return {"result": {"value": ""}}
        if method == "Network.getCookies":
            return {"cookies": []}
        return {}

    client.send = AsyncMock(side_effect=fake_send)

    session = Session(client, "cdp_chrome")
    session._url_stability_timeout = 0.0  # skip the SSO redirect poll
    session._nav_timeout = 2.0

    page = await session.navigate("http://t/", wait_until="load")

    # The load event must have fired before the content capture ran.
    assert "navigate" in order and "load_fired" in order and "capture" in order
    assert order.index("load_fired") < order.index("capture"), order
    assert page.title == "loaded"
    assert page.text == "hi"


@pytest.mark.asyncio
async def test_navigate_reports_hard_error_text():
    """A Page.navigate errorText (DNS failure, connection refused) is treated
    as a real failure — we don't hang waiting for a load event that never comes."""
    client = _make_client()

    async def fake_send(method, params=None):
        if method == "Page.navigate":
            return {"frameId": "F1", "errorText": "net::ERR_NAME_NOT_RESOLVED"}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            expr = (params or {}).get("expression", "")
            if "document.title" in expr and "html" in expr:
                return {"result": {"value": {"title": "", "html": "", "text": "", "url": "http://bad/"}}}
            return {"result": {"value": ""}}
        if method == "Network.getCookies":
            return {"cookies": []}
        return {}

    client.send = AsyncMock(side_effect=fake_send)

    session = Session(client, "cdp_chrome")
    session._url_stability_timeout = 0.0
    session._nav_timeout = 0.5  # must NOT wait the full timeout on hard error

    # Should return promptly (well under nav_timeout) with status_code 0.
    page = await asyncio.wait_for(session.navigate("http://bad/", wait_until="load"), timeout=1.0)
    assert page.status_code == 0


@pytest.mark.asyncio
async def test_navigate_falls_back_when_load_event_never_fires():
    """If the load event never arrives, navigate falls back to the readyState
    poll after nav_timeout instead of hanging forever."""
    client = _make_client()

    async def fake_send(method, params=None):
        if method == "Page.navigate":
            return {"frameId": "F1"}  # no errorText, but we never fire load
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 7}
        if method == "Runtime.evaluate":
            expr = (params or {}).get("expression", "")
            if "document.title" in expr and "html" in expr:
                return {"result": {"value": {"title": "late", "html": "<html></html>", "text": "late", "url": "http://t/"}}}
            if expr == "location.href":
                return {"result": {"value": "http://t/"}}
            if "readyState" in expr:
                return {"result": {"value": "complete"}}
            return {"result": {"value": ""}}
        if method == "Network.getCookies":
            return {"cookies": []}
        return {}

    client.send = AsyncMock(side_effect=fake_send)

    session = Session(client, "cdp_chrome")
    session._url_stability_timeout = 0.0
    session._nav_timeout = 0.2  # short fallback

    page = await asyncio.wait_for(session.navigate("http://t/", wait_until="load"), timeout=2.0)
    assert page.title == "late"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
