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
    session_id: str = ""
    target_id: str = ""
    failure_reason: str | None = None


class NetworkCapture:
    """Opt-in request/response capture for security debugging.

    Usage::

        net = NetworkCapture(enabled=True)
        await net.start(cdp_client)
        await session.navigate("https://api.target.com/users")
        flows = net.flows  # list of {request, response, timing}
        await net.stop(cdp_client)
    """

    def __init__(self, enabled: bool = False, max_archived_flows: int = 5000):
        self.enabled = enabled
        # Completed client buckets are moved here by stop().  Keeping this
        # distinct from active state makes ``await fast_browse(); net.to_dict()``
        # useful instead of erasing its evidence during automatic cleanup.
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
        self._max_archived_flows = max(1, int(max_archived_flows))
        self._dropped_flows = 0

    @property
    def flows(self) -> list[Flow]:
        """Current capture view, preserving the historical active semantics.

        While clients are active this is their live view; after the final
        client stops it becomes the retained archive.  ``to_dict()`` always
        exports both retained and active evidence.
        """
        merged: list[Flow] = [] if self._by_client else list(self._flows)
        for state in self._by_client.values():
            merged.extend(state["flows"])
        return merged

    def _all_flows(self) -> list[Flow]:
        merged: list[Flow] = list(self._flows)
        for state in self._by_client.values():
            merged.extend(state["flows"])
        return merged

    def _state(self, key: int) -> dict:
        return self._by_client.setdefault(key, {"flows": [], "pending": {}})

    async def start(
        self, cdp: CDPClient, *, session_id: str = "", target_id: str = "",
    ) -> None:
        """Enable network capture. This calls CDP Network.enable (detection vector!).

        Only call this when debug mode is explicitly requested.
        """
        if not self.enabled:
            return
        key = id(cdp)
        if key in self._by_client:
            return
        handlers = {
            "Network.requestWillBeSent": lambda p, k=key: self._on_request(k, p),
            "Network.responseReceived": lambda p, k=key: self._on_response(k, p),
            "Network.loadingFinished": lambda p, k=key: self._on_finished(k, p),
            "Network.loadingFailed": lambda p, k=key: self._on_failed(k, p),
        }
        self._by_client[key] = {
            "flows": [], "pending": {}, "handlers": handlers,
            "session_id": session_id, "target_id": target_id,
        }
        self._active = True

        for name, callback in handlers.items():
            await cdp.on_event(name, callback)

        try:
            await cdp.send("Network.enable")
            logger.info("Network capture enabled (debug mode)")
        except CDPError as exc:
            logger.warning("Network.enable failed: %s", exc)
            state = self._by_client.pop(key, None)
            self._unregister_handlers(cdp, state)
            self._active = bool(self._by_client)

    async def stop(self, cdp: CDPClient) -> None:
        """Disable network capture for *this client only*."""
        key = id(cdp)
        state = self._by_client.get(key)
        if state is None:
            return
        # Move any pending flows to completed (they never got a terminal event),
        # then archive the whole client bucket before unregistering it.
        for flow in state["pending"].values():
            if flow.failure_reason is None:
                flow.failure_reason = "capture_stopped_before_terminal_event"
        state["flows"].extend(state["pending"].values())
        state["pending"].clear()
        self._by_client.pop(key, None)
        self._archive(state["flows"])
        self._unregister_handlers(cdp, state)
        self._active = bool(self._by_client)
        try:
            await cdp.send("Network.disable")
        except (CDPError, AttributeError):
            pass

    def clear(self) -> None:
        """Drop all captured flows across every client."""
        self._flows.clear()
        self._dropped_flows = 0
        for state in self._by_client.values():
            state["flows"].clear()
            state["pending"].clear()

    def _archive(self, flows: list[Flow]) -> None:
        if not flows:
            return
        self._flows.extend(flows)
        excess = len(self._flows) - self._max_archived_flows
        if excess > 0:
            del self._flows[:excess]
            self._dropped_flows += excess

    def _append_completed(self, state: dict, flow: Flow) -> None:
        state["flows"].append(flow)
        # Active buckets are bounded too; move oldest records into the durable
        # archive so long-lived sessions cannot grow without limit.
        if len(state["flows"]) > self._max_archived_flows:
            overflow = len(state["flows"]) - self._max_archived_flows
            self._archive(state["flows"][:overflow])
            del state["flows"][:overflow]

    @staticmethod
    def _unregister_handlers(cdp: CDPClient, state: dict | None) -> None:
        if not state:
            return
        registered = getattr(cdp, "_event_handlers", None)
        if not isinstance(registered, dict):
            # Some test/fallback clients keep handlers on another attribute;
            # closing their transport still retires those callbacks.
            return
        for name, callback in state.get("handlers", {}).items():
            callbacks = registered.get(name)
            if callbacks and callback in callbacks:
                callbacks.remove(callback)

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
            evicted = pending.pop(oldest_key)
            evicted.failure_reason = "pending_capacity_exceeded"
            self._append_completed(state, evicted)
        flow = Flow(
            request_id=params.get("requestId", ""),
            url=params.get("request", {}).get("url", ""),
            method=params.get("request", {}).get("method", ""),
            request_headers=params.get("request", {}).get("headers", {}),
            post_data=params.get("request", {}).get("postData"),
            timestamp=params.get("timestamp", 0.0),
            resource_type=params.get("type", ""),
            session_id=state.get("session_id", ""),
            target_id=state.get("target_id", ""),
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
            self._append_completed(state, flow)

    async def _on_failed(self, key: int, params: dict) -> None:
        """Handle Network.loadingFailed."""
        state = self._by_client.get(key)
        if state is None:
            return
        flow = state["pending"].pop(params.get("requestId", ""), None)
        if flow:
            flow.failure_reason = str(params.get("errorText") or "loading_failed")
            self._append_completed(state, flow)

    @property
    def capabilities(self) -> dict[str, Any]:
        """Serializable capture capabilities (metadata-only in this release)."""
        return {
            "enabled": bool(self.enabled),
            "request_metadata": bool(self.enabled),
            "response_metadata": bool(self.enabled),
            "response_bodies": False,
            "bounded_retention": True,
            "max_archived_flows": self._max_archived_flows,
        }

    @property
    def stats(self) -> dict[str, int]:
        return {
            "retained_flows": len(self._all_flows()),
            "active_clients": len(self._by_client),
            "dropped_flows": self._dropped_flows,
        }

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
                "session_id": f.session_id or None,
                "target_id": f.target_id or None,
                "failure_reason": f.failure_reason,
            }
            for f in self._all_flows()
        ]
