from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.database import get_db, ActivityLog, MCPTool, MCPResource, MCPPrompt, MCPResourceTemplate
from gateway.auth import get_client_by_key
from gateway import registry

logger = logging.getLogger("mcp_proxy")
router = APIRouter(tags=["mcp"])

import uuid

# Maps downstream session_id -> (client_api_key, asyncio.Queue)
# We use the Queue to push upstream responses down to the connected client
_downstream_sse_sessions: dict[str, tuple[str, asyncio.Queue]] = {}
# Isolated cache: (server_url, client_api_key) -> session ID
_session_cache: dict[tuple[str, str], str] = {}
_elicit_pending: dict[int | str, asyncio.Future] = {}

def rpc_error(id_, code: int, message: str) -> dict: return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}
def rpc_ok(id_, result) -> dict: return {"jsonrpc": "2.0", "id": id_, "result": result}
def _sse_event(data: dict) -> str: return f"event: message\r\ndata: {json.dumps(data)}\r\n\r\n"

async def _log(db, method, client_name, tool, status, detail=""):
    db.add(ActivityLog(method=method, client_name=client_name, tool=tool, status=status, detail=detail))
    await db.commit()

async def _get_session_id(server_url: str, upstream_key: str, client_key: str) -> Optional[str]:
    cache_key = (server_url, client_key)
    if cache_key in _session_cache: return _session_cache[cache_key]
    url = server_url.rstrip("/") + "/mcp"
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if upstream_key: headers["Authorization"] = f"Bearer {upstream_key}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {"elicitation": {}}, "clientInfo": {"name": "mcp-gateway"}}})
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if sid: _session_cache[cache_key] = sid
            return sid
    except Exception as e:
        logger.warning(f"Session init failed: {e}")
        return None

@router.post("/mcp")
async def mcp_endpoint(request: Request, auth=Depends(get_client_by_key), db: AsyncSession = Depends(get_db)):
    client, allowed_tools = auth
    body = await request.json()
    method = body.get("method", "")
    rpc_id = body.get("id")

    # ── PING ──
    if method == "ping":
        return JSONResponse(rpc_ok(rpc_id, {}))

    # ── INITIALIZE ──
    if method == "initialize":
        has_res = (await db.execute(select(MCPResource).limit(1))).scalar_one_or_none()
        has_prm = (await db.execute(select(MCPPrompt).limit(1))).scalar_one_or_none()
        
        caps = {"tools": {}, "elicitation": {}}
        if has_res: caps["resources"] = {}
        if has_prm: caps["prompts"] = {}
        
        await _log(db, "initialize", client.name, "—", 200)
        return JSONResponse(rpc_ok(rpc_id, {"protocolVersion": "2024-11-05", "capabilities": caps, "serverInfo": {"name": "mcp-gateway", "version": "1.0.0"}}))

    if method == "notifications/initialized": return JSONResponse(content=None, status_code=202)
    if method == "notifications/cancelled": return JSONResponse(content=None, status_code=202)

    # ── TOOLS ──
    if method == "tools/list":
        visible = [t for t in registry.all_tools() if t in allowed_tools]
        tools_out = []
        if visible:
            rows = {t.name: t for t in (await db.execute(select(MCPTool).where(MCPTool.name.in_(visible)))).scalars().all()}
            tools_out = [{"name": n, "description": rows[n].description if n in rows else "", "inputSchema": rows[n].input_schema if n in rows else {}} for n in visible]
        return JSONResponse(rpc_ok(rpc_id, {"tools": tools_out}))

    if method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name", "")
        if tool_name not in allowed_tools: return JSONResponse(rpc_error(rpc_id, -32603, "Access denied"), status_code=403)
        srv_info = registry.get_server_for_tool(tool_name)
        if not srv_info: return JSONResponse(rpc_error(rpc_id, -32601, "Tool not found"), status_code=404)
        
        raw_name = tool_name.split("__", 1)[1] if "__" in tool_name else tool_name
        forward_body = {**body, "params": {**params, "name": raw_name}}
        return await _forward_request(request, db, client, "tool", tool_name, forward_body, srv_info, rpc_id)

    # ── PROMPTS ──
    if method == "prompts/list":
        prompts = [{"name": p.name, "description": p.description} for p in (await db.execute(select(MCPPrompt))).scalars().all()]
        return JSONResponse(rpc_ok(rpc_id, {"prompts": prompts}))

    if method == "prompts/get":
        params = body.get("params", {})
        p_name = params.get("name", "")
        srv_info = registry.get_server_for_prompt(p_name)
        if not srv_info: return JSONResponse(rpc_error(rpc_id, -32601, "Prompt not found"), status_code=404)
        raw_name = p_name.split("__", 1)[1] if "__" in p_name else p_name
        forward_body = {**body, "params": {**params, "name": raw_name}}
        return await _forward_request(request, db, client, "prompt", p_name, forward_body, srv_info, rpc_id)

    # ── RESOURCES ──
    if method == "resources/list":
        resources = [{"uri": r.uri, "name": r.name, "mimeType": r.mime_type} for r in (await db.execute(select(MCPResource))).scalars().all()]
        return JSONResponse(rpc_ok(rpc_id, {"resources": resources}))
        
    if method == "resources/templates/list":
        templates = [{"uriTemplate": t.uri_template, "name": t.name} for t in (await db.execute(select(MCPResourceTemplate))).scalars().all()]
        return JSONResponse(rpc_ok(rpc_id, {"resourceTemplates": templates}))

    if method == "resources/read":
        uri = body.get("params", {}).get("uri", "")
        srv_info = registry.get_server_for_resource(uri)
        if not srv_info: return JSONResponse(rpc_error(rpc_id, -32601, "Resource not found"), status_code=404)
        return await _forward_request(request, db, client, "resource", uri, body, srv_info, rpc_id)

    # ── ELICITATION RESPONSES ──
    if "result" in body and "method" not in body:
        handler = _elicit_pending.pop(body.get("id"), None)
        if handler and not handler.done(): handler.set_result(body)
        return JSONResponse(content=None, status_code=202)

    return JSONResponse(rpc_error(rpc_id, -32601, f"Method not found: {method}"), status_code=404)


