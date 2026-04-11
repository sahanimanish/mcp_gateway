from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
import httpx
import logging

from gateway.database import get_db, ActivityLog
from gateway.auth import get_client_by_key
from gateway import registry

logger = logging.getLogger("mcp_proxy")
router = APIRouter(tags=["mcp"])

# Per-server session ID cache (FastMCP streamable-HTTP requires this)
# Maps server_url → Mcp-Session-Id
_session_cache: dict[str, str] = {}


def rpc_error(id_, code: int, message: str):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def rpc_ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _parse_upstream_response(resp: httpx.Response) -> dict:
    """
    Parse upstream response — handles both plain JSON and SSE-wrapped JSON (FastMCP).
    SSE format: lines containing 'data: {...json...}'
    """
    ct = resp.headers.get("content-type", "")
    if "text/event-stream" in ct:
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    import json
                    return json.loads(payload)
    try:
        return resp.json()
    except Exception:
        return {}


async def _log(db: AsyncSession, method: str, client_name: str, tool: str, status: int, detail: str = ""):
    db.add(ActivityLog(method=method, client_name=client_name, tool=tool, status=status, detail=detail))
    await db.commit()


async def _get_session_id(server_url: str, upstream_key: str) -> str | None:
    """
    Ensure we have a valid session ID for FastMCP servers.
    Runs initialize if we don't have one cached yet.
    Returns session_id string, or None if server doesn't use sessions.
    """
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
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-gateway", "version": "1.0.0"}
                }
            }, headers=headers)
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if sid:
                _session_cache[server_url] = sid
                logger.debug(f"Cached session for {server_url}: {sid}")
            return sid
    except Exception as e:
        logger.warning(f"Could not initialize session for {server_url}: {e}")
        return None


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    auth=Depends(get_client_by_key),
    db: AsyncSession = Depends(get_db),
):
    client, allowed_tools = auth
    body = await request.json()

    method = body.get("method", "")
    rpc_id = body.get("id")

    print(f"Received request from client '{client.name}': method={method}, id={rpc_id}")
    print(f"Allowed tools for this client: {allowed_tools}")
    print(f"Body: {body}")


    # ── initialize ──────────────────────────────────────────────
    if method == "initialize":
        await _log(db, "initialize", client.name, "—", 200)
        return JSONResponse(rpc_ok(rpc_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mcp-gateway", "version": "1.0.0"}
        }))

    # ── tools/list ──────────────────────────────────────────────
    if method == "tools/list":
        # Return full tool metadata from DB, not just names
        from sqlalchemy import select
        from gateway.database import MCPTool
        visible_names = [t for t in registry.all_tools() if t in allowed_tools]
        # Fetch descriptions and schemas from DB
        tools_out = []
        if visible_names:
            result = await db.execute(
                select(MCPTool).where(MCPTool.name.in_(visible_names))
            )
            tool_rows = {t.name: t for t in result.scalars().all()}
            for name in visible_names:
                row = tool_rows.get(name)
                tools_out.append({
                    "name": name,
                    "description": row.description if row else "",
                    "inputSchema": row.input_schema if row else {"type": "object"},
                })
        await _log(db, "tools/list", client.name, "—", 200, f"{len(tools_out)} tools visible")
        return JSONResponse(rpc_ok(rpc_id, {"tools": tools_out}))

    # ── tools/call ──────────────────────────────────────────────
    if method == "tools/call":
        params    = body.get("params", {})
        tool_name = params.get("name", "")
        print(f"Client '{client.name}' is calling tool '{tool_name}'")
        print(f"Allowed tools for this client: {allowed_tools}")
        if tool_name not in allowed_tools:
            await _log(db, "tools/call", client.name, tool_name, 403, "Access denied")
            return JSONResponse(rpc_error(rpc_id, -32603, f"Access denied: '{tool_name}'"), status_code=403)

        server_url = registry.get_server_url(tool_name)
        if not server_url:
            await _log(db, "tools/call", client.name, tool_name, 404, "Tool not in registry")
            return JSONResponse(rpc_error(rpc_id, -32601, f"Tool not found: '{tool_name}'"), status_code=404)

        upstream_key = registry.get_server_key(tool_name) or ""

        # Build upstream headers — include Accept for FastMCP compatibility
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if upstream_key:
            headers["Authorization"] = f"Bearer {upstream_key}"

        # Attach session ID for FastMCP servers (no-op for plain JSON servers)
        sid = await _get_session_id(server_url, upstream_key)
        if sid:
            headers["Mcp-Session-Id"] = sid

        # Strip our prefix from the tool name before forwarding
        # upstream expects its own bare name (e.g. "add" not "myserver__add")
        forward_body = dict(body)
        if "params" in forward_body and "name" in forward_body["params"]:
            raw_name = tool_name.split("__", 1)[1] if "__" in tool_name else tool_name
            forward_body["params"] = dict(forward_body["params"])
            forward_body["params"]["name"] = raw_name

        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                resp = await http_client.post(
                    server_url.rstrip("/") + "/mcp",
                    json=forward_body,
                    headers=headers,
                )

            # If session expired (401/404), clear cache and retry once
            if resp.status_code in (401, 404) and sid:
                _session_cache.pop(server_url, None)
                new_sid = await _get_session_id(server_url, upstream_key)
                if new_sid:
                    headers["Mcp-Session-Id"] = new_sid
                    async with httpx.AsyncClient(timeout=30.0) as http_client:
                        resp = await http_client.post(
                            server_url.rstrip("/") + "/mcp",
                            json=forward_body,
                            headers=headers,
                        )

            result = _parse_upstream_response(resp)
            await _log(db, "tools/call", client.name, tool_name, resp.status_code)
            return JSONResponse(result)

        except httpx.TimeoutException:
            await _log(db, "tools/call", client.name, tool_name, 504, "Upstream timeout")
            return JSONResponse(rpc_error(rpc_id, -32000, "Upstream server timed out"), status_code=504)

        except Exception as e:
            logger.error(f"Upstream error for {tool_name}: {e}")
            await _log(db, "tools/call", client.name, tool_name, 502, str(e))
            return JSONResponse(rpc_error(rpc_id, -32000, "Upstream server error"), status_code=502)

    # ── unknown method ───────────────────────────────────────────
    await _log(db, method, client.name, "—", 404, "Unknown method")
    return JSONResponse(rpc_error(rpc_id, -32601, f"Method not found: {method}"), status_code=404)



