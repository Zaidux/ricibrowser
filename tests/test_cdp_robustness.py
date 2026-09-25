"""Regression tests for three CDP robustness defects found in audit.

1. A cancelled command's late reply killed the whole recv loop
   (InvalidStateError on a dead future), failing every concurrent command.
2. close() returned early when the recv loop had already set _closed, so the
   WebSocket (and the attached Chrome tab) leaked for the life of the process.
3. _wait_for_url_stability busy-looped: `continue` skipped the sleep, spinning
   thousands of Runtime.evaluate round-trips per second for the full deadline.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from ricibrowser.cdp_client import CDPClient, CDPError  # noqa: E402
from ricibrowser.session import Session  # noqa: E402


class _FakeWS:
    """Minimal websockets stand-in driven by the test."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False
        self._inbox: asyncio.Queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def reply(self, msg_id: int, result: dict | None = None, error: dict | None = None) -> None:
        payload: dict = {"id": msg_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result or {}
        self._inbox.put_nowait(payload)


def _client(ws: _FakeWS) -> CDPClient:
    c = CDPClient.__new__(CDPClient)
    c._ws = ws
    c._pending = {}
    c._event_handlers = {}
    c._msg_id = 0
    c._closed = False
    c._recv_task = None
    c._command_timeout = 5.0
    return c


# ── 1. late reply for a cancelled command must not kill the recv loop ──


@pytest.mark.asyncio
async def test_cancelled_command_late_reply_does_not_kill_recv_loop():
    ws = _FakeWS()
    c = _client(ws)
    c._recv_task = asyncio.create_task(c._recv_loop())
    await asyncio.sleep(0)

    # A healthy command in flight when the other one gets cancelled.
    survivor = asyncio.create_task(c.send("Runtime.evaluate"))
    victim = asyncio.create_task(c.send("Page.navigate"))
    # Let both tasks run up to their first await on the socket send.
    for _ in range(10):
        await asyncio.sleep(0)
        if len(ws.sent) >= 2:
            break
    assert len(ws.sent) == 2, ws.sent

    victim_id = ws.sent[1]["id"]
    surv_id = ws.sent[0]["id"]

    # Cancel the victim the way asyncio.wait_for(timeout=...) does.
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim
    await asyncio.sleep(0)

    # Its reply arrives late — previously InvalidStateError, which escaped to
    # the recv loop's outer except and killed the connection.
    ws.reply(victim_id, {"frameId": "F"})
    await asyncio.sleep(0.05)

    # The survivor must still be alive and resolvable.
    assert not survivor.done(), "recv loop died from a late reply"
    assert not c._closed
    ws.reply(surv_id, {"result": {"type": "number", "value": 42}})
    assert await asyncio.wait_for(survivor, timeout=2) == {"result": {"type": "number", "value": 42}}

    c._recv_task.cancel()


@pytest.mark.asyncio
async def test_recv_loop_survives_settled_future_reply():
    """A reply for an already-settled future must be ignored, not fatal."""
    ws = _FakeWS()
    c = _client(ws)
    c._recv_task = asyncio.create_task(c._recv_loop())
    await asyncio.sleep(0)

    fut = asyncio.get_running_loop().create_future()
    fut.cancel()
    c._pending[99] = fut

    ws.reply(99, {"x": 1})
    await asyncio.sleep(0.05)

    assert not c._closed
    assert c._recv_task is not None and not c._recv_task.done()
    c._recv_task.cancel()


# ── 2. close() must close the transport even if the recv loop already died ──


@pytest.mark.asyncio
async def test_close_closes_ws_even_when_recv_loop_already_exited():
    ws = _FakeWS()
    c = _client(ws)
    # Simulate the recv loop having died and set the flag in its finally.
    c._closed = True
    c._recv_task = None

    await c.close()
    assert ws.closed is True, "socket leaked: close() returned early on _closed"


@pytest.mark.asyncio
async def test_session_close_closes_even_when_client_flagged_closed():
    ws = _FakeWS()
    c = _client(ws)
    c._closed = True
    client = type("C", (), {"_closed": False, "_event_handlers": {}, "_pending": {}})()
    client.send = lambda *a, **k: None
    sess = Session(client)
    sess._cdp = c

    await sess.close()
    assert ws.closed is True


@pytest.mark.asyncio
async def test_close_is_idempotent():
    ws = _FakeWS()
    c = _client(ws)
    await c.close()
    await c.close()
    assert ws.closed is True


# ── 3. URL-stability polling must not busy-loop ──


@pytest.mark.asyncio
async def test_wait_for_url_stability_does_not_spin_when_context_gone():
    """A destroyed execution context (the normal post-click state) must
    still be polled at ~2 Hz, not thousands of times per second."""
    calls = 0

    async def evaluate(_expr: str):
        nonlocal calls
        calls += 1
        return None          # context destroyed — the case that used to spin

    client = type("C", (), {"_closed": False, "_event_handlers": {}, "_pending": {}})()
    client.send = lambda *a, **k: None
    sess = Session(client)
    sess.evaluate = evaluate
    sess._url_stability_timeout = 1.0

    t0 = time.monotonic()
    await sess._wait_for_url_stability("https://old.test/")
    elapsed = time.monotonic() - t0

    assert elapsed >= 0.9, "returned before the deadline"
    # With a 0.5s poll over 1s this must be ~2-3, never thousands.
    assert calls <= 6, f"busy-looped: {calls} evaluate round-trips in {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_wait_for_url_stability_detects_change():
    seq = ["https://new.test/", "https://new.test/"]
    client = type("C", (), {"_closed": False, "_event_handlers": {}, "_pending": {}})()
    client.send = lambda *a, **k: None
    sess = Session(client)
    it = iter(seq)

    async def evaluate(_expr: str):
        try:
            return next(it)
        except StopIteration:
            return "https://new.test/"

    sess.evaluate = evaluate
    sess._url_stability_timeout = 5.0
    await sess._wait_for_url_stability("https://old.test/")
    assert sess._current_url == "https://new.test/"