@router.get("/sse")
async def sse_subscribe(apiKey: str, db: AsyncSession = Depends(get_db)):
    """
    Standard MCP SSE connection endpoint. 
    Client connects here and stays connected.
    """
    # 1. Validate the API Key (since we can't easily use headers in GET SSE)
    from gateway.database import Client
    client = (await db.execute(select(Client).where(Client.api_key == apiKey))).scalar_one_or_none()
    if not client or not client.is_active:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    # 2. Create a session
    session_id = str(uuid.uuid4())
    client_queue = asyncio.Queue()
    _downstream_sse_sessions[session_id] = (apiKey, client_queue)

    async def sse_generator():
        # First event MUST be the 'endpoint' event per MCP spec
        # This tells the client where to send its POST requests
        yield f"event: endpoint\r\ndata: /mcp/messages?sessionId={session_id}\r\n\r\n"
        
        try:
            while True:
                # Wait for messages pushed from the /messages POST handler
                msg = await client_queue.get()
                yield f"event: message\r\ndata: {json.dumps(msg)}\r\n\r\n"
        except asyncio.CancelledError:
            # Client disconnected, clean up
            _downstream_sse_sessions.pop(session_id, None)

    return StreamingResponse(
        sse_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no" # Important for Nginx
        }
    )

@router.post("/messages")
async def sse_messages(request: Request, sessionId: str, db: AsyncSession = Depends(get_db)):
    """
    Standard MCP SSE messages endpoint.
    Clients POST their JSON-RPC requests here. We process them, 
    and push the response into their open SSE stream.
    """
    if sessionId not in _downstream_sse_sessions:
        return JSONResponse({"error": "Invalid or expired session"}, status_code=400)

    api_key, client_queue = _downstream_sse_sessions[sessionId]
    
    # Re-authenticate to get full client object and allowed tools
    from gateway.auth import get_client_by_key
    # Manually call our auth dependency
    class FakeAuthRequest:
        headers = {"x-api-key": api_key}
    client, allowed_tools = await get_client_by_key(x_api_key=api_key, db=db)

    body = await request.json()
    
    # We must return 202 Accepted immediately per MCP spec
    # and process the actual request in the background
    async def process_and_push():
        # Temporarily mock the request so we can reuse our existing mcp_endpoint logic
        request.scope["headers"] = [(b"x-api-key", api_key.encode())]
        
        # 1. Process the request using your massive mcp_endpoint logic
        response = await mcp_endpoint(request, auth=(client, allowed_tools), db=db)
        
        # 2. Extract the JSON and push it down the SSE stream
        if isinstance(response, JSONResponse):
            resp_body = json.loads(response.body)
            await client_queue.put(resp_body)
        elif isinstance(response, StreamingResponse):
            # If the upstream was streaming (like an elicitation), 
            # proxy those stream events directly into the client's queue
            async for chunk in response.body_iterator:
                if isinstance(chunk, str) and chunk.startswith("data: "):
                    try:
                        msg = json.loads(chunk[6:].strip())
                        await client_queue.put(msg)
                    except:
                        pass

    # Run the processing in the background so we can return 202 instantly
    asyncio.create_task(process_and_push())
    
    return JSONResponse(content="Accepted", status_code=202)

    