def rpc_error(id_, code: int, message: str):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def rpc_ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}


async def _log(db: AsyncSession, method: str, client_name: str, tool: str, status: int, detail: str = ""):
    entry = ActivityLog(
        method=method, client_name=client_name,
        tool=tool, status=status, detail=detail
    )
    db.add(entry)
    await db.commit()


@router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    auth=Depends(get_client_by_key),
    db: AsyncSession = Depends(get_db),
):
    client, allowed_tools = auth
    body = await request.json()

    method = body.get("method", "")
    rpc_id = body.get("id")

    # ── initialize ──────────────────────────────────────────
    if method == "initialize":
        await _log(db, "initialize", client.name, "—", 200)
        return JSONResponse(rpc_ok(rpc_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mcp-gateway", "version": "1.0.0"}
        }))

    # ── tools/list ──────────────────────────────────────────
    if method == "tools/list":
        visible = [t for t in registry.all_tools() if t in allowed_tools]
        await _log(db, "tools/list", client.name, "—", 200, f"{len(visible)} tools visible")
        return JSONResponse(rpc_ok(rpc_id, {
            "tools": [{"name": t, "description": "", "inputSchema": {"type": "object"}} for t in visible]
        }))

    # ── tools/call ──────────────────────────────────────────
    if method == "tools/call":
        params    = body.get("params", {})
        tool_name = params.get("name", "")

        if tool_name not in allowed_tools:
            await _log(db, "tools/call", client.name, tool_name, 403, "Access denied")
            return JSONResponse(rpc_error(rpc_id, -32603, f"Access denied: '{tool_name}'"), status_code=403)

        server_url = registry.get_server_url(tool_name)
        if not server_url:
            await _log(db, "tools/call", client.name, tool_name, 404, "Tool not in registry")
            return JSONResponse(rpc_error(rpc_id, -32601, f"Tool not found: '{tool_name}'"), status_code=404)

        # Forward to upstream
        headers = {"Content-Type": "application/json"}
        upstream_key = registry.get_server_key(tool_name)
        if upstream_key:
            headers["Authorization"] = f"Bearer {upstream_key}"

        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                resp = await http_client.post(
                    f"{server_url}/mcp",
                    json=body,
                    headers=headers
                )
            result = resp.json()
            await _log(db, "tools/call", client.name, tool_name, resp.status_code)
            return JSONResponse(result, status_code=resp.status_code)

        except httpx.TimeoutException:
            await _log(db, "tools/call", client.name, tool_name, 504, "Upstream timeout")
            return JSONResponse(rpc_error(rpc_id, -32000, "Upstream server timed out"), status_code=504)

        except Exception as e:
            logger.error(f"Upstream error for {tool_name}: {e}")
            await _log(db, "tools/call", client.name, tool_name, 502, str(e))
            return JSONResponse(rpc_error(rpc_id, -32000, "Upstream server error"), status_code=502)

    # ── unknown method ───────────────────────────────────────
    await _log(db, method, client.name, "—", 404, "Unknown method")
    return JSONResponse(rpc_error(rpc_id, -32601, f"Method not found: {method}"), status_code=404)