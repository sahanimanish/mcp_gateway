"""
proxy.py — MCP gateway proxy

Handles all JSON-RPC from downstream clients, enforces permissions,
and forwards to upstream servers. Key challenge: Elicitation.

Elicitation flow:
  1. Client sends  tools/call  →  gateway
  2. Gateway forwards to upstream (streaming)
  3. Upstream may emit  elicitation/create  mid-stream
  4. Gateway intercepts it, re-sends to the downstream client as a
     server→client request
  5. Client responds with  {"id":N,"result":{"action":"accept","content":{...}}}
  6. Gateway POSTs that response back to the upstream
  7. Upstream resumes tool execution and emits the final tool result
  8. Gateway returns that result to the client

The downstream client must declare  "elicitation": {}  in its initialize
capabilities for the gateway to forward elicitation requests to it.
"""

from __future__ import annotations
import asyncio
import json
import logging
import re
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.database import get_db, ActivityLog, MCPServer, MCPTool, MCPResource, MCPResourceTemplate, MCPPrompt
from gateway.auth import get_client_by_key
from gateway import registry

logger = logging.getLogger("mcp_proxy")
router = APIRouter(tags=["mcp"])

# ── Session cache for FastMCP streamable-HTTP ────────────────────
_session_cache: dict[str, str] = {}


# ── Helpers ──────────────────────────────────────────────────────

def rpc_error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def rpc_ok(id_, result) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


async def _log(db, method, client_name, tool, status, detail=""):
    db.add(ActivityLog(method=method, client_name=client_name,
                       tool=tool, status=status, detail=detail))
    await db.commit()


