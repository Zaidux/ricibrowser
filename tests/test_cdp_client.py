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
