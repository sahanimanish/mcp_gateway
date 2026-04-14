from fastapi import Header, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from gateway.database import get_db, Client, Permission
from typing import Optional


def _normalize_permission_type(permission_type: str) -> str:
    permission_type = (permission_type or "tool").strip().lower()
    if permission_type == "tool":
        return "tools"
    if permission_type == "prompt":
        return "prompts"
    if permission_type == "resource":
        return "resources"
    if permission_type in ("resource_template", "template"):
        return "resource_templates"
    return permission_type


async def get_client_by_key(
    x_api_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Validates X-Api-Key header.
    Returns (client, permissions_by_server, allowed_server_ids_set).
    Raises 401 if key missing/invalid or client inactive.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-Api-Key header")

    result = await db.execute(
        select(Client).where(Client.api_key == x_api_key)
    )
    client = result.scalar_one_or_none()

    if not client:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not client.is_active:
        raise HTTPException(status_code=403, detail="Client is deactivated")

    # Load allowed permissions
    perms = await db.execute(
        select(Permission).where(Permission.client_id == client.id)
    )
    permission_rows = perms.scalars().all()
    permissions_by_server = {}
    for permission in permission_rows:
        permission_type = _normalize_permission_type(permission.permission_type)
        permission_value = permission.permission_value or permission.tool_name
        if permission.server_id not in permissions_by_server:
            permissions_by_server[permission.server_id] = {
                "tools": set(),
                "prompts": set(),
                "resources": set(),
                "resource_templates": set(),
            }
        if permission_type in permissions_by_server[permission.server_id] and permission_value:
            permissions_by_server[permission.server_id][permission_type].add(permission_value)
    allowed_server_ids = {p.server_id for p in permission_rows}

    return client, permissions_by_server, allowed_server_ids


async def require_admin(x_admin_key: Optional[str] = Header(None)):
    """
    Simple admin key check for /admin/* routes.
    In production replace with proper JWT or OAuth.
    """
    # Default admin key — override via env var in production
    import os
    admin_key = os.getenv("ADMIN_KEY", "admin-secret-change-me")
    if x_admin_key != admin_key:
        raise HTTPException(status_code=401, detail="Invalid admin key")
