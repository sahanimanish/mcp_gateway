"""
sse_client.py — Persistent SSE connection manager for MCP SSE-transport servers.

SSE transport protocol:
  1. GET /sse                           → server opens SSE stream
                                          first event: 'endpoint'
                                          data: /messages/?session_id=<uuid>
  2. POST /messages/?session_id=<uuid>  → send JSON-RPC (returns 202 Accepted)
  3. SSE stream                         → JSON-RPC responses arrive as
                                          event: message
                                          data: {"jsonrpc":"2.0","id":N,"result":{...}}

The SSEServerClient keeps the SSE connection alive and uses asyncio.Event
to correlate POST requests with their SSE responses by JSON-RPC id.
"""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional
import httpx

logger = logging.getLogger("sse_client")


class SSEServerClient:
    """
    Manages a single persistent SSE connection to one upstream server.
    Thread-safe for concurrent tool calls via asyncio locks.
    """

    def __init__(self, server_url: str, server_name: str, upstream_key: str = ""):
        self.server_url   = server_url.rstrip("/")
        self.server_name  = server_name
        self.upstream_key = upstream_key

        self._messages_url: Optional[str] = None   # e.g. /messages/?session_id=xxx
        self._pending: dict[int | str, asyncio.Future] = {}
        self._sse_task: Optional[asyncio.Task] = None
        self._ready    = asyncio.Event()
        self._http     = httpx.AsyncClient(timeout=30.0)
        self._lock     = asyncio.Lock()
        self._next_id  = 10  # start at 10 to avoid clash with discovery ids

    # ── Lifecycle ────────────────────────────────────────────────

    async def connect(self):
        """Open the SSE stream, wait for the messages endpoint, then auto-initialize."""
        self._sse_task = asyncio.create_task(self._sse_reader())
        await asyncio.wait_for(self._ready.wait(), timeout=10.0)

        # MCP protocol requires initialize before any tool calls
        await self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"}
        }, id_=0)

    async def close(self):
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        await self._http.aclose()

    # ── Internal SSE reader ──────────────────────────────────────

    async def _sse_reader(self):
        headers = {"Accept": "text/event-stream"}
        if self.upstream_key:
            headers["Authorization"] = f"Bearer {self.upstream_key}"

        try:
            async with self._http.stream("GET", f"{self.server_url}/sse",
                                         headers=headers) as resp:
                resp.raise_for_status()
                event_type = None

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()

                        if event_type == "endpoint":
                            # e.g. /messages/?session_id=abc123
                            self._messages_url = data
                            self._ready.set()
                            logger.info(f"[{self.server_name}] SSE session URL: {data}")

                        elif event_type == "message":
                            try:
                                msg = json.loads(data)
                                msg_id = msg.get("id")
                                fut = self._pending.pop(msg_id, None)
                                if fut and not fut.done():
                                    fut.set_result(msg)
                                else:
                                    logger.debug(f"[{self.server_name}] unmatched response id={msg_id}")
                            except json.JSONDecodeError as e:
                                logger.warning(f"[{self.server_name}] SSE JSON parse error: {e}")

                        event_type = None

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.server_name}] SSE stream error: {e}")
            # Wake up any waiting callers with an error
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(e)
            self._pending.clear()

    # ── Public RPC call ──────────────────────────────────────────

    async def call(self, method: str, params: dict, id_: Optional[int] = None) -> dict:
        """
        Send a JSON-RPC request over SSE transport and wait for the response.
        The POST body goes to /messages?session_id=...,
        the response comes back on the SSE stream.
        """
        if not self._ready.is_set():
            raise RuntimeError("SSE client not connected")

        async with self._lock:
            if id_ is None:
                id_ = self._next_id
                self._next_id += 1

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[id_] = fut

        # Build messages URL
        if self._messages_url.startswith("/"):
            msg_url = self.server_url + self._messages_url
        else:
            msg_url = self._messages_url

        headers = {"Content-Type": "application/json"}
        if self.upstream_key:
            headers["Authorization"] = f"Bearer {self.upstream_key}"

        payload = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}

        try:
            resp = await self._http.post(msg_url, json=payload, headers=headers)
            if resp.status_code not in (200, 202):
                self._pending.pop(id_, None)
                if not fut.done():
                    fut.set_exception(RuntimeError(f"POST returned {resp.status_code}: {resp.text[:200]}"))
        except Exception as e:
            self._pending.pop(id_, None)
            if not fut.done():
                fut.set_exception(e)

        # Wait for SSE response (timeout 20s)
        return await asyncio.wait_for(fut, timeout=20.0)


# ── Global SSE client pool ───────────────────────────────────────
# Keyed by server_url → SSEServerClient

_pool: dict[str, SSEServerClient] = {}


async def get_or_connect(server_url: str, server_name: str, upstream_key: str = "") -> SSEServerClient:
    """Return a connected SSEServerClient, creating one if needed."""
    if server_url not in _pool:
        c = SSEServerClient(server_url, server_name, upstream_key)
        await c.connect()
        _pool[server_url] = c
        logger.info(f"[{server_name}] SSE client connected")
    return _pool[server_url]


async def drop(server_url: str):
    """Disconnect and remove a client from the pool."""
    c = _pool.pop(server_url, None)
    if c:
        await c.close()


async def close_all():
    for c in list(_pool.values()):
        await c.close()
    _pool.clear()
