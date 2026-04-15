from __future__ import annotations
from typing import Optional
from dataclasses import dataclass, field
from datetime import datetime
import asyncio
import json
import httpx
import logging

logger = logging.getLogger("registry")

_servers: dict[str, dict] = {} # server_name -> {url, key, transport}

# Routing maps
_tool_registry: dict[str, str] = {}      # prefixed_name -> server_name
_prompt_registry: dict[str, str] = {}    # prefixed_name -> server_name
_resource_registry: dict[str, str] = {}  # uri -> server_name

def get_server_info(server_name: str) -> Optional[dict]:
    return _servers.get(server_name)

def get_server_for_tool(tool_name: str) -> Optional[dict]:
    s_name = _tool_registry.get(tool_name)
    return _servers.get(s_name) if s_name else None

def get_server_for_prompt(prompt_name: str) -> Optional[dict]:
    s_name = _prompt_registry.get(prompt_name)
    return _servers.get(s_name) if s_name else None

def get_server_for_resource(uri: str) -> Optional[dict]:
    s_name = _resource_registry.get(uri)
    return _servers.get(s_name) if s_name else None

def all_tools() -> list[str]:
    return list(_tool_registry.keys())

def unregister_server(server_name: str):
    _servers.pop(server_name, None)
    prefix = server_name + "__"
    for k in [k for k in _tool_registry if k.startswith(prefix)]: del _tool_registry[k]
    for k in [k for k in _prompt_registry if k.startswith(prefix)]: del _prompt_registry[k]
    for k, v in list(_resource_registry.items()):
        if v == server_name: del _resource_registry[k]

async def load_from_db(db):
    from sqlalchemy import select
    from gateway.database import MCPServer, MCPTool, MCPResource, MCPPrompt
    servers = (await db.execute(select(MCPServer))).scalars().all()
    _servers.clear()
    _tool_registry.clear()
    _prompt_registry.clear()
    _resource_registry.clear()

    for srv in servers:
        transport = srv.server_info.get("_transport", "http") if srv.server_info else "http"
        _servers[srv.name] = {"url": srv.url, "key": srv.upstream_key or "", "transport": transport}
        
        tools = (await db.execute(select(MCPTool).where(MCPTool.server_id == srv.id))).scalars().all()
        for t in tools: _tool_registry[t.name] = srv.name
            
        prompts = (await db.execute(select(MCPPrompt).where(MCPPrompt.server_id == srv.id))).scalars().all()
        for p in prompts: _prompt_registry[p.name] = srv.name
            
        resources = (await db.execute(select(MCPResource).where(MCPResource.server_id == srv.id))).scalars().all()
        for r in resources: _resource_registry[r.uri] = srv.name

@dataclass
class DiscoveredTool:
    raw_name: str; prefixed_name: str; description: str = ""; input_schema: dict = field(default_factory=dict)
@dataclass
class DiscoveredResource:
    uri: str; name: str = ""; description: str = ""; mime_type: str = ""
@dataclass
class DiscoveredTemplate:
    uri_template: str; name: str = ""; description: str = ""; mime_type: str = ""
@dataclass
class DiscoveredPrompt:
    name: str; description: str = ""; arguments: list = field(default_factory=list)

@dataclass
class DiscoveryResult:
    success:          bool
    error:            str = ""
    transport:        str = "http"
    protocol_version: str = ""
    server_info:      dict = field(default_factory=dict)
    capabilities:     dict = field(default_factory=dict)
    tools:            list[DiscoveredTool]     = field(default_factory=list)
    resources:        list[DiscoveredResource] = field(default_factory=list)
    # The dataclass uses 'templates'
    templates:        list[DiscoveredTemplate] = field(default_factory=list) 
    prompts:          list[DiscoveredPrompt]   = field(default_factory=list)

def _parse_response(resp: httpx.Response) -> dict:
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in resp.text.splitlines():
            if line.strip().startswith("data:"):
                payload = line.strip()[5:].strip()
                if payload: return json.loads(payload)
    try: return resp.json()
    except: return {}





async def discover(server_url: str, server_name: str, upstream_key: str = "") -> DiscoveryResult:
    result = await _discover_http(server_url, server_name, upstream_key)
    if result.success: return result
    result_sse = await _discover_sse(server_url, server_name, upstream_key)
    if result_sse.success: return result_sse
    result.error = f"HTTP: {result.error} | SSE: {result_sse.error}"
    return result

async def _discover_http(server_url: str, server_name: str, upstream_key: str = "") -> DiscoveryResult:
    url = server_url.rstrip("/") + "/mcp"
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if upstream_key: headers["Authorization"] = f"Bearer {upstream_key}"
    result = DiscoveryResult(success=False, transport="http")

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"}}}, headers=headers)
            resp.raise_for_status()
            init_data = _parse_response(resp)
        except Exception as e:
            result.error = f"init failed: {e}"
            return result

        result.protocol_version = init_data.get("result", {}).get("protocolVersion", "")
        result.capabilities = init_data.get("result", {}).get("capabilities", {})
        result.success = True
        
        session_id = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if session_id: headers["Mcp-Session-Id"] = session_id

        async def rpc(method: str, id_: int) -> dict:
            return _parse_response(await client.post(url, json={"jsonrpc": "2.0", "id": id_, "method": method}, headers=headers))

        result.tools = await _fetch_tools(server_name, rpc, 2)
        result.resources = await _fetch_resources(server_name, rpc, 3)
        result.templates = await _fetch_templates(server_name, rpc, 4)
        result.prompts = await _fetch_prompts(server_name, rpc, 5)
    return result

