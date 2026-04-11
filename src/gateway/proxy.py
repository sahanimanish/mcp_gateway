from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import httpx
import logging

from gateway.database import get_db, Client, ActivityLog, new_id
from gateway.auth import get_client_by_key
from gateway.schemas import JSONRPCRequest
from gateway import registry

logger = logging.getLogger("mcp_proxy")
router = APIRouter(tags=["mcp"])


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
