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
        self.flows: list[Flow] = []
        self._pending: dict[str, Flow] = {}
        self._active = False
        self._MAX_PENDING = 1000  # cap to prevent unbounded memory growth

    async def start(self, cdp: CDPClient) -> None:
        """Enable network capture. This calls CDP Network.enable (detection vector!).

        Only call this when debug mode is explicitly requested.
        """
        if not self.enabled or self._active:
            return
        self._active = True
        self._pending.clear()
        self.flows.clear()

        await cdp.on_event("Network.requestWillBeSent", self._on_request)
        await cdp.on_event("Network.responseReceived", self._on_response)
        await cdp.on_event("Network.loadingFinished", self._on_finished)
        await cdp.on_event("Network.loadingFailed", self._on_failed)

        try:
            await cdp.send("Network.enable")
            logger.info("Network capture enabled (debug mode)")
        except CDPError as exc:
            logger.warning("Network.enable failed: %s", exc)
            self._active = False

    async def stop(self, cdp: CDPClient) -> None:
        """Disable network capture."""
        if not self._active:
            return
        self._active = False
        # Move any pending flows to completed (they never got a terminal event)
        for flow in self._pending.values():
            self.flows.append(flow)
        self._pending.clear()
        try:
            await cdp.send("Network.disable")
        except CDPError:
            pass

    async def _on_request(self, params: dict) -> None:
        """Handle Network.requestWillBeSent."""
        if not self._active:
            return
        # Evict oldest if at capacity (prevents unbounded growth from
        # flows that never get a terminal event — SSE, WebSocket upgrades).
        if len(self._pending) >= self._MAX_PENDING:
            oldest_key = next(iter(self._pending))
            self.flows.append(self._pending.pop(oldest_key))
        flow = Flow(
            request_id=params.get("requestId", ""),
            url=params.get("request", {}).get("url", ""),
            method=params.get("request", {}).get("method", ""),
            request_headers=params.get("request", {}).get("headers", {}),
            post_data=params.get("request", {}).get("postData"),
            timestamp=params.get("timestamp", 0.0),
            resource_type=params.get("type", ""),
        )
        self._pending[flow.request_id] = flow

    async def _on_response(self, params: dict) -> None:
        """Handle Network.responseReceived."""
        if not self._active:
            return
        req_id = params.get("requestId", "")
        flow = self._pending.get(req_id)
        if flow:
            resp = params.get("response", {})
            flow.response_status = resp.get("status", 0)
            flow.response_headers = resp.get("headers", {})
            flow.mime_type = resp.get("mimeType", "")
            flow.url = flow.url or resp.get("url", "")

    async def _on_finished(self, params: dict) -> None:
        """Handle Network.loadingFinished."""
        req_id = params.get("requestId", "")
        flow = self._pending.pop(req_id, None)
        if flow:
            flow.duration = params.get("timestamp", 0.0) - flow.timestamp
            self.flows.append(flow)

    async def _on_failed(self, params: dict) -> None:
        """Handle Network.loadingFailed."""
        req_id = params.get("requestId", "")
        flow = self._pending.pop(req_id, None)
        if flow:
            self.flows.append(flow)

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
