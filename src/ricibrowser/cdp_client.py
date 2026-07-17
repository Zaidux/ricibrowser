"""Minimal CDP (Chrome DevTools Protocol) WebSocket client.

Built from the public CDP specification at
chromedevtools.github.io/devtools-protocol/. No Playwright, Puppeteer, or
selenium dependency — just WebSocket + JSON-RPC.

This client implements:
  - WebSocket connection to a CDP endpoint (Chrome's /devtools/page/<id> or
    Lightpanda's root ws://host:port)
  - JSON-RPC command/response matching by id
  - CDP event subscription via callback
  - Target discovery via HTTP /json endpoint

It does NOT implement:
  - Runtime.enable on the main world (we use isolated worlds for JS eval)
  - Console.enable by default (off — detection vector)
  - Any browser-specific logic (that's in chrome_launcher.py / lightpanda.py)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import httpx
import websockets
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)

CDPEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class CDPError(Exception):
    """CDP protocol error (method returned an error response)."""

    def __init__(self, method: str, code: int, message: str, data: Any = None):
        self.method = method
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"CDP {method} failed ({code}): {message}")


class CDPClient:
    """Low-level CDP WebSocket client.

    Usage::

        client = await CDPClient.connect("ws://127.0.0.1:9222")
        result = await client.send("Page.navigate", {"url": "https://example.com"})
        await client.close()

    For Chrome (which uses target-level WS endpoints), use :meth:`connect_to_target`
    which discovers targets via the HTTP /json API first.
    """

    def __init__(self, ws: websockets.WebSocketClientProtocol):
        self._ws = ws
        self._msg_id: int = 0
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._event_handlers: dict[str, list[CDPEventCallback]] = {}
        self._recv_task: asyncio.Task | None = None
        self._closed = False

    @classmethod
    async def connect(cls, ws_url: str) -> "CDPClient":
        """Connect directly to a CDP WebSocket endpoint.

        For Lightpanda: ws_url = "ws://127.0.0.1:9222"
        For Chrome: ws_url = "ws://127.0.0.1:9223/devtools/page/<target_id>"
        """
        ws = await ws_connect(ws_url, max_size=50 * 1024 * 1024)  # 50 MB max
        client = cls(ws)
        client._recv_task = asyncio.create_task(client._recv_loop())
        return client

    @classmethod
    async def connect_to_target(
        cls,
        http_url: str,
        target_id: str | None = None,
    ) -> "CDPClient":
        """Connect to a Chrome CDP target discovered via the HTTP /json API.

        1. GET /json to list targets (or /json/list)
        2. Find a page-type target (or the one matching target_id)
        3. Connect to its webSocketDebuggerUrl
        """
        # Fetch target list
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(f"{http_url}/json")
            if resp.status_code != 200:
                raise CDPError("connect_to_target", resp.status_code, f"HTTP {resp.status_code}")
            targets = resp.json()

        # Find the target
        target = None
        if target_id:
            # When a specific target_id is requested, search exclusively for it.
            # Don't fall back to "first page target" — that could match an
            # unrelated tab.
            for t in targets:
                if t.get("id") == target_id:
                    target = t
                    break
            if target is None:
                raise CDPError("connect_to_target", -1,
                               f"Target {target_id} not found in CDP target list")
        else:
            for t in targets:
                if t.get("type") == "page":
                    target = t
                    break

        if target is None:
            # Create a new tab if none exists
            async with httpx.AsyncClient(timeout=10.0) as http:
                resp = await http.put(f"{http_url}/json/new")
                if resp.status_code in (200, 201):
                    target = resp.json()

        if target is None:
            raise CDPError("connect_to_target", -1, "No CDP page target found and could not create one")

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CDPError("connect_to_target", -1, "Target has no webSocketDebuggerUrl")

        return await cls.connect(ws_url)

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a CDP command and await the response.

        Args:
            method: CDP method name (e.g. "Page.navigate", "Runtime.evaluate")
            params: Method parameters (default empty dict)

        Returns:
            The "result" field from the CDP response.

        Raises:
            CDPError: If the CDP response contains an error.
        """
        if self._closed:
            raise CDPError(method, -1, "CDP client is closed")

        self._msg_id += 1
        msg_id = self._msg_id
        msg: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params

        future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._ws.send(json.dumps(msg))
        except Exception as exc:
            self._pending.pop(msg_id, None)
            raise CDPError(method, -1, f"WebSocket send failed: {exc}") from exc

        try:
            result = await asyncio.wait_for(future, timeout=120.0)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise CDPError(method, -1, f"Timeout waiting for response to {method}")

    async def on_event(self, event_name: str, callback: CDPEventCallback) -> None:
        """Register a callback for a CDP event (e.g. "Page.loadEventFired").

        Callbacks can be sync or async. Multiple callbacks per event are supported.
        """
        self._event_handlers.setdefault(event_name, []).append(callback)

    async def _recv_loop(self) -> None:
        """Background task that reads WebSocket messages and dispatches them."""
        try:
            async for raw in self._ws:
                if self._closed:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("CDP: received non-JSON message: %s", raw[:200])
                    continue

                # Response to a command (has "id")
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is None:
                        logger.warning("CDP: received response for unknown id %s", msg["id"])
                        continue
                    if "error" in msg:
                        err = msg["error"]
                        fut.set_exception(CDPError(
                            method="<response>",
                            code=err.get("code", -1),
                            message=err.get("message", "Unknown error"),
                            data=err.get("data"),
                        ))
                    else:
                        fut.set_result(msg.get("result", {}))
                    continue

                # Event (has "method", no "id")
                if "method" in msg:
                    event_name = msg["method"]
                    handlers = self._event_handlers.get(event_name, [])
                    for handler in handlers:
                        try:
                            result = handler(msg.get("params", {}))
                            if asyncio.iscoroutine(result):
                                asyncio.create_task(result)
                        except Exception:
                            logger.exception("CDP: event handler for %s raised", event_name)
        except websockets.ConnectionClosed:
            pass
        except Exception:
            logger.exception("CDP: recv loop crashed")
        finally:
            # Mark as closed so send() fast-fails and callers reconnect
            # instead of queuing futures that will never resolve.
            self._closed = True
            # Reject any pending futures
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(CDPError("<recv-closed>", -1, "Connection closed"))
            self._pending.clear()

    async def close(self) -> None:
        """Close the WebSocket connection and stop the recv task."""
        if self._closed:
            return
        self._closed = True
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await asyncio.wait_for(self._recv_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        try:
            await self._ws.close()
        except Exception:
            pass

    @staticmethod
    async def discover_targets(http_url: str) -> list[dict[str, Any]]:
        """List CDP targets via the HTTP /json endpoint.

        Returns a list of target dicts with keys: id, type, title, url,
        webSocketDebuggerUrl, etc.
        """
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.get(f"{http_url}/json")
            if resp.status_code == 200:
                return resp.json()
        return []
