from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List, Optional
from datetime import UTC, datetime
import logging

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    AgentSession, Entity, Project, AgentSessionStatus, AgentApproval, AgentApprovalStatus,
)
from agent_kanban_pm.schemas import (
    AgentApprovalCreate, AgentApprovalResolve, AgentApprovalResponse,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/approvals", response_model=AgentApprovalResponse, status_code=status.HTTP_201_CREATED)
async def request_agent_approval(
    payload: AgentApprovalCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create an approval request from a CLI agent or supervisor."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    project_result = await db.execute(select(Project).filter(Project.id == payload.project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    agent_id = payload.agent_id or current_entity.id
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only request approval for yourself"
        )

    from agent_kanban_pm.services.coordination import validate_approval_scope
    task_id = await validate_approval_scope(db, payload.project_id, payload.task_id, payload.session_id, agent_id)
    approval = AgentApproval(
        project_id=payload.project_id,
        task_id=task_id,
        session_id=payload.session_id,
        agent_id=agent_id,
        approval_type=payload.approval_type,
        title=payload.title,
        message=payload.message,
        command=payload.command,
        diff_content=payload.diff_content,
        payload_json=payload.payload_json,
        status=AgentApprovalStatus.PENDING,
    )
    db.add(approval)
    await db.flush()
    await db.refresh(approval)

    if payload.session_id:
        sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == payload.session_id))
        session_row = sess_result.scalar_one_or_none()
        if session_row and session_row.status != AgentSessionStatus.BLOCKED:
            session_row.status = AgentSessionStatus.BLOCKED
            session_row.last_seen_at = datetime.now(UTC)

    event_bus.enqueue(db,
        EventType.AGENT_APPROVAL_REQUESTED.value,
        {
            "approval_id": approval.id,
            "project_id": approval.project_id,
            "task_id": approval.task_id,
            "session_id": approval.session_id,
            "agent_id": approval.agent_id,
            "approval_type": approval.approval_type.value,
            "title": approval.title,
            "message": approval.message,
            "command": approval.command,
        },
        project_id=approval.project_id,
        entity_id=approval.agent_id
    )
    await db.commit()
    return approval


@router.get("/approvals", response_model=List[AgentApprovalResponse])
async def list_agent_approvals(
    request: Request,
    project_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    task_id: Optional[int] = None,
    session_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """List approval queue entries with offset-based pagination.

    Use status_filter=pending for the workbench tab.
    """
    has_explicit_identity = bool(request.headers.get("x-entity-id"))
    if not current_entity or not has_explicit_identity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    entity_id_header = request.headers.get("x-entity-id")
    if entity_id_header:
        try:
            if int(entity_id_header) != current_entity.id:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid identity")
        except ValueError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid identity")

    query = select(AgentApproval).order_by(desc(AgentApproval.requested_at))
    if project_id is not None:
        query = query.filter(AgentApproval.project_id == project_id)
    if agent_id is not None:
        query = query.filter(AgentApproval.agent_id == agent_id)
    if task_id is not None:
        query = query.filter(AgentApproval.task_id == task_id)
    if session_id is not None:
        query = query.filter(AgentApproval.session_id == session_id)
    if status_filter:
        try:
            query = query.filter(AgentApproval.status == AgentApprovalStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid status: {status_filter}")

    if not is_owner_or_manager(current_entity):
        query = query.filter(AgentApproval.agent_id == current_entity.id)

    result = await db.execute(query.offset(offset).limit(limit))
    return result.scalars().all()


@router.patch("/approvals/{approval_id}/resolve", response_model=AgentApprovalResponse)
async def resolve_agent_approval(
    approval_id: int,
    payload: AgentApprovalResolve,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Approve, reject, or cancel a pending approval request."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if payload.decision not in (
        AgentApprovalStatus.APPROVED,
        AgentApprovalStatus.REJECTED,
        AgentApprovalStatus.CANCELLED,
    ):
        raise HTTPException(status_code=422, detail="decision must be approved, rejected, or cancelled")

    result = await db.execute(select(AgentApproval).filter(AgentApproval.id == approval_id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != AgentApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Approval already {approval.status.value}")

    if payload.decision == AgentApprovalStatus.CANCELLED:
        if not is_owner_or_manager(current_entity) and current_entity.id != approval.agent_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the requester or a manager can cancel")
    else:
        if not is_owner_or_manager(current_entity):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can approve or reject approvals")

    # Optimistic concurrency: CAS on update_version to prevent race conditions.
    # Three independent paths can resolve the same approval concurrently
    # (REST, MCP, session streamer). This ensures only one succeeds.
    expected_version = approval.update_version
    from sqlalchemy import update as sa_update
    cas_result = await db.execute(
        sa_update(AgentApproval)
        .where(AgentApproval.id == approval_id, AgentApproval.update_version == expected_version)
        .values(
            status=payload.decision,
            resolved_at=datetime.now(UTC),
            resolved_by_entity_id=current_entity.id,
            response_message=payload.response_message,
            update_version=expected_version + 1,
        )
    )
    if cas_result.rowcount == 0:
        raise HTTPException(status_code=409, detail="Approval was resolved by another request (concurrent modification)")

    if approval.session_id:
        sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == approval.session_id))
        session_row = sess_result.scalar_one_or_none()
        if session_row and session_row.status == AgentSessionStatus.BLOCKED:
            session_row.status = AgentSessionStatus.ACTIVE
            session_row.last_seen_at = datetime.now(UTC)

    event_bus.enqueue(db,
        EventType.AGENT_APPROVAL_RESOLVED.value,
        {
            "approval_id": approval.id,
            "project_id": approval.project_id,
            "task_id": approval.task_id,
            "session_id": approval.session_id,
            "agent_id": approval.agent_id,
            "approval_type": approval.approval_type.value,
            "status": approval.status.value,
            "resolved_by_entity_id": current_entity.id,
            "response_message": approval.response_message,
        },
        project_id=approval.project_id,
        entity_id=approval.agent_id
    )
    await db.commit()
    await db.refresh(approval)
    return approval


# ---------------------------------------------------------------------------
# Stage Policy endpoints (AGENTS.md §11)
# ---------------------------------------------------------------------------