async def _forward_request(request, db, client, type_, target_name, body, srv_info, rpc_id):
    if srv_info["transport"] == "sse":
        return await _call_sse(request, db, client, target_name, body, srv_info, rpc_id)
    return await _call_http(request, db, client, target_name, body, srv_info, rpc_id)

async def _call_http(request, db, client, target_name, forward_body, srv_info, rpc_id):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if srv_info["key"]: headers["Authorization"] = f"Bearer {srv_info['key']}"
    sid = await _get_session_id(srv_info["url"], srv_info["key"], client.api_key)
    if sid: headers["Mcp-Session-Id"] = sid

    client_wants_sse = "text/event-stream" in request.headers.get("accept", "")
    
    async def upstream_stream():
        try:
            async with httpx.AsyncClient(timeout=60.0) as http:
                async with http.stream("POST", srv_info["url"].rstrip("/") + "/mcp", json=forward_body, headers=headers) as resp:
                    if resp.status_code in (401, 404) and sid: _session_cache.pop((srv_info["url"], client.api_key), None)
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if line.startswith("data:"):
                            payload = line[5:].strip()
                            if payload:
                                try:
                                    msg = json.loads(payload)
                                    if msg.get("method") == "elicitation/create":
                                        if client_wants_sse:
                                            yield _sse_event(msg)
                                            fut = asyncio.get_event_loop().create_future()
                                            _elicit_pending[msg.get("id")] = fut
                                            try:
                                                resp = await asyncio.wait_for(fut, timeout=120.0)
                                            except asyncio.TimeoutError:
                                                resp = {"jsonrpc": "2.0", "id": msg.get("id"), "result": {"action": "cancel"}}
                                            await http.post(srv_info["url"].rstrip("/") + "/mcp", json=resp, headers=headers)
                                        else:
                                            await http.post(srv_info["url"].rstrip("/") + "/mcp", json={"jsonrpc": "2.0", "id": msg.get("id"), "result": {"action": "cancel"}}, headers=headers)
                                    else:
                                        yield _sse_event(msg)
                                except json.JSONDecodeError: pass
        except httpx.RequestError as e:
            yield _sse_event(rpc_error(rpc_id, -32603, f"Upstream unavailable: {str(e)}"))

    if client_wants_sse: return StreamingResponse(upstream_stream(), media_type="text/event-stream")
    
    result_msg = {}
    try:
        async for chunk in upstream_stream():
            for line in chunk.splitlines():
                if line.startswith("data:"):
                    msg = json.loads(line[5:])
                    if "result" in msg or "error" in msg: result_msg = msg
    except Exception as e:
        result_msg = rpc_error(rpc_id, -32603, str(e))
    return JSONResponse(result_msg or rpc_error(rpc_id, -32000, "No response"))

async def _call_sse(request, db, client, target_name, forward_body, srv_info, rpc_id):
    from gateway.sse_client import get_or_connect
    sse_c = await get_or_connect(srv_info["url"], "server", srv_info["key"])
    client_wants_sse = "text/event-stream" in request.headers.get("accept", "")

    if client_wants_sse:
        elicit_q = asyncio.Queue()
        async def elicitation_handler(msg: dict):
            await elicit_q.put(("elicit", msg))
            fut = asyncio.get_event_loop().create_future()
            _elicit_pending[msg.get("id")] = fut
            try: return (await asyncio.wait_for(fut, timeout=120.0)).get("result", {"action": "cancel"})
            except asyncio.TimeoutError: return {"action": "cancel"}
            
        sse_c._elicitation_handlers.add(elicitation_handler)
        result_holder = []
        async def run_tool():
            try:
                res = await sse_c.call(forward_body["method"], forward_body.get("params", {}))
                if "id" in res: res["id"] = rpc_id
                result_holder.append(res)
            except Exception as e:
                result_holder.append(rpc_error(rpc_id, -32000, str(e)))
            finally:
                await elicit_q.put(("done", None))
                sse_c._elicitation_handlers.discard(elicitation_handler)
                
        asyncio.create_task(run_tool())
        async def stream_gen():
            while True:
                kind, data = await elicit_q.get()
                if kind == "elicit": yield _sse_event(data)
                elif kind == "done": break
            if result_holder: yield _sse_event(result_holder[0])
            
        return StreamingResponse(stream_gen(), media_type="text/event-stream")
    else:
        try:
            res = await sse_c.call(forward_body["method"], forward_body.get("params", {}))
            if "id" in res: res["id"] = rpc_id
            return JSONResponse(res)
        except Exception as e:
            return JSONResponse(rpc_error(rpc_id, -32000, str(e)))