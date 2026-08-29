from __future__ import annotations

import pytest
from ricibrowser.session import Session


class ThrowingCdp:
    """CDP that succeeds on Runtime.evaluate but reports a JS exception."""

    def __init__(self, *, throw: bool = False):
        self._throw = throw
        self._event_handlers = {}

    async def send(self, method, params=None):
        if method == "Runtime.evaluate":
            if self._throw:
                return {
                    "result": {"type": "object", "subtype": "error", "value": None},
                    "exceptionDetails": {
                        "text": "Uncaught",
                        "exception": {
                            "description": "TypeError: Cannot read properties of null (reading 'payload')\n    at <anonymous>:2:31",
                        },
                    },
                }
            return {"result": {"type": "string", "value": "ok"}}
        raise RuntimeError(method)


@pytest.mark.asyncio
async def test_evaluate_returns_none_and_records_exception():
    session = Session(ThrowingCdp(throw=True))
    value = await session.evaluate("nullRef.payload")
    assert value is None
    assert session.last_eval_error is not None
    assert "TypeError" in session.last_eval_error
    assert "payload" in session.last_eval_error


@pytest.mark.asyncio
async def test_evaluate_clears_error_on_success():
    session = Session(ThrowingCdp(throw=False))
    value = await session.evaluate("'fine'")
    assert value == "ok"
    assert session.last_eval_error is None


@pytest.mark.asyncio
async def test_success_after_exception_resets_state():
    class AlternatingCdp(ThrowingCdp):
        def __init__(self):
            super().__init__(throw=True)
            self.calls = 0

        async def send(self, method, params=None):
            if method == "Runtime.evaluate":
                self.calls += 1
                self._throw = self.calls == 1
            return await super().send(method, params)

    session = Session(AlternatingCdp())
    assert await session.evaluate("boom") is None
    assert session.last_eval_error is not None
    assert await session.evaluate("fine") == "ok"
    assert session.last_eval_error is None
