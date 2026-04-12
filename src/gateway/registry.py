"""
registry.py — MCP discovery engine + in-memory tool registry

Supports all three MCP HTTP transports automatically:

  1. Streamable-HTTP (FastMCP default, modern)
       POST /mcp  with  Accept: application/json, text/event-stream
       Responses are SSE-wrapped JSON on same connection.
       Requires Mcp-Session-Id on calls after initialize.

  2. SSE transport (FastMCP legacy / explicit transport="sse")
       GET  /sse              → server streams events; first gives /messages?session_id=xxx
       POST /messages?session → send JSON-RPC (202 Accepted)
       Responses arrive on the open SSE stream as event:message / data:{...}

  3. Plain JSON-RPC (our example_upstream, custom FastAPI servers)
       POST /mcp  with  Content-Type: application/json
       Response body is plain JSON.

Detection order:
  - Try streamable-HTTP first (POST /mcp with Accept header)
  - If 404/connection error on /mcp, try SSE (GET /sse)
  - Plain JSON servers work via the same streamable-HTTP path (they ignore Accept)
"""

from __future__ import annotations
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import json
import httpx
import logging

logger = logging.getLogger("registry")

# ── In-memory routing table ──────────────────────────────────────
_tool_registry: dict[str, str] = {}   # prefixed_name → server_url
_tool_keys:     dict[str, str] = {}   # prefixed_name → upstream api key
_tool_transport: dict[str, str] = {}  # prefixed_name → "http" | "sse"


def get_server_url(tool_name: str) -> Optional[str]:
    return _tool_registry.get(tool_name)


def get_server_key(tool_name: str) -> Optional[str]:
    return _tool_keys.get(tool_name)


def get_transport(tool_name: str) -> str:
    """Return 'sse' or 'http' for a tool's server transport."""
    return _tool_transport.get(tool_name, "http")


def all_tools() -> list[str]:
    return list(_tool_registry.keys())


def register_tool(name: str, url: str, key: str = "", transport: str = "http"):
    _tool_registry[name] = url
    _tool_keys[name] = key
    _tool_transport[name] = transport


def unregister_server(server_name: str):
    prefix = server_name + "__"
    for k in [k for k in _tool_registry if k.startswith(prefix)]:
        del _tool_registry[k]
        _tool_keys.pop(k, None)
        _tool_transport.pop(k, None)


async def load_from_db(db):
    """Rebuild routing table from DB at startup."""
    from sqlalchemy import select
    from gateway.database import MCPServer, MCPTool

    servers = (await db.execute(select(MCPServer))).scalars().all()
    _tool_registry.clear()
    _tool_keys.clear()
    _tool_transport.clear()

    for srv in servers:
        tools = (await db.execute(
            select(MCPTool).where(MCPTool.server_id == srv.id)
        )).scalars().all()
        transport = srv.server_info.get("_transport", "http") if srv.server_info else "http"
        for t in tools:
            register_tool(t.name, srv.url, srv.upstream_key or "", transport)

    logger.info(f"Registry rebuilt: {len(_tool_registry)} tools from {len(servers)} servers")


# ── Discovery result dataclass ───────────────────────────────────

@dataclass
class DiscoveredTool:
    raw_name:     str
    prefixed_name: str
    description:  str = ""
    input_schema: dict = field(default_factory=dict)


@dataclass
class DiscoveredResource:
    uri:         str
    name:        str = ""
    description: str = ""
    mime_type:   str = ""


@dataclass
class DiscoveredPrompt:
    name:        str
    description: str = ""
    arguments:   list = field(default_factory=list)


@dataclass
class DiscoveryResult:
    success:          bool
    error:            str = ""
    transport:        str = "http"   # "http" | "sse"
    protocol_version: str = ""
    server_info:      dict = field(default_factory=dict)
    capabilities:     dict = field(default_factory=dict)
    tools:            list[DiscoveredTool]     = field(default_factory=list)
    resources:        list[DiscoveredResource] = field(default_factory=list)
    prompts:          list[DiscoveredPrompt]   = field(default_factory=list)


# ── Transport-aware response parser ─────────────────────────────

def _parse_response(resp: httpx.Response) -> dict:
    """
    Parse an MCP response regardless of transport encoding.

    Plain JSON servers  → Content-Type: application/json  → resp.json()
    FastMCP / streamable-HTTP → Content-Type: text/event-stream
        Body looks like:
            event: message\r\n
            data: {"jsonrpc":"2.0","id":1,"result":{...}}\r\n
            \r\n
    We extract the first `data:` line and parse it.
    """
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    return json.loads(payload)
        # No data line found — fall through to json attempt
    try:
        return resp.json()
    except Exception:
        return {}


