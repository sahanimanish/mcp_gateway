from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete
from sqlalchemy.orm import selectinload
from typing import List

from gateway.database import (
    get_db, MCPServer, MCPTool, MCPResource, MCPPrompt,
    Client, Permission, ActivityLog, new_id
)
from gateway.schemas import (
    ServerCreate, ServerUpdate, ServerOut, DiscoveryPreview,
    ClientCreate, ClientUpdate, ClientOut,
    PermissionSet, ClientPermsSummary,
    LogOut, StatsOut,
)
from gateway import registry as reg

router = APIRouter(prefix="/admin", tags=["admin"])


# ── Helpers ──────────────────────────────────────────────────────

async def log_action(db, method, client_name, tool, status, detail=""):
    db.add(ActivityLog(
        method=method, client_name=client_name,
        tool=tool, status=status, detail=detail
    ))
    await db.commit()


def _server_load_opts():
    return (
        selectinload(MCPServer.tools),
        selectinload(MCPServer.resources),
        selectinload(MCPServer.prompts),
    )


async def _get_server_full(db, server_id):
    result = await db.execute(
        select(MCPServer)
        .options(*_server_load_opts())
        .where(MCPServer.id == server_id)
    )
    return result.scalar_one_or_none()


# ══════════════════════════════════════════════════════════════
# STATS
# ══════════════════════════════════════════════════════════════

@router.get("/stats", response_model=StatsOut)
async def get_stats(db: AsyncSession = Depends(get_db)):
    return StatsOut(
        servers     = (await db.execute(select(func.count(MCPServer.id)))).scalar(),
        clients     = (await db.execute(select(func.count(Client.id)))).scalar(),
        tools       = (await db.execute(select(func.count(MCPTool.id)))).scalar(),
        resources   = (await db.execute(select(func.count(MCPResource.id)))).scalar(),
        prompts     = (await db.execute(select(func.count(MCPPrompt.id)))).scalar(),
        permissions = (await db.execute(select(func.count(Permission.id)))).scalar(),
    )


# ══════════════════════════════════════════════════════════════
# SERVER — PREVIEW (test connection before registering)
# ══════════════════════════════════════════════════════════════

@router.post("/servers/preview", response_model=DiscoveryPreview)
async def preview_server(body: ServerCreate):
    """
    Probe a server URL and return what it exposes — without saving anything.
    The UI calls this first to show the user what will be discovered.
    """
    result = await reg.discover(body.url, body.name, body.upstream_key or "")

    return DiscoveryPreview(
        reachable        = result.success,
        error            = result.error,
        protocol_version = result.protocol_version,
        server_info      = result.server_info,
        capabilities     = result.capabilities,
        tool_count       = len(result.tools),
        resource_count   = len(result.resources),
        prompt_count     = len(result.prompts),
        tools     = [{"name": t.raw_name, "description": t.description} for t in result.tools],
        resources = [{"uri": r.uri, "name": r.name, "mimeType": r.mime_type} for r in result.resources],
        prompts   = [{"name": p.name, "description": p.description} for p in result.prompts],
    )


# ══════════════════════════════════════════════════════════════
# SERVER — REGISTER
# ══════════════════════════════════════════════════════════════

@router.get("/servers", response_model=List[ServerOut])
async def list_servers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MCPServer)
        .options(*_server_load_opts())
        .order_by(MCPServer.created_at)
    )
    return result.scalars().all()


