"""Tests for the CDP WebSocket client."""

import asyncio
import json

import pytest

from ricibrowser.cdp_client import CDPClient, CDPError


@pytest.mark.asyncio
async def test_cdp_error_constructs():
    """CDPError carries method, code, and message."""
    err = CDPError("Page.navigate", -32000, "Cannot navigate to invalid URL")
    assert err.method == "Page.navigate"
    assert err.code == -32000
    assert "Page.navigate" in str(err)
    assert "Cannot navigate" in str(err)


@pytest.mark.asyncio
async def test_cdp_client_send_closed_raises():
    """Sending on a closed client raises CDPError."""
    client = CDPClient.__new__(CDPClient)
    client._closed = True
    client._ws = None
    client._pending = {}
    client._msg_id = 0
    with pytest.raises(CDPError) as exc_info:
        await client.send("Page.navigate", {"url": "https://example.com"})
    assert "closed" in exc_info.value.message.lower()


@pytest.mark.asyncio
async def test_cdp_event_registration():
    """Event callbacks can be registered without error."""
    client = CDPClient.__new__(CDPClient)
    client._closed = False
    client._event_handlers = {}

    called = []

    async def handler(params):
        called.append(params)

    await client.on_event("Page.loadEventFired", handler)
    assert "Page.loadEventFired" in client._event_handlers
    assert len(client._event_handlers["Page.loadEventFired"]) == 1


@pytest.mark.asyncio
async def test_wait_for_event_resolves_on_fire():
    """wait_for_event resolves when the event fires and cleans up its handler."""
    client = CDPClient.__new__(CDPClient)
    client._closed = False
    client._event_handlers = {}

    async def fire_soon():
        await asyncio.sleep(0.05)
        for cb in list(client._event_handlers.get("Page.loadEventFired", [])):
            cb({"timestamp": 123})

    task = asyncio.create_task(fire_soon())
    params = await client.wait_for_event("Page.loadEventFired", timeout=2.0)
    await task
    assert params == {"timestamp": 123}
    # Temporary handler must be removed after resolution.
    assert not client._event_handlers.get("Page.loadEventFired")


@pytest.mark.asyncio
async def test_wait_for_event_timeout_returns_none():
    """wait_for_event returns None on timeout and removes its handler."""
    client = CDPClient.__new__(CDPClient)
    client._closed = False
    client._event_handlers = {}

    params = await client.wait_for_event("Page.loadEventFired", timeout=0.1)
    assert params is None
    assert not client._event_handlers.get("Page.loadEventFired")


@pytest.mark.asyncio
async def test_wait_for_event_predicate_filters():
    """wait_for_event only resolves when the predicate matches."""
    client = CDPClient.__new__(CDPClient)
    client._closed = False
    client._event_handlers = {}

    async def fire():
        await asyncio.sleep(0.02)
        for cb in list(client._event_handlers.get("Page.frameStoppedLoading", [])):
            cb({"frameId": "OTHER"})
        await asyncio.sleep(0.02)
        for cb in list(client._event_handlers.get("Page.frameStoppedLoading", [])):
            cb({"frameId": "WANT"})

    task = asyncio.create_task(fire())
    params = await client.wait_for_event(
        "Page.frameStoppedLoading",
        predicate=lambda p: p.get("frameId") == "WANT",
        timeout=2.0,
    )
    await task
    assert params == {"frameId": "WANT"}
