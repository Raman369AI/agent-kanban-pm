from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import Optional
from datetime import UTC, datetime, timedelta
from pydantic import BaseModel, Field
import asyncio
import logging

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    AgentSession, Entity, Task, AgentSessionStatus, TaskLease, LeaseStatus, LaunchRequest,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter()


class LaunchQueueAction(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=1000)


def _launch_request_payload(row: LaunchRequest) -> dict:
    return {
        "id": row.id, "event_id": row.event_id, "actor_id": row.actor_id,
        "task_id": row.task_id, "agent_id": row.agent_id, "role": row.role,
        "source_session_id": row.source_session_id, "stage_id": row.stage_id,
        "status": row.status, "attempts": row.attempts,
        "retry_at": row.retry_at.isoformat() if row.retry_at else None,
        "last_error": row.last_error, "session_id": row.session_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
    }


@router.get("/launch-requests")
async def list_launch_requests(
    project_id: Optional[int] = None, status_filter: Optional[str] = None,
    include_archived: bool = False, limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    if not current_entity or not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=403, detail="Manager access required")
    query = select(LaunchRequest).join(Task, Task.id == LaunchRequest.task_id)
    if project_id is not None:
        query = query.where(Task.project_id == project_id)
    if status_filter:
        query = query.where(LaunchRequest.status == status_filter)
    if not include_archived:
        query = query.where(LaunchRequest.archived_at.is_(None))
    rows = list((await db.execute(query.order_by(desc(LaunchRequest.id)).limit(min(max(limit, 1), 500)))).scalars())
    return [_launch_request_payload(row) for row in rows]


@router.post("/launch-requests/{request_id}/cancel")
async def cancel_launch_request(
    request_id: int, action: LaunchQueueAction = LaunchQueueAction(),
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    if not current_entity or not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=403, detail="Manager access required")
    row = await db.get(LaunchRequest, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Launch request not found")
    session = await db.get(AgentSession, row.session_id) if row.session_id else None
    if session is None:
        session = await db.scalar(select(AgentSession).where(
            AgentSession.launch_request_id == row.id,
            AgentSession.ended_at.is_(None),
        ).limit(1))
    if session is not None and session.ended_at is None:
        from agent_kanban_pm.runtime.task_runner import receipt_state
        receipt = await asyncio.to_thread(receipt_state, session)
        if session.status != AgentSessionStatus.STARTING or receipt.status != "missing":
            raise HTTPException(status_code=409, detail="A started session must be stopped through session controls")
        now = datetime.now(UTC)
        session.status = AgentSessionStatus.ERROR
        session.ended_at = now
        session.last_seen_at = now
        session.handoff_summary = action.reason or f"Launch cancelled by {current_entity.name} before execution"
        row.session_id = session.id
        leases = list((await db.execute(select(TaskLease).where(
            TaskLease.session_id == session.id,
            TaskLease.status == LeaseStatus.ACTIVE,
        ))).scalars())
        for lease in leases:
            lease.status = LeaseStatus.RELEASED
            lease.released_at = now
    elif row.status in {"starting", "started", "completed"}:
        raise HTTPException(status_code=409, detail="A started session must be stopped through session controls")
    row.status = "cancelled"
    row.last_error = action.reason or f"Cancelled by {current_entity.name}"
    row.retry_at = None
    row.claimed_at = None
    row.claim_token = None
    await db.commit()
    return _launch_request_payload(row)


@router.post("/launch-requests/{request_id}/retry")
async def retry_launch_request(
    request_id: int, action: LaunchQueueAction = LaunchQueueAction(),
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    if not current_entity or not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=403, detail="Manager access required")
    old = await db.get(LaunchRequest, request_id)
    if old is None:
        raise HTTPException(status_code=404, detail="Launch request not found")
    if old.status not in {"blocked", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only blocked, failed, or cancelled requests can be retried")
    if old.session_id is not None:
        session = await db.get(AgentSession, old.session_id)
        if session is not None and session.ended_at is None:
            raise HTTPException(status_code=409, detail="The prior session is still active")
    task = await db.get(Task, old.task_id)
    if task is None:
        raise HTTPException(status_code=409, detail="The task no longer exists")
    event = event_bus.enqueue(db, EventType.TASK_ASSIGNED.value, {
        "task_id": old.task_id, "entity_id": old.agent_id, "role": old.role,
        "stage_id": task.stage_id, "source_session_id": old.source_session_id,
    }, project_id=task.project_id, entity_id=current_entity.id)
    await db.flush()
    retried = LaunchRequest(
        event_id=event.id, actor_id=current_entity.id, task_id=old.task_id,
        agent_id=old.agent_id, role=old.role, source_session_id=old.source_session_id,
        stage_id=task.stage_id, status="queued",
        override_reason=action.reason,
        last_error=f"Retried from request #{old.id} by {current_entity.name}",
    )
    db.add(retried)
    await db.commit()
    await db.refresh(retried)
    return _launch_request_payload(retried)


@router.post("/launch-requests/cleanup")
async def cleanup_launch_requests(
    older_than_days: int = 30, db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    if not current_entity or not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=403, detail="Manager access required")
    cutoff = datetime.now(UTC) - timedelta(days=max(1, older_than_days))
    rows = list((await db.execute(select(LaunchRequest).where(
        LaunchRequest.status.in_(["completed", "failed", "cancelled"]),
        LaunchRequest.archived_at.is_(None), LaunchRequest.created_at < cutoff,
    ))).scalars())
    now = datetime.now(UTC)
    for row in rows:
        row.archived_at = now
    await db.commit()
    return {"archived": len(rows), "older_than_days": max(1, older_than_days)}

# ---------------------------------------------------------------------------
