from fastapi import Header, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from gateway.database import get_db, Client, Permission
from typing import Optional


async def get_client_by_key(
    x_api_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Validates X-Api-Key header.
    Returns (client, allowed_tools_set, allowed_server_ids_set).
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

    # Load allowed tools
    perms = await db.execute(
        select(Permission).where(Permission.client_id == client.id)
    )
    permission_rows = perms.scalars().all()
    allowed_tools = {p.tool_name for p in permission_rows}
    allowed_server_ids = {p.server_id for p in permission_rows}

    return client, allowed_tools, allowed_server_ids


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
