"""
registry.py — MCP discovery engine + in-memory tool registry

Discovery flow on server registration:
  1. POST initialize      → confirm protocol, get server_info + capabilities
  2. POST tools/list      → full tool objects (name, description, inputSchema)
  3. POST resources/list  → resource objects (uri, name, mimeType)  — if supported
  4. POST prompts/list    → prompt objects (name, description, arguments) — if supported

The in-memory registry maps prefixed tool names → server URL for fast routing.
"""

from __future__ import annotations
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
import httpx
import logging

logger = logging.getLogger("registry")

# ── In-memory routing table ──────────────────────────────────────
_tool_registry: dict[str, str] = {}   # prefixed_name → server_url
_tool_keys:     dict[str, str] = {}   # prefixed_name → upstream api key


def get_server_url(tool_name: str) -> Optional[str]:
    return _tool_registry.get(tool_name)


def get_server_key(tool_name: str) -> Optional[str]:
    return _tool_keys.get(tool_name)


def all_tools() -> list[str]:
    return list(_tool_registry.keys())


def register_tool(name: str, url: str, key: str = ""):
    _tool_registry[name] = url
    _tool_keys[name] = key


def unregister_server(server_name: str):
    prefix = server_name + "__"
    for k in [k for k in _tool_registry if k.startswith(prefix)]:
        del _tool_registry[k]
        _tool_keys.pop(k, None)


async def load_from_db(db):
    """Rebuild routing table from DB at startup."""
    from sqlalchemy import select
    from gateway.database import MCPServer, MCPTool

    servers = (await db.execute(select(MCPServer))).scalars().all()
    _tool_registry.clear()
    _tool_keys.clear()

    for srv in servers:
        tools = (await db.execute(
            select(MCPTool).where(MCPTool.server_id == srv.id)
        )).scalars().all()
        for t in tools:
            register_tool(t.name, srv.url, srv.upstream_key or "")

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
    protocol_version: str = ""
    server_info:      dict = field(default_factory=dict)
    capabilities:     dict = field(default_factory=dict)
    tools:            list[DiscoveredTool]     = field(default_factory=list)
    resources:        list[DiscoveredResource] = field(default_factory=list)
    prompts:          list[DiscoveredPrompt]   = field(default_factory=list)


# ── Core discovery ───────────────────────────────────────────────

async def discover(server_url: str, server_name: str, upstream_key: str = "") -> DiscoveryResult:
    """
    Full MCP handshake against server_url/mcp.
    Returns DiscoveryResult with everything the server exposes.
    """
    url     = server_url.rstrip("/") + "/mcp"
    headers = {"Content-Type": "application/json"}
    if upstream_key:
        headers["Authorization"] = f"Bearer {upstream_key}"

    result = DiscoveryResult(success=False)

    async with httpx.AsyncClient(timeout=10.0) as client:

        # ── Step 1: initialize ──────────────────────────────────
        try:
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"}
                }
            }, headers=headers)
            resp.raise_for_status()
            init_data = resp.json()
        except Exception as e:
            result.error = f"initialize failed: {e}"
            logger.warning(f"[{server_name}] {result.error}")
            return result

        init_result = init_data.get("result", {})
        result.protocol_version = init_result.get("protocolVersion", "")
        result.server_info      = init_result.get("serverInfo", {})
        result.capabilities     = init_result.get("capabilities", {})
        result.success          = True

        caps = result.capabilities

        # ── Step 2: tools/list ──────────────────────────────────
        if "tools" in caps or True:   # always attempt — many servers omit capabilities
            try:
                resp = await client.post(url, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
                }, headers=headers)
                tools_raw = resp.json().get("result", {}).get("tools", [])
                for t in tools_raw:
                    raw = t.get("name", "") if isinstance(t, dict) else str(t)
                    if not raw:
                        continue
                    result.tools.append(DiscoveredTool(
                        raw_name      = raw,
                        prefixed_name = f"{server_name}__{raw}",
                        description   = t.get("description", "") if isinstance(t, dict) else "",
                        input_schema  = t.get("inputSchema", {}) if isinstance(t, dict) else {},
                    ))
                logger.info(f"[{server_name}] discovered {len(result.tools)} tools")
            except Exception as e:
                logger.warning(f"[{server_name}] tools/list failed: {e}")

        # ── Step 3: resources/list ──────────────────────────────
        if "resources" in caps or True:
            try:
                resp = await client.post(url, json={
                    "jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}
                }, headers=headers)
                data = resp.json()
                if "error" not in data:
                    for r in data.get("result", {}).get("resources", []):
                        result.resources.append(DiscoveredResource(
                            uri         = r.get("uri", ""),
                            name        = r.get("name", ""),
                            description = r.get("description", ""),
                            mime_type   = r.get("mimeType", ""),
                        ))
                    logger.info(f"[{server_name}] discovered {len(result.resources)} resources")
            except Exception as e:
                logger.debug(f"[{server_name}] resources/list not supported: {e}")

        # ── Step 4: prompts/list ────────────────────────────────
        if "prompts" in caps or True:
            try:
                resp = await client.post(url, json={
                    "jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}
                }, headers=headers)
                data = resp.json()
                if "error" not in data:
                    for p in data.get("result", {}).get("prompts", []):
                        result.prompts.append(DiscoveredPrompt(
                            name        = p.get("name", ""),
                            description = p.get("description", ""),
                            arguments   = p.get("arguments", []),
                        ))
                    logger.info(f"[{server_name}] discovered {len(result.prompts)} prompts")
            except Exception as e:
                logger.debug(f"[{server_name}] prompts/list not supported: {e}")

    return result


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

    # update server metadata
    server.protocol_version = result.protocol_version
    server.server_info      = result.server_info
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
        register_tool(t.prefixed_name, server.url, server.upstream_key or "")

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
        f"[{server.name}] saved: {len(result.tools)} tools, "
        f"{len(result.resources)} resources, {len(result.prompts)} prompts"
    )
