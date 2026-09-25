"""Network capture — opt-in request/response logging via CDP Network domain.

OFF by default. ``Network.enable`` is a known detection vector (sites can
detect the CDP listener), so it's only turned on when the operator explicitly
requests debug network capture. This is the opposite of stealth-scraping tools
that leave everything on — we're a security tool that WANTS visibility, but
only when we ask for it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ricibrowser.cdp_client import CDPClient, CDPError

logger = logging.getLogger(__name__)


@dataclass
class Flow:
    """A single request/response flow captured by the network monitor."""

    request_id: str
    url: str = ""
    method: str = ""
    request_headers: dict = field(default_factory=dict)
    post_data: str | None = None
    response_status: int = 0
    response_headers: dict = field(default_factory=dict)
    response_body: str | None = None
    mime_type: str = ""
    resource_type: str = ""
    timestamp: float = 0.0
    duration: float = 0.0


class NetworkCapture:
    """Opt-in request/response capture for security debugging.

    Usage::

        net = NetworkCapture(enabled=True)
        await net.start(cdp_client)
        await session.navigate("https://api.target.com/users")
        flows = net.flows  # list of {request, response, timing}
        await net.stop(cdp_client)
    """

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._flows: list[Flow] = []
        self._pending: dict[str, Flow] = {}
        self._active = False
        # One Engine can host several concurrent Sessions, each with its own
        # CDPClient. CDP requestIds are per-connection and collide across
        # them, and a single shared _active flag meant one session's stop()
        # disabled every other session's capture. State is therefore keyed by
        # client, and the event handlers are bound to their own client so an
        # inbound event can be routed back to the right bucket.
        self._by_client: dict[int, dict] = {}
        self._MAX_PENDING = 1000  # cap to prevent unbounded memory growth

    @property
    def flows(self) -> list[Flow]:
        """Captured flows, newest last, across every active client."""
        merged: list[Flow] = []
        for state in self._by_client.values():
            merged.extend(state["flows"])
        return merged

    def _state(self, key: int) -> dict:
        return self._by_client.setdefault(key, {"flows": [], "pending": {}})

    async def start(self, cdp: CDPClient) -> None:
        """Enable network capture. This calls CDP Network.enable (detection vector!).

        Only call this when debug mode is explicitly requested.
        """
        if not self.enabled:
            return
        key = id(cdp)
        if key in self._by_client:
            return
        self._by_client[key] = {"flows": [], "pending": {}}
        self._active = True

        await cdp.on_event(
            "Network.requestWillBeSent",
            lambda p, k=key: self._on_request(k, p),
        )
        await cdp.on_event(
            "Network.responseReceived",
            lambda p, k=key: self._on_response(k, p),
        )
        await cdp.on_event(
            "Network.loadingFinished",
            lambda p, k=key: self._on_finished(k, p),
        )
        await cdp.on_event(
            "Network.loadingFailed",
            lambda p, k=key: self._on_failed(k, p),
        )

        try:
            await cdp.send("Network.enable")
            logger.info("Network capture enabled (debug mode)")
        except CDPError as exc:
            logger.warning("Network.enable failed: %s", exc)
            self._by_client.pop(key, None)
            self._active = bool(self._by_client)

    async def stop(self, cdp: CDPClient) -> None:
        """Disable network capture for *this client only*."""
        key = id(cdp)
        state = self._by_client.get(key)
        if state is None:
            return
        # Move any pending flows to completed (they never got a terminal event)
        state["flows"].extend(state["pending"].values())
        state["pending"].clear()
        self._by_client.pop(key, None)
        self._active = bool(self._by_client)
        try:
            await cdp.send("Network.disable")
        except CDPError:
            pass

    def clear(self) -> None:
        """Drop all captured flows across every client."""
        self._flows.clear()
        for state in self._by_client.values():
            state["flows"].clear()
            state["pending"].clear()

    async def _on_request(self, key: int, params: dict) -> None:
        """Handle Network.requestWillBeSent."""
        state = self._by_client.get(key)
        if state is None:
            return
        pending = state["pending"]
        # Evict oldest if at capacity (prevents unbounded growth from
        # flows that never get a terminal event — SSE, WebSocket upgrades).
        if len(pending) >= self._MAX_PENDING:
            oldest_key = next(iter(pending))
            state["flows"].append(pending.pop(oldest_key))
        flow = Flow(
            request_id=params.get("requestId", ""),
            url=params.get("request", {}).get("url", ""),
            method=params.get("request", {}).get("method", ""),
            request_headers=params.get("request", {}).get("headers", {}),
            post_data=params.get("request", {}).get("postData"),
            timestamp=params.get("timestamp", 0.0),
            resource_type=params.get("type", ""),
        )
        pending[flow.request_id] = flow

    async def _on_response(self, key: int, params: dict) -> None:
        """Handle Network.responseReceived."""
        state = self._by_client.get(key)
        if state is None:
            return
        flow = state["pending"].get(params.get("requestId", ""))
        if flow:
            resp = params.get("response", {})
            flow.response_status = resp.get("status", 0)
            flow.response_headers = resp.get("headers", {})
            flow.mime_type = resp.get("mimeType", "")
            flow.url = flow.url or resp.get("url", "")

    async def _on_finished(self, key: int, params: dict) -> None:
        """Handle Network.loadingFinished."""
        state = self._by_client.get(key)
        if state is None:
            return
        flow = state["pending"].pop(params.get("requestId", ""), None)
        if flow:
            flow.duration = params.get("timestamp", 0.0) - flow.timestamp
            state["flows"].append(flow)

    async def _on_failed(self, key: int, params: dict) -> None:
        """Handle Network.loadingFailed."""
        state = self._by_client.get(key)
        if state is None:
            return
        flow = state["pending"].pop(params.get("requestId", ""), None)
        if flow:
            state["flows"].append(flow)

    def to_dict(self) -> list[dict]:
        """Return captured flows as a list of dicts."""
        return [
            {
                "url": f.url,
                "method": f.method,
                "status": f.response_status,
                "mime_type": f.mime_type,
                "resource_type": f.resource_type,
                "request_headers": f.request_headers,
                "response_headers": f.response_headers,
                "post_data": f.post_data,
                "duration_ms": round(f.duration * 1000, 1),
            }
            for f in self.flows
        ]