# ── Core discovery ───────────────────────────────────────────────

async def discover(server_url: str, server_name: str, upstream_key: str = "") -> DiscoveryResult:
    """
    Auto-detect transport and run full MCP discovery.
    Tries streamable-HTTP first, falls back to SSE transport.
    """
    # Try streamable-HTTP first (works for both FastMCP http mode and plain JSON servers)
    result = await _discover_http(server_url, server_name, upstream_key)
    if result.success:
        return result

    http_error = result.error

    # If /mcp endpoint not found or returned 404, try SSE transport (/sse)
    logger.info(f"[{server_name}] HTTP transport failed ({http_error}), trying SSE...")
    result_sse = await _discover_sse(server_url, server_name, upstream_key)

    if result_sse.success:
        return result_sse

    # Both failed — return a combined error
    result.error = f"HTTP: {http_error} | SSE: {result_sse.error}"
    return result


async def _discover_http(server_url: str, server_name: str, upstream_key: str = "") -> DiscoveryResult:
    """
    Discovery via streamable-HTTP or plain JSON-RPC POST /mcp.
    FastMCP http mode:  Accept: application/json, text/event-stream  → SSE-wrapped JSON + Mcp-Session-Id
    Plain JSON servers: same POST, returns application/json directly.
    """
    url = server_url.rstrip("/") + "/mcp"

    base_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if upstream_key:
        base_headers["Authorization"] = f"Bearer {upstream_key}"

    result = DiscoveryResult(success=False, transport="http")

    async with httpx.AsyncClient(timeout=10.0) as client:

        # Step 1: initialize
        try:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"}
                }
            }, headers=base_headers)
            resp.raise_for_status()
            init_data = _parse_response(resp)
        except Exception as e:
            result.error = f"initialize failed: {e}"
            logger.debug(f"[{server_name}] HTTP discovery: {result.error}")
            return result

        init_result = init_data.get("result", {})
        result.protocol_version = init_result.get("protocolVersion", "")
        result.server_info      = init_result.get("serverInfo", {})
        result.capabilities     = init_result.get("capabilities", {})
        result.success          = True

        # Carry session ID for FastMCP
        session_id = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        call_headers = dict(base_headers)
        if session_id:
            call_headers["Mcp-Session-Id"] = session_id

        async def rpc(method: str, id_: int) -> dict:
            r = await client.post(url, json={
                "jsonrpc": "2.0", "id": id_, "method": method, "params": {}
            }, headers=call_headers)
            return _parse_response(r)

        # Steps 2–4
        result.tools     = await _fetch_tools(server_name, rpc, 2)
        result.resources = await _fetch_resources(server_name, rpc, 3)
        result.prompts   = await _fetch_prompts(server_name, rpc, 4)

    return result


async def _discover_sse(server_url: str, server_name: str, upstream_key: str = "") -> DiscoveryResult:
    """
    Discovery via SSE transport (FastMCP transport="sse").
    Opens GET /sse to get the messages URL, then sends JSON-RPC POSTs,
    collecting responses from the SSE stream.
    """
    from gateway.sse_client import SSEServerClient

    result = DiscoveryResult(success=False, transport="sse")
    client = SSEServerClient(server_url, server_name, upstream_key)

    try:
        await asyncio.wait_for(client.connect(), timeout=8.0)
    except Exception as e:
        result.error = f"SSE connect failed: {e}"
        logger.debug(f"[{server_name}] {result.error}")
        await client.close()
        return result

    try:
        # Step 1: initialize
        init_resp = await client.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"}
        }, id_=1)

        init_result = init_resp.get("result", {})
        result.protocol_version = init_result.get("protocolVersion", "")
        result.server_info      = init_result.get("serverInfo", {})
        result.capabilities     = init_result.get("capabilities", {})
        result.success          = True
        # Tag as SSE so proxy knows how to route tool calls
        result.server_info["_transport"] = "sse"

        # Steps 2–4
        async def rpc(method: str, id_: int) -> dict:
            return await client.call(method, {}, id_=id_)

        result.tools     = await _fetch_tools(server_name, rpc, 2)
        result.resources = await _fetch_resources(server_name, rpc, 3)
        result.prompts   = await _fetch_prompts(server_name, rpc, 4)

    except Exception as e:
        result.success = False
        result.error = f"SSE discovery error: {e}"
        logger.warning(f"[{server_name}] {result.error}")
    finally:
        await client.close()

    return result