async def _discover_sse(server_url: str, server_name: str, upstream_key: str = "") -> DiscoveryResult:
    from gateway.sse_client import SSEServerClient
    result = DiscoveryResult(success=False, transport="sse")
    client = SSEServerClient(server_url, server_name, upstream_key)
    try:
        await asyncio.wait_for(client.connect(), timeout=8.0)
        init = await client.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "gateway", "version": "1.0.0"}}, id_=1)
        result.protocol_version = init.get("result", {}).get("protocolVersion", "")
        result.capabilities = init.get("result", {}).get("capabilities", {})
        result.success = True
        result.server_info["_transport"] = "sse"
        
        async def rpc(method: str, id_: int) -> dict: return await client.call(method, {}, id_=id_)
        
        result.tools = await _fetch_tools(server_name, rpc, 2)
        result.resources = await _fetch_resources(server_name, rpc, 3)
        result.templates = await _fetch_templates(server_name, rpc, 4)
        result.prompts = await _fetch_prompts(server_name, rpc, 5)
    except Exception as e:
        result.error = str(e)
    finally:
        await client.close()
    return result

async def _fetch_tools(server_name: str, rpc, id_: int) -> list[DiscoveredTool]:
    try:
        data = await rpc("tools/list", id_)
        return [DiscoveredTool(raw_name=t.get("name"), prefixed_name=f"{server_name}__{t.get('name')}", description=t.get("description", ""), input_schema=t.get("inputSchema", {})) for t in data.get("result", {}).get("tools", []) if isinstance(t, dict)]
    except: return []

async def _fetch_resources(server_name: str, rpc, id_: int) -> list[DiscoveredResource]:
    try:
        data = await rpc("resources/list", id_)
        return [DiscoveredResource(uri=r.get("uri", ""), name=r.get("name", ""), mime_type=r.get("mimeType", "")) for r in data.get("result", {}).get("resources", [])]
    except: return []

async def _fetch_templates(server_name: str, rpc, id_: int) -> list[DiscoveredTemplate]:
    try:
        data = await rpc("resources/templates/list", id_)
        return [DiscoveredTemplate(uri_template=t.get("uriTemplate", ""), name=t.get("name", "")) for t in data.get("result", {}).get("resourceTemplates", [])]
    except: return []

async def _fetch_prompts(server_name: str, rpc, id_: int) -> list[DiscoveredPrompt]:
    try:
        data = await rpc("prompts/list", id_)
        return [DiscoveredPrompt(name=f"{server_name}__{p.get('name', '')}", description=p.get("description", ""), arguments=p.get("arguments", [])) for p in data.get("result", {}).get("prompts", [])]
    except: return []

async def save_discovery(db, server, result: DiscoveryResult):
    from sqlalchemy import delete
    from gateway.database import MCPTool, MCPResource, MCPPrompt, MCPResourceTemplate, new_id
    from datetime import datetime, timezone

    await db.execute(delete(MCPTool).where(MCPTool.server_id == server.id))
    await db.execute(delete(MCPResource).where(MCPResource.server_id == server.id))
    await db.execute(delete(MCPResourceTemplate).where(MCPResourceTemplate.server_id == server.id))
    await db.execute(delete(MCPPrompt).where(MCPPrompt.server_id == server.id))
    unregister_server(server.name)

    server.protocol_version = result.protocol_version
    server.server_info = {**(result.server_info or {}), "_transport": result.transport}
    server.capabilities = result.capabilities
    server.status = "online" if result.success else "offline"
    server.last_seen = datetime.now(timezone.utc)
    
    _servers[server.name] = {"url": server.url, "key": server.upstream_key or "", "transport": result.transport}

    for t in result.tools:
        db.add(MCPTool(id=new_id(), server_id=server.id, name=t.prefixed_name, raw_name=t.raw_name, description=t.description, input_schema=t.input_schema))
        _tool_registry[t.prefixed_name] = server.name

    for r in result.resources:
        db.add(MCPResource(id=new_id(), server_id=server.id, uri=r.uri, name=r.name, mime_type=r.mime_type))
        _resource_registry[r.uri] = server.name

    for tpl in result.templates: # Uses result.templates from the dataclass
        db.add(MCPResourceTemplate(
            id           = new_id(),
            server_id    = server.id,
            uri_template = tpl.uri_template,
            name         = tpl.name,
            description  = tpl.description,
            mime_type    = tpl.mime_type
        ))

    for p in result.prompts:
        db.add(MCPPrompt(id=new_id(), server_id=server.id, name=p.name, description=p.description, arguments=p.arguments))
        _prompt_registry[p.name] = server.name



    await db.commit()