@router.post("/servers", response_model=ServerOut, status_code=201)
async def create_server(body: ServerCreate, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(
        select(MCPServer).where(MCPServer.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Server '{body.name}' already registered")
    
    existing = (await db.execute(
        select(MCPServer).where(MCPServer.url == body.url.rstrip("/"))
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Server '{body.url}' already registered")

    # Create server record first
    server = MCPServer(
        id           = new_id(),
        name         = body.name,
        url          = body.url.rstrip("/"),
        description  = body.description or "",
        upstream_key = body.upstream_key or "",
        status       = "unknown",
    )
    db.add(server)
    await db.flush()

    # Run full MCP discovery
    discovery = await reg.discover(server.url, server.name, server.upstream_key)
    await reg.save_discovery(db, server, discovery)

    if not discovery.success:
        # Still save the server — it might come online later
        server.status = "offline"
        await db.commit()

    await log_action(
        db, "server/register", "admin", "—", 201,
        f"Registered '{server.name}': {len(discovery.tools)} tools, "
        f"{len(discovery.resources)} resources, {len(discovery.prompts)} prompts"
        + (f" — WARNING: {discovery.error}" if not discovery.success else "")
    )

    srv = await _get_server_full(db, server.id)
    return srv


@router.patch("/servers/{server_id}", response_model=ServerOut)
async def update_server(server_id: str, body: ServerUpdate, db: AsyncSession = Depends(get_db)):
    server = (await db.execute(
        select(MCPServer).where(MCPServer.id == server_id)
    )).scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    if body.url is not None:          server.url = body.url.rstrip("/")
    if body.description is not None:  server.description = body.description
    if body.upstream_key is not None: server.upstream_key = body.upstream_key
    if body.status is not None:       server.status = body.status

    await db.commit()
    return await _get_server_full(db, server_id)


@router.delete("/servers/{server_id}", status_code=204)
async def delete_server(server_id: str, db: AsyncSession = Depends(get_db)):
    server = (await db.execute(
        select(MCPServer).where(MCPServer.id == server_id)
    )).scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    reg.unregister_server(server.name)
    await db.execute(delete(Permission).where(Permission.server_id == server_id))
    await db.execute(delete(MCPTool).where(MCPTool.server_id == server_id))
    await db.execute(delete(MCPResource).where(MCPResource.server_id == server_id))
    await db.execute(delete(MCPPrompt).where(MCPPrompt.server_id == server_id))
    await db.delete(server)
    await db.commit()
    await log_action(db, "server/delete", "admin", "—", 200, f"Deleted {server.name}")


@router.post("/servers/{server_id}/refresh", response_model=ServerOut)
async def refresh_server(server_id: str, db: AsyncSession = Depends(get_db)):
    """Re-run full MCP discovery against an already-registered server."""
    server = (await db.execute(
        select(MCPServer).where(MCPServer.id == server_id)
    )).scalar_one_or_none()
    if not server:
        raise HTTPException(404, "Server not found")

    discovery = await reg.discover(server.url, server.name, server.upstream_key or "")
    await reg.save_discovery(db, server, discovery)

    await log_action(
        db, "server/refresh", "admin", "—", 200,
        f"Refreshed '{server.name}': {len(discovery.tools)} tools, "
        f"{len(discovery.resources)} resources, {len(discovery.prompts)} prompts"
    )
    return await _get_server_full(db, server_id)


# ══════════════════════════════════════════════════════════════
# CLIENTS
# ══════════════════════════════════════════════════════════════

@router.get("/clients", response_model=List[ClientOut])
async def list_clients(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Client).order_by(Client.created_at))
    return result.scalars().all()


@router.post("/clients", response_model=ClientOut, status_code=201)
async def create_client(body: ClientCreate, db: AsyncSession = Depends(get_db)):
    if (await db.execute(select(Client).where(Client.api_key == body.api_key))).scalar_one_or_none():
        raise HTTPException(409, "API key already in use")
    client = Client(id=new_id(), name=body.name, description=body.description or "", api_key=body.api_key)
    db.add(client)
    await db.commit()
    await db.refresh(client)
    await log_action(db, "client/add", "admin", "—", 201, f"Added client {body.name}")
    return client


@router.patch("/clients/{client_id}", response_model=ClientOut)
async def update_client(client_id: str, body: ClientUpdate, db: AsyncSession = Depends(get_db)):
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Client not found")
    if body.name is not None:        client.name = body.name
    if body.description is not None: client.description = body.description
    if body.is_active is not None:   client.is_active = body.is_active
    await db.commit()
    await db.refresh(client)
    return client


@router.delete("/clients/{client_id}", status_code=204)
async def delete_client(client_id: str, db: AsyncSession = Depends(get_db)):
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Client not found")
    await db.execute(delete(Permission).where(Permission.client_id == client_id))
    await db.delete(client)
    await db.commit()
    await log_action(db, "client/delete", "admin", "—", 200, f"Deleted {client.name}")


# ══════════════════════════════════════════════════════════════
# PERMISSIONS
# ══════════════════════════════════════════════════════════════

@router.get("/clients/{client_id}/permissions", response_model=ClientPermsSummary)
async def get_permissions(client_id: str, db: AsyncSession = Depends(get_db)):
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Client not found")
    perms = (await db.execute(select(Permission).where(Permission.client_id == client_id))).scalars().all()
    grouped: dict[str, list] = {}
    for p in perms:
        grouped.setdefault(p.server_id, []).append(p.tool_name)
    return ClientPermsSummary(client_id=client_id, client_name=client.name, permissions=grouped)


@router.put("/clients/{client_id}/permissions", response_model=ClientPermsSummary)
async def set_permissions(client_id: str, body: PermissionSet, db: AsyncSession = Depends(get_db)):
    client = (await db.execute(select(Client).where(Client.id == client_id))).scalar_one_or_none()
    if not client:
        raise HTTPException(404, "Client not found")

    await db.execute(delete(Permission).where(Permission.client_id == client_id))

    count = 0
    for server_id, tools in body.permissions.items():
        server = (await db.execute(select(MCPServer).where(MCPServer.id == server_id))).scalar_one_or_none()
        if not server:
            continue
        for tool_name in tools:
            db.add(Permission(id=new_id(), client_id=client_id, server_id=server_id, tool_name=tool_name))
            count += 1

    await db.commit()
    await log_action(db, "permission/save", "admin", "—", 200, f"Set {count} permissions for {client.name}")
    return ClientPermsSummary(client_id=client_id, client_name=client.name, permissions=body.permissions)


@router.get("/permissions/matrix")
async def permissions_matrix(db: AsyncSession = Depends(get_db)):
    clients = (await db.execute(select(Client).order_by(Client.name))).scalars().all()
    servers = (await db.execute(
        select(MCPServer).options(*_server_load_opts()).order_by(MCPServer.name)
    )).scalars().all()
    all_perms = (await db.execute(select(Permission))).scalars().all()

    perm_index: dict[str, dict[str, list]] = {}
    for p in all_perms:
        perm_index.setdefault(p.client_id, {}).setdefault(p.server_id, []).append(p.tool_name)

    return {
        "clients": [{"id": c.id, "name": c.name} for c in clients],
        "servers": [
            {
                "id": s.id, "name": s.name,
                "tools": [t.name for t in s.tools],
                "resources": [r.uri for r in s.resources],
                "prompts": [p.name for p in s.prompts],
            }
            for s in servers
        ],
        "permissions": {
            cid: {sid: tools for sid, tools in smap.items()}
            for cid, smap in perm_index.items()
        }
    }


# ══════════════════════════════════════════════════════════════
# LOGS
# ══════════════════════════════════════════════════════════════

@router.get("/logs", response_model=List[LogOut])
async def get_logs(limit: int = 100, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ActivityLog).order_by(ActivityLog.timestamp.desc()).limit(limit)
    )
    return result.scalars().all()


@router.delete("/logs", status_code=204)
async def clear_logs(db: AsyncSession = Depends(get_db)):
    await db.execute(delete(ActivityLog))
    await db.commit()


# ══════════════════════════════════════════════════════════════
# EXPORT
# ══════════════════════════════════════════════════════════════

@router.get("/export")
async def export_config(db: AsyncSession = Depends(get_db)):
    servers = (await db.execute(
        select(MCPServer).options(*_server_load_opts())
    )).scalars().all()
    clients = (await db.execute(select(Client))).scalars().all()
    all_perms = (await db.execute(select(Permission))).scalars().all()

    perm_index: dict[str, dict[str, list]] = {}
    for p in all_perms:
        perm_index.setdefault(p.client_id, {}).setdefault(p.server_id, []).append(p.tool_name)

    srv_map = {s.id: s.name for s in servers}

    return {
        "servers": [
            {
                "name": s.name, "url": s.url, "description": s.description,
                "protocol_version": s.protocol_version,
                "server_info": s.server_info,
                "tools":     [{"name": t.raw_name, "description": t.description} for t in s.tools],
                "resources": [{"uri": r.uri, "name": r.name} for r in s.resources],
                "prompts":   [{"name": p.name, "description": p.description} for p in s.prompts],
            }
            for s in servers
        ],
        "clients": [
            {
                "name": c.name, "api_key": c.api_key,
                "permissions": [
                    {"server": srv_map.get(sid, sid), "tools": tools}
                    for sid, tools in perm_index.get(c.id, {}).items()
                ]
            }
            for c in clients
        ]
    }