# ── Shared fetch helpers ─────────────────────────────────────────

async def _fetch_tools(server_name: str, rpc, id_: int) -> list[DiscoveredTool]:
    try:
        data = await rpc("tools/list", id_)
        tools_raw = data.get("result", {}).get("tools", [])
        tools = []
        for t in tools_raw:
            raw = t.get("name", "") if isinstance(t, dict) else str(t)
            if raw:
                tools.append(DiscoveredTool(
                    raw_name      = raw,
                    prefixed_name = f"{server_name}__{raw}",
                    description   = t.get("description", "") if isinstance(t, dict) else "",
                    input_schema  = t.get("inputSchema", {}) if isinstance(t, dict) else {},
                ))
        logger.info(f"[{server_name}] discovered {len(tools)} tools")
        return tools
    except Exception as e:
        logger.warning(f"[{server_name}] tools/list failed: {e}")
        return []


async def _fetch_resources(server_name: str, rpc, id_: int) -> list[DiscoveredResource]:
    try:
        data = await rpc("resources/list", id_)
        if "error" in data:
            return []
        resources = []
        for r in data.get("result", {}).get("resources", []):
            resources.append(DiscoveredResource(
                uri         = r.get("uri", ""),
                name        = r.get("name", ""),
                description = r.get("description", ""),
                mime_type   = r.get("mimeType", ""),
            ))
        logger.info(f"[{server_name}] discovered {len(resources)} resources")
        return resources
    except Exception as e:
        logger.debug(f"[{server_name}] resources/list not supported: {e}")
        return []


async def _fetch_prompts(server_name: str, rpc, id_: int) -> list[DiscoveredPrompt]:
    try:
        data = await rpc("prompts/list", id_)
        if "error" in data:
            return []
        prompts = []
        for p in data.get("result", {}).get("prompts", []):
            prompts.append(DiscoveredPrompt(
                name        = p.get("name", ""),
                description = p.get("description", ""),
                arguments   = p.get("arguments", []),
            ))
        logger.info(f"[{server_name}] discovered {len(prompts)} prompts")
        return prompts
    except Exception as e:
        logger.debug(f"[{server_name}] prompts/list not supported: {e}")
        return []


async def save_discovery(db, server, result: DiscoveryResult):
    """
    Persist a DiscoveryResult into the DB and update in-memory registry.
    Clears existing tools/resources/prompts for this server first.
    """
    from sqlalchemy import delete
    from gateway.database import MCPTool, MCPResource, MCPPrompt, new_id
    from datetime import datetime, timezone

    # clear old data
    await db.execute(delete(MCPTool).where(MCPTool.server_id == server.id))
    await db.execute(delete(MCPResource).where(MCPResource.server_id == server.id))
    await db.execute(delete(MCPPrompt).where(MCPPrompt.server_id == server.id))
    unregister_server(server.name)

    # update server metadata — stash transport in server_info so it survives restarts
    server.protocol_version = result.protocol_version
    server.server_info      = {**(result.server_info or {}), "_transport": result.transport}
    server.capabilities     = result.capabilities
    server.status           = "online" if result.success else "offline"
    server.last_seen        = datetime.now(timezone.utc)

    # persist tools
    for t in result.tools:
        db.add(MCPTool(
            id           = new_id(),
            server_id    = server.id,
            name         = t.prefixed_name,
            raw_name     = t.raw_name,
            description  = t.description,
            input_schema = t.input_schema,
        ))
        register_tool(t.prefixed_name, server.url, server.upstream_key or "", result.transport)

    # persist resources
    for r in result.resources:
        db.add(MCPResource(
            id          = new_id(),
            server_id   = server.id,
            uri         = r.uri,
            name        = r.name,
            description = r.description,
            mime_type   = r.mime_type,
        ))

    # persist prompts
    for p in result.prompts:
        db.add(MCPPrompt(
            id          = new_id(),
            server_id   = server.id,
            name        = p.name,
            description = p.description,
            arguments   = p.arguments,
        ))

    await db.commit()
    logger.info(
        f"[{server.name}] saved ({result.transport}): {len(result.tools)} tools, "
        f"{len(result.resources)} resources, {len(result.prompts)} prompts"
    )