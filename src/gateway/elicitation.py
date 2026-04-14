"""
elicitation.py — MCP Elicitation handler

When an upstream server calls elicitation/create (mid tool-call), the gateway
acts as the MCP *client* and must:

  1. Accept the elicitation request from the server
  2. Queue it so the UI can display a form to the user
  3. Block the in-progress tool call until the user responds
  4. Return {action, content} back to the server so it can resume

Flow:
  upstream server
    → elicitation/create {message, requestedSchema}
    → gateway intercepts this mid-stream
    → creates ElicitationRequest, stores in pending queue
    → downstream client polls GET /elicitation/pending
    → user fills form, submits POST /elicitation/{id}/respond
    → asyncio.Future resolves, gateway returns response to server
    → server resumes tool execution, returns final result to gateway client

The schema is deliberately flat (MCP spec): object with only primitive properties
(string, number, boolean, enum). No nesting. This makes UI rendering trivial.
"""

from __future__ import annotations
import asyncio
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("elicitation")

router = APIRouter(prefix="/elicitation", tags=["elicitation"])


# ── Data models ──────────────────────────────────────────────────

class ElicitationRequest(BaseModel):
    id:               str
    server_name:      str
    client_name:      str
    tool_name:        str
    message:          str
    requested_schema: dict
    created_at:       datetime
    status:           Literal["pending", "responded", "expired"] = "pending"


class ElicitationResponse(BaseModel):
    action:  Literal["accept", "decline", "cancel"]
    content: Optional[dict] = None


# ── In-memory pending queue ──────────────────────────────────────

_pending: dict[str, tuple[ElicitationRequest, asyncio.Future]] = {}


def create_pending(
    server_name: str,
    client_name: str,
    tool_name: str,
    message: str,
    requested_schema: dict,
) -> tuple[str, asyncio.Future]:
    elicit_id = str(uuid.uuid4())[:12]
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()
    req = ElicitationRequest(
        id=elicit_id, server_name=server_name, client_name=client_name,
        tool_name=tool_name, message=message, requested_schema=requested_schema,
        created_at=datetime.now(timezone.utc), status="pending",
    )
    _pending[elicit_id] = (req, fut)
    logger.info(f"Elicitation queued: {elicit_id} from {server_name} ({tool_name})")
    return elicit_id, fut


def resolve(elicit_id: str, response: ElicitationResponse) -> bool:
    entry = _pending.get(elicit_id)
    if not entry:
        return False
    req, fut = entry
    req.status = "responded"
    if not fut.done():
        fut.set_result(response)
    logger.info(f"Elicitation {elicit_id} resolved: action={response.action}")
    return True


# ── REST endpoints ────────────────────────────────────────────────

@router.get("/pending")
async def list_pending() -> list[ElicitationRequest]:
    """Poll endpoint — UI calls this every ~2s to get pending forms."""
    return [req for req, _ in _pending.values() if req.status == "pending"]


@router.post("/{elicit_id}/respond")
async def respond(elicit_id: str, body: ElicitationResponse):
    """Submit user's form response — unblocks the in-progress tool call."""
    if elicit_id not in _pending:
        raise HTTPException(404, "Elicitation not found or already resolved")
    if not resolve(elicit_id, body):
        raise HTTPException(409, "Already resolved")

    async def _cleanup():
        await asyncio.sleep(5)
        _pending.pop(elicit_id, None)
    asyncio.create_task(_cleanup())

    return {"ok": True, "action": body.action}


@router.delete("/{elicit_id}")
async def cancel_elicitation(elicit_id: str):
    """Cancel a pending elicitation (user closed the form)."""
    resolve(elicit_id, ElicitationResponse(action="cancel"))
    _pending.pop(elicit_id, None)
    return {"ok": True}


@router.get("/stats")
async def elicitation_stats():
    total   = len(_pending)
    pending = sum(1 for r, _ in _pending.values() if r.status == "pending")
    return {"total": total, "pending": pending}