def _parse_sse(text: str) -> list[dict]:
    """Extract all JSON objects from an SSE body (one per data: line)."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                try:
                    out.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    return out


def _sse_event(data: dict) -> str:
    """Format a dict as an SSE event string."""
    return f"event: message\r\ndata: {json.dumps(data)}\r\n\r\n"


def _gateway_prompt_name(server_name: str, prompt_name: str) -> str:
    return f"{server_name}__{prompt_name}"


def _strip_gateway_prompt_name(prompt_name: str) -> tuple[str | None, str]:
    if "__" not in prompt_name:
        return None, prompt_name
    return prompt_name.split("__", 1)


def _template_to_pattern(uri_template: str) -> re.Pattern[str]:
    pattern = re.escape(uri_template)
    pattern = re.sub(r"\\\{[^{}]+\\\}", r"[^/]+", pattern)
    return re.compile(f"^{pattern}$")


def _resource_matches_templates(uri: str, templates: set[str]) -> bool:
    return any(_template_to_pattern(template).match(uri) for template in templates)


async def _get_session_id(server_url: str, upstream_key: str) -> Optional[str]:
    """Initialize a streamable-HTTP session and return its session ID."""
    if server_url in _session_cache:
        return _session_cache[server_url]

    url = server_url.rstrip("/") + "/mcp"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if upstream_key:
        headers["Authorization"] = f"Bearer {upstream_key}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"elicitation": {}},
                    "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"},
                }
            })
            sid = (resp.headers.get("mcp-session-id")
                   or resp.headers.get("Mcp-Session-Id"))
            if sid:
                _session_cache[server_url] = sid
            return sid
    except Exception as e:
        logger.warning(f"Could not initialize session for {server_url}: {e}")
        return None


def _parse_upstream_response(resp: httpx.Response) -> dict:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for message in _parse_sse(resp.text):
            if "result" in message or "error" in message:
                return message
        return {}
    try:
        return resp.json()
    except json.JSONDecodeError:
        return {}


async def _send_upstream_rpc_http(
    server_url: str,
    upstream_key: str,
    body: dict,
) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if upstream_key:
        headers["Authorization"] = f"Bearer {upstream_key}"

    sid = await _get_session_id(server_url, upstream_key)
    if sid:
        headers["Mcp-Session-Id"] = sid

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(server_url.rstrip("/") + "/mcp", json=body, headers=headers)
        response.raise_for_status()
        return _parse_upstream_response(response)


async def _send_upstream_rpc_sse(
    server_url: str,
    server_name: str,
    upstream_key: str,
    method: str,
    params: dict,
) -> dict:
    from gateway.sse_client import get_or_connect

    sse_client = await get_or_connect(server_url, server_name, upstream_key)
    return await sse_client.call(method, params, id_=None)


async def _send_upstream_rpc(
    server_url: str,
    server_name: str,
    upstream_key: str,
    transport: str,
    rpc_id,
    method: str,
    params: dict,
) -> dict:
    if transport == "sse":
        response = await _send_upstream_rpc_sse(server_url, server_name, upstream_key, method, params)
    else:
        response = await _send_upstream_rpc_http(
            server_url,
            upstream_key,
            {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
        )

    if "id" in response:
        response["id"] = rpc_id
    return response


# ── Main endpoint ────────────────────────────────────────────────

@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    auth=Depends(get_client_by_key),
    db: AsyncSession = Depends(get_db),
):
    client, permissions_by_server, allowed_server_ids = auth
    allowed_tools = {
        tool_name
        for server_permissions in permissions_by_server.values()
        for tool_name in server_permissions["tools"]
    }
    body = await request.json()
    method = body.get("method", "")
    rpc_id = body.get("id")

    # ── initialize ──────────────────────────────────────────────
    if method == "initialize":
        await _log(db, "initialize", client.name, "—", 200)
        return JSONResponse(rpc_ok(rpc_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
                "resources": {},
                "prompts": {},
                "elicitation": {},   # advertise elicitation support
            },
            "serverInfo": {"name": "mcp-gateway", "version": "1.0.0"},
        }))

    # ── tools/list ──────────────────────────────────────────────
    if method == "tools/list":
        visible_names = [t for t in registry.all_tools() if t in allowed_tools]
        tools_out = []
        if visible_names:
            result = await db.execute(
                select(MCPTool).where(MCPTool.name.in_(visible_names))
            )
            rows = {t.name: t for t in result.scalars().all()}
            for name in visible_names:
                row = rows.get(name)
                tools_out.append({
                    "name": name,
                    "description": row.description if row else "",
                    "inputSchema": row.input_schema if row else {"type": "object"},
                })
        await _log(db, "tools/list", client.name, "—", 200,
                   f"{len(tools_out)} tools visible")
        return JSONResponse(rpc_ok(rpc_id, {"tools": tools_out}))

    if method == "prompts/list":
        result = await db.execute(
            select(MCPPrompt, MCPServer)
            .join(MCPServer, MCPPrompt.server_id == MCPServer.id)
            .where(MCPPrompt.server_id.in_(allowed_server_ids))
            .order_by(MCPServer.name, MCPPrompt.name)
        )
        prompts_out = []
        for prompt, server in result.all():
            server_permissions = permissions_by_server.get(server.id, {})
            if prompt.name not in server_permissions.get("prompts", set()):
                continue
            prompts_out.append({
                "name": _gateway_prompt_name(server.name, prompt.name),
                "title": prompt.title or prompt.name,
                "description": prompt.description,
                "arguments": prompt.arguments or [],
                "icons": prompt.icons or [],
            })

        await _log(db, "prompts/list", client.name, "—", 200, f"{len(prompts_out)} prompts visible")
        return JSONResponse(rpc_ok(rpc_id, {"prompts": prompts_out}))

    if method == "prompts/get":
        params = body.get("params", {})
        prompt_name = params.get("name", "")
        server_name, raw_prompt_name = _strip_gateway_prompt_name(prompt_name)
        if not server_name:
            await _log(db, "prompts/get", client.name, prompt_name, 400, "Prompt must be namespaced as server__prompt")
            return JSONResponse(rpc_error(rpc_id, -32602, "Prompt name must be namespaced as server__prompt"), status_code=400)

        prompt_row = await db.execute(
            select(MCPPrompt, MCPServer)
            .join(MCPServer, MCPPrompt.server_id == MCPServer.id)
            .where(
                MCPServer.name == server_name,
                MCPPrompt.name == raw_prompt_name,
                MCPPrompt.server_id.in_(allowed_server_ids),
            )
        )
        row = prompt_row.first()
        if not row:
            await _log(db, "prompts/get", client.name, prompt_name, 404, "Prompt not found")
            return JSONResponse(rpc_error(rpc_id, -32601, f"Prompt not found: '{prompt_name}'"), status_code=404)

        prompt, server = row
        server_permissions = permissions_by_server.get(server.id, {})
        if prompt.name not in server_permissions.get("prompts", set()):
            await _log(db, "prompts/get", client.name, prompt_name, 403, "Prompt access denied")
            return JSONResponse(rpc_error(rpc_id, -32603, f"Access denied: '{prompt_name}'"), status_code=403)
        response = await _send_upstream_rpc(
            server.url,
            server.name,
            server.upstream_key or "",
            (server.server_info or {}).get("_transport", "http"),
            rpc_id,
            "prompts/get",
            {"name": prompt.name, "arguments": params.get("arguments", {})},
        )
        await _log(db, "prompts/get", client.name, prompt_name, 200 if "error" not in response else 502)
        return JSONResponse(response, status_code=200 if "error" not in response else 502)

    if method == "resources/list":
        result = await db.execute(
            select(MCPResource)
            .where(MCPResource.server_id.in_(allowed_server_ids))
            .order_by(MCPResource.name, MCPResource.uri)
        )
        resources_out = []
        for resource in result.scalars().all():
            server_permissions = permissions_by_server.get(resource.server_id, {})
            if resource.uri not in server_permissions.get("resources", set()):
                continue
            resources_out.append({
                "uri": resource.uri,
                "name": resource.name,
                "title": resource.title,
                "description": resource.description,
                "mimeType": resource.mime_type,
                "size": resource.size,
                "icons": resource.icons or [],
                "annotations": resource.annotations or {},
            })
        await _log(db, "resources/list", client.name, "—", 200, f"{len(resources_out)} resources visible")
        return JSONResponse(rpc_ok(rpc_id, {"resources": resources_out}))

    if method == "resources/templates/list":
        result = await db.execute(
            select(MCPResourceTemplate)
            .where(MCPResourceTemplate.server_id.in_(allowed_server_ids))
            .order_by(MCPResourceTemplate.name, MCPResourceTemplate.uri_template)
        )
        templates_out = []
        for template in result.scalars().all():
            server_permissions = permissions_by_server.get(template.server_id, {})
            if template.uri_template not in server_permissions.get("resource_templates", set()):
                continue
            templates_out.append({
                "uriTemplate": template.uri_template,
                "name": template.name,
                "title": template.title,
                "description": template.description,
                "mimeType": template.mime_type,
                "icons": template.icons or [],
                "annotations": template.annotations or {},
            })
        await _log(db, "resources/templates/list", client.name, "—", 200, f"{len(templates_out)} templates visible")
        return JSONResponse(rpc_ok(rpc_id, {"resourceTemplates": templates_out}))

    if method == "resources/read":
        params = body.get("params", {})
        uri = params.get("uri", "")
        resource_result = await db.execute(
            select(MCPResource, MCPServer)
            .join(MCPServer, MCPResource.server_id == MCPServer.id)
            .where(MCPResource.uri == uri, MCPResource.server_id.in_(allowed_server_ids))
        )
        matches = resource_result.all()
        allowed_matches = [
            (resource, server) for resource, server in matches
            if (
                resource.uri in permissions_by_server.get(server.id, {}).get("resources", set())
                or _resource_matches_templates(
                    resource.uri,
                    permissions_by_server.get(server.id, {}).get("resource_templates", set()),
                )
            )
        ]

        if not allowed_matches:
            template_result = await db.execute(
                select(MCPResourceTemplate, MCPServer)
                .join(MCPServer, MCPResourceTemplate.server_id == MCPServer.id)
                .where(MCPResourceTemplate.server_id.in_(allowed_server_ids))
            )
            template_matches = [
                (template, server) for template, server in template_result.all()
                if (
                    template.uri_template in permissions_by_server.get(server.id, {}).get("resource_templates", set())
                    and _resource_matches_templates(uri, {template.uri_template})
                )
            ]
            if len(template_matches) == 1:
                _, server = template_matches[0]
                response = await _send_upstream_rpc(
                    server.url,
                    server.name,
                    server.upstream_key or "",
                    (server.server_info or {}).get("_transport", "http"),
                    rpc_id,
                    "resources/read",
                    {"uri": uri},
                )
                await _log(db, "resources/read", client.name, uri, 200 if "error" not in response else 502)
                return JSONResponse(response, status_code=200 if "error" not in response else 502)
            if len(template_matches) > 1:
                await _log(db, "resources/read", client.name, uri, 409, "Ambiguous resource URI")
                return JSONResponse(rpc_error(rpc_id, -32602, f"Ambiguous resource URI across multiple servers: '{uri}'"), status_code=409)
            if matches:
                await _log(db, "resources/read", client.name, uri, 403, "Resource access denied")
                return JSONResponse(rpc_error(rpc_id, -32603, f"Access denied: '{uri}'"), status_code=403)
            await _log(db, "resources/read", client.name, uri, 404, "Resource not found")
            return JSONResponse(rpc_error(rpc_id, -32601, f"Resource not found: '{uri}'"), status_code=404)

        if len(allowed_matches) > 1:
            await _log(db, "resources/read", client.name, uri, 409, "Ambiguous resource URI")
            return JSONResponse(rpc_error(rpc_id, -32602, f"Ambiguous resource URI across multiple servers: '{uri}'"), status_code=409)

        resource, server = allowed_matches[0]
        response = await _send_upstream_rpc(
            server.url,
            server.name,
            server.upstream_key or "",
            (server.server_info or {}).get("_transport", "http"),
            rpc_id,
            "resources/read",
            {"uri": resource.uri},
        )
        await _log(db, "resources/read", client.name, uri, 200 if "error" not in response else 502)
        return JSONResponse(response, status_code=200 if "error" not in response else 502)

    # ── tools/call ──────────────────────────────────────────────
    if method == "tools/call":
        params    = body.get("params", {})
        tool_name = params.get("name", "")

        if tool_name not in allowed_tools:
            await _log(db, "tools/call", client.name, tool_name, 403)
            return JSONResponse(
                rpc_error(rpc_id, -32603, f"Access denied: '{tool_name}'"),
                status_code=403,
            )

        server_url   = registry.get_server_url(tool_name)
        upstream_key = registry.get_server_key(tool_name) or ""
        transport    = registry.get_transport(tool_name)

        if not server_url:
            await _log(db, "tools/call", client.name, tool_name, 404)
            return JSONResponse(
                rpc_error(rpc_id, -32601, f"Tool not found: '{tool_name}'"),
                status_code=404,
            )

        # Strip prefix from tool name for upstream
        raw_name = tool_name.split("__", 1)[1] if "__" in tool_name else tool_name
        forward_body = {
            **body,
            "params": {**params, "name": raw_name},
        }

        # ── SSE transport ────────────────────────────────────────
        if transport == "sse":
            return await _call_sse(
                request, db, client, tool_name, forward_body,
                server_url, upstream_key, rpc_id,
            )

        # ── HTTP / streamable-HTTP transport ─────────────────────
        return await _call_http(
            request, db, client, tool_name, forward_body,
            server_url, upstream_key, rpc_id,
        )

    # ── elicitation response from client ────────────────────────
    # The client POSTs {"jsonrpc":"2.0","id":N,"result":{...}} in response
    # to an elicitation/create we forwarded. Route it to the pending handler.
    if "result" in body and "method" not in body:
        elicit_id = body.get("id")
        handler = _elicit_pending.pop(elicit_id, None)
        if handler and not handler.done():
            handler.set_result(body)
            return JSONResponse(content=None, status_code=202)
        logger.warning(f"Unexpected elicitation response id={elicit_id}")
        return JSONResponse(content=None, status_code=202)

    # ── unknown method ───────────────────────────────────────────
    await _log(db, method, client.name, "—", 404, "Unknown method")
    return JSONResponse(
        rpc_error(rpc_id, -32601, f"Method not found: {method}"),
        status_code=404,
    )


# ── Elicitation pending map ──────────────────────────────────────
# Maps elicitation JSON-RPC id → asyncio.Future[dict]
_elicit_pending: dict[int | str, asyncio.Future] = {}


async def _forward_elicitation(
    request: Request,
    elicit_msg: dict,
    client_name: str,
) -> dict:
    """
    Forward an elicitation/create from upstream to the downstream client.

    The downstream client must respond by POSTing back to /mcp with the
    same id and a result.  We wait on a Future that _mcp_endpoint resolves
    when that POST arrives.
    """
    elicit_id = elicit_msg.get("id", 0)
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    _elicit_pending[elicit_id] = fut

    # We cannot send unsolicited messages to the downstream client over HTTP
    # (they don't hold an open SSE stream to us).  Instead, we surface the
    # elicitation as a pending item in a short-poll endpoint AND store it
    # so the next SSE-capable client can pick it up.
    # For SSE-capable downstream clients the StreamingResponse path handles it.
    logger.info(f"[{client_name}] elicitation/create id={elicit_id} pending")

    try:
        response = await asyncio.wait_for(fut, timeout=120.0)
        return response
    except asyncio.TimeoutError:
        _elicit_pending.pop(elicit_id, None)
        return {"jsonrpc": "2.0", "id": elicit_id,
                "result": {"action": "cancel"}}


# ── HTTP tool call with elicitation support ──────────────────────

async def _call_http(
    request: Request,
    db,
    client,
    tool_name: str,
    forward_body: dict,
    server_url: str,
    upstream_key: str,
    rpc_id,
) -> StreamingResponse | JSONResponse:
    """
    Forward a tools/call to an HTTP/streamable-HTTP upstream.
    Handles elicitation/create events mid-stream by:
      1. Detecting if the downstream client declared elicitation support
         (Accept header contains text/event-stream, or we check db/caps)
      2. For SSE-streaming clients → yield elicitation event inline
      3. For plain-JSON clients → answer with 'cancel' automatically
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if upstream_key:
        headers["Authorization"] = f"Bearer {upstream_key}"

    sid = await _get_session_id(server_url, upstream_key)
    if sid:
        headers["Mcp-Session-Id"] = sid

    # Check if the downstream client can receive SSE (sent Accept: text/event-stream)
    downstream_accept = request.headers.get("accept", "")
    client_wants_sse  = "text/event-stream" in downstream_accept

    async def upstream_stream():
        """Generator that streams upstream SSE, intercepting elicitation events."""
        async with httpx.AsyncClient(timeout=60.0) as http:
            async with http.stream(
                "POST",
                server_url.rstrip("/") + "/mcp",
                json=forward_body,
                headers=headers,
            ) as resp:
                if resp.status_code in (401, 404) and sid:
                    _session_cache.pop(server_url, None)

                event_type = None
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        payload = line[5:].strip()
                        if not payload:
                            continue
                        try:
                            msg = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        # ── Intercept elicitation/create ──────────
                        if msg.get("method") == "elicitation/create":
                            elicit_id   = msg.get("id", 0)
                            elicit_params = msg.get("params", {})
                            logger.info(
                                f"[{client.name}] elicitation/create "
                                f"id={elicit_id} mode={elicit_params.get('mode','form')}"
                            )

                            if client_wants_sse:
                                # Forward to downstream client as SSE event
                                yield _sse_event(msg)

                                # Wait for downstream to POST a response
                                loop = asyncio.get_event_loop()
                                fut: asyncio.Future = loop.create_future()
                                _elicit_pending[elicit_id] = fut
                                try:
                                    elicit_resp = await asyncio.wait_for(fut, timeout=120.0)
                                except asyncio.TimeoutError:
                                    _elicit_pending.pop(elicit_id, None)
                                    elicit_resp = {
                                        "jsonrpc": "2.0", "id": elicit_id,
                                        "result": {"action": "cancel"}
                                    }

                                # POST elicitation response back to upstream
                                try:
                                    await http.post(
                                        server_url.rstrip("/") + "/mcp",
                                        json=elicit_resp,
                                        headers=headers,
                                    )
                                except Exception as e:
                                    logger.warning(f"Failed to send elicitation response: {e}")
                            else:
                                # Client can't handle elicitation → auto-cancel
                                logger.info(
                                    f"[{client.name}] auto-cancelling elicitation "
                                    f"(client not SSE-capable)"
                                )
                                try:
                                    await http.post(
                                        server_url.rstrip("/") + "/mcp",
                                        json={"jsonrpc": "2.0", "id": elicit_id,
                                              "result": {"action": "cancel"}},
                                        headers=headers,
                                    )
                                except Exception:
                                    pass

                        else:
                            # Regular message — pass through
                            yield _sse_event(msg)

                        event_type = None

    if client_wants_sse:
        await _log(db, "tools/call", client.name, tool_name, 200)
        return StreamingResponse(
            upstream_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        # Collect all events, return last result as JSON
        result_msg = {}
        async for chunk in upstream_stream():
            for msg in _parse_sse(chunk):
                if "result" in msg or "error" in msg:
                    result_msg = msg
        await _log(db, "tools/call", client.name, tool_name, 200)
        return JSONResponse(result_msg or rpc_error(rpc_id, -32000, "No response"))


# ── SSE transport tool call with elicitation support ─────────────

async def _call_sse(
    request: Request,
    db,
    client,
    tool_name: str,
    forward_body: dict,
    server_url: str,
    upstream_key: str,
    rpc_id,
) -> StreamingResponse | JSONResponse:
    """
    Forward a tools/call to an SSE-transport upstream.
    Registers an elicitation handler on the SSE client so that
    elicitation/create events are forwarded to the downstream client.
    """
    from gateway.sse_client import get_or_connect
    server_name = tool_name.split("__")[0]

    downstream_accept = request.headers.get("accept", "")
    client_wants_sse  = "text/event-stream" in downstream_accept

    sse_c = await get_or_connect(server_url, server_name, upstream_key)
    params = forward_body.get("params", {})
    # Always use SSE client's auto-increment id — never the client's rpc_id.
    # This prevents collisions when multiple clients make concurrent calls.
    call_id = None  # let SSE client pick a unique id

    if client_wants_sse:
        # Queue to pass elicitation events to the streaming generator
        elicit_q: asyncio.Queue = asyncio.Queue()

        async def elicitation_handler(msg: dict) -> dict:
            """Called by SSE reader when elicitation/create arrives."""
            elicit_id = msg.get("id", 0)
            # Push to stream queue so generator can yield it to client
            await elicit_q.put(("elicit", msg))
            # Wait for client's response via /mcp POST
            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            _elicit_pending[elicit_id] = fut
            try:
                resp = await asyncio.wait_for(fut, timeout=120.0)
                return resp.get("result", {"action": "cancel"})
            except asyncio.TimeoutError:
                return {"action": "cancel"}

        sse_c._elicitation_handler = elicitation_handler

        result_holder: list = []

        async def run_tool():
            try:
                result = await sse_c.call("tools/call", params, id_=call_id)
                # Remap internal SSE id back to the original client's rpc_id
                if "id" in result:
                    result = {**result, "id": rpc_id}
                result_holder.append(result)
            finally:
                await elicit_q.put(("done", None))
                sse_c._elicitation_handler = None

        tool_task = asyncio.create_task(run_tool())

        async def stream_gen():
            while True:
                item = await elicit_q.get()
                kind, data = item
                if kind == "elicit":
                    yield _sse_event(data)
                elif kind == "done":
                    break
            # Yield final tool result
            if result_holder:
                yield _sse_event(result_holder[0])
            await tool_task  # ensure cleanup

        await _log(db, "tools/call", client.name, tool_name, 200)
        return StreamingResponse(
            stream_gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    else:
        # Plain JSON client — auto-cancel any elicitation
        async def auto_cancel_handler(msg: dict) -> dict:
            logger.info(f"[{client.name}] auto-cancelling elicitation (non-SSE client)")
            return {"action": "cancel"}

        sse_c._elicitation_handler = auto_cancel_handler
        try:
            result_msg = await sse_c.call("tools/call", params, id_=call_id)
            # Remap internal SSE id back to the original client's rpc_id
            if "id" in result_msg:
                result_msg = {**result_msg, "id": rpc_id}
        except Exception as e:
            result_msg = rpc_error(rpc_id, -32000, str(e))
        finally:
            sse_c._elicitation_handler = None

        await _log(db, "tools/call", client.name, tool_name, 200)
        return JSONResponse(result_msg)
