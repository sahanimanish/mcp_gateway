from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional
import httpx

logger = logging.getLogger("sse_client")

class SSEServerClient:
    def __init__(self, server_url: str, server_name: str, upstream_key: str = ""):
        self.server_url   = server_url.rstrip("/")
        self.server_name  = server_name
        self.upstream_key = upstream_key

        self._messages_url: Optional[str] = None
        self._pending: dict[int | str, asyncio.Future] = {}
        self._sse_task: Optional[asyncio.Task] = None
        self._ready    = asyncio.Event()
        self._sse_http  = httpx.AsyncClient(timeout=None)
        self._post_http = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20)
        )
        self._lock     = asyncio.Lock()
        self._next_id  = 10
        
        # FIXED: Use a set for concurrent handlers
        self._elicitation_handlers = set()

    async def connect(self):
        self._sse_task = asyncio.create_task(self._sse_reader())
        await asyncio.wait_for(self._ready.wait(), timeout=10.0)
        await self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"}
        }, id_=0)

    async def close(self):
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except asyncio.CancelledError:
                pass
        await self._sse_http.aclose()
        await self._post_http.aclose()

    async def _sse_reader(self):
        headers = {"Accept": "text/event-stream"}
        if self.upstream_key:
            headers["Authorization"] = f"Bearer {self.upstream_key}"

        try:
            async with self._sse_http.stream("GET", f"{self.server_url}/sse", headers=headers) as resp:
                resp.raise_for_status()
                event_type = None

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data = line[5:].strip()

                        if event_type == "endpoint":
                            self._messages_url = data
                            self._ready.set()

                        if event_type == "message":
                            try:
                                msg = json.loads(data)
                                msg_id = msg.get("id")

                                if msg.get("method") == "elicitation/create":
                                    asyncio.create_task(self._handle_elicitation(msg))
                                    continue

                                fut = self._pending.pop(msg_id, None)
                                if fut and not fut.done():
                                    fut.set_result(msg)
                            except json.JSONDecodeError:
                                pass
                        event_type = None
        except asyncio.CancelledError:
            pass
        except Exception as e:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(e)
            self._pending.clear()

    async def _handle_elicitation(self, msg: dict):
        elicit_id = msg.get("id")
        
        if not self._elicitation_handlers:
            await self.call_raw_response({
                "jsonrpc": "2.0", "id": elicit_id, "result": {"action": "cancel"}
            })
            return

        # Fire all active handlers
        for handler in list(self._elicitation_handlers):
            try:
                asyncio.create_task(handler(msg))
            except Exception:
                pass

    async def call_raw_response(self, response_body: dict):
        if not self._ready.is_set():
            raise RuntimeError("SSE client not connected")
        msg_url = (self.server_url + self._messages_url if self._messages_url.startswith("/") else self._messages_url)
        headers = {"Content-Type": "application/json"}
        if self.upstream_key:
            headers["Authorization"] = f"Bearer {self.upstream_key}"
        await self._post_http.post(msg_url, json=response_body, headers=headers)

    async def call(self, method: str, params: dict, id_: Optional[int] = None) -> dict:
        if not self._ready.is_set():
            raise RuntimeError("SSE client not connected")
        async with self._lock:
            if id_ is None:
                id_ = self._next_id
                self._next_id += 1

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[id_] = fut

        msg_url = (self.server_url + self._messages_url if self._messages_url.startswith("/") else self._messages_url)
        headers = {"Content-Type": "application/json"}
        if self.upstream_key:
            headers["Authorization"] = f"Bearer {self.upstream_key}"

        payload = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
        try:
            resp = await self._post_http.post(msg_url, json=payload, headers=headers)
            if resp.status_code not in (200, 202):
                self._pending.pop(id_, None)
                if not fut.done():
                    fut.set_exception(RuntimeError(f"POST {resp.status_code}"))
        except Exception as e:
            self._pending.pop(id_, None)
            if not fut.done():
                fut.set_exception(e)
        return await asyncio.wait_for(fut, timeout=20.0)

_pool: dict[str, SSEServerClient] = {}

async def get_or_connect(server_url: str, server_name: str, upstream_key: str = "") -> SSEServerClient:
    if server_url not in _pool:
        c = SSEServerClient(server_url, server_name, upstream_key)
        await c.connect()
        _pool[server_url] = c
    return _pool[server_url]

async def drop(server_url: str):
    c = _pool.pop(server_url, None)
    if c: await c.close()

async def close_all():
    for c in list(_pool.values()): await c.close()
    _pool.clear()