from __future__ import annotations

import pytest
from ricibrowser.session import Session


class RecordingCdp:
    """Fake CDP that records Runtime.evaluate/Input.dispatch params."""

    def __init__(self):
        self._event_handlers = {}
        self.evaluate_params: list[dict] = []
        self.input_events: list[dict] = []
        self.next_rect = {"x": 100.0, "y": 50.0}

    async def send(self, method, params=None):
        if method == "Runtime.evaluate":
            self.evaluate_params.append(dict(params or {}))
            expression = (params or {}).get("expression", "")
            if "r.left + r.width / 2" in expression:
                if self.next_rect is None:
                    return {"result": {"value": None}}
                return {"result": {"value": dict(self.next_rect)}}
            if "location.href" in expression:
                return {"result": {"value": "https://example.test/page"}}
            return {"result": {"value": True}}
        if method == "Input.dispatchMouseEvent":
            self.input_events.append(dict(params or {}))
            return {}
        raise RuntimeError(method)


@pytest.mark.asyncio
async def test_evaluate_awaits_promises():
    cdp = RecordingCdp()
    session = Session(cdp)
    await session.evaluate_value("fetch('/x').then(r => r.text())")
    assert cdp.evaluate_params[0]["awaitPromise"] is True
    assert cdp.evaluate_params[0]["returnByValue"] is True


@pytest.mark.asyncio
async def test_click_dispatches_trusted_mouse_events():
    cdp = RecordingCdp()
    session = Session(cdp)
    ok = await session.click("#submit", timeout=0.1, wait_for_navigation=False)
    assert ok is True
    types = [event["type"] for event in cdp.input_events]
    assert types == ["mousePressed", "mouseReleased"]
    assert cdp.input_events[0]["x"] == 100.0
    assert cdp.input_events[0]["y"] == 50.0
    assert cdp.input_events[0]["button"] == "left"


@pytest.mark.asyncio
async def test_click_falls_back_to_synthetic_when_rect_unavailable():
    cdp = RecordingCdp()
    cdp.next_rect = None
    session = Session(cdp)
    ok = await session.click("#hidden", timeout=0.1, wait_for_navigation=False)
    assert ok is True
    assert cdp.input_events == []


@pytest.mark.asyncio
async def test_click_falls_back_when_input_dispatch_fails():
    class FailingInputCdp(RecordingCdp):
        async def send(self, method, params=None):
            if method == "Input.dispatchMouseEvent":
                raise RuntimeError("Input domain unavailable")
            return await super().send(method, params)

    cdp = FailingInputCdp()
    session = Session(cdp)
    ok = await session.click("#submit", timeout=0.1, wait_for_navigation=False)
    assert ok is True
    # Synthetic fallback ran via Runtime.evaluate
    assert len(cdp.evaluate_params) >= 2
