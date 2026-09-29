from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import UTC, datetime, timedelta
import logging

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Entity, Task, ProjectWorkspace,
    OrchestrationDecision, TaskLease, ActivitySummary, AgentCheckpoint, LeaseStatus,
)
from agent_kanban_pm.schemas import (
    ProjectWorkspaceCreate, ProjectWorkspaceResponse,
    OrchestrationDecisionCreate, OrchestrationDecisionResponse,
    TaskLeaseCreate, TaskLeaseResponse,
    ActivitySummaryCreate, ActivitySummaryResponse,
    AgentCheckpointCreate, AgentCheckpointResponse,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/projects/{project_id}/workspaces", response_model=List[ProjectWorkspaceResponse])
async def get_project_workspaces(
    project_id: int,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ProjectWorkspace)
        .filter(ProjectWorkspace.project_id == project_id)
        .order_by(desc(ProjectWorkspace.is_primary), ProjectWorkspace.created_at)
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/workspaces", response_model=ProjectWorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_project_workspace(
    project_id: int,
    workspace: ProjectWorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can add workspaces")
    if workspace.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")

    if workspace.is_primary:
        existing = await db.execute(select(ProjectWorkspace).filter(ProjectWorkspace.project_id == project_id))
        for item in existing.scalars().all():
            item.is_primary = False

    db_workspace = ProjectWorkspace(**workspace.model_dump())
    db.add(db_workspace)
    await db.commit()
    await db.refresh(db_workspace)
    return db_workspace


@router.get("/projects/{project_id}/decisions", response_model=List[OrchestrationDecisionResponse])
async def get_project_decisions(
    project_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(OrchestrationDecision)
        .filter(OrchestrationDecision.project_id == project_id)
        .order_by(desc(OrchestrationDecision.created_at))
        .limit(limit)
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/decisions", response_model=OrchestrationDecisionResponse, status_code=status.HTTP_201_CREATED)
async def log_project_decision(
    project_id: int,
    decision: OrchestrationDecisionCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can log orchestration decisions")
    if decision.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")

    db_decision = OrchestrationDecision(**decision.model_dump())
    if db_decision.manager_agent_id is None and current_entity.entity_type.value == "agent":
        db_decision.manager_agent_id = current_entity.id
    db.add(db_decision)
    await db.flush()
    event_bus.enqueue(db,
        EventType.ORCHESTRATION_DECISION_LOGGED.value,
        {
            "decision_id": db_decision.id,
            "project_id": project_id,
            "decision_type": db_decision.decision_type.value,
            "rationale": db_decision.rationale,
            "affected_task_ids": db_decision.affected_task_ids,
            "affected_agent_ids": db_decision.affected_agent_ids,
        },
        project_id=project_id,
        entity_id=current_entity.id
    )
    await db.commit()
    await db.refresh(db_decision)
    return db_decision


@router.get("/projects/{project_id}/summaries", response_model=List[ActivitySummaryResponse])
async def get_project_summaries(
    project_id: int,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ActivitySummary)
        .filter(ActivitySummary.project_id == project_id)
        .order_by(desc(ActivitySummary.created_at))
        .limit(limit)
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/summaries", response_model=ActivitySummaryResponse, status_code=status.HTTP_201_CREATED)
async def create_activity_summary(
    project_id: int,
    summary: ActivitySummaryCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if summary.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")
    if summary.agent_id is not None and not is_owner_or_manager(current_entity) and summary.agent_id != current_entity.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only summarize your own activity")

    db_summary = ActivitySummary(**summary.model_dump())
    db.add(db_summary)
    await db.flush()
    event_bus.enqueue(db,
        EventType.ACTIVITY_SUMMARY_CREATED.value,
        {"summary_id": db_summary.id, "project_id": project_id, "summary": db_summary.summary},
        project_id=project_id,
        entity_id=current_entity.id
    )
    await db.commit()
    await db.refresh(db_summary)
    return db_summary


@router.get("/tasks/{task_id}/checkpoints", response_model=List[AgentCheckpointResponse])
async def get_task_checkpoints(
    task_id: int,
    agent_id: Optional[int] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Return durable resume checkpoints for a task."""
    query = (
        select(AgentCheckpoint)
        .filter(AgentCheckpoint.task_id == task_id)
        .order_by(desc(AgentCheckpoint.updated_at))
    )
    if agent_id:
        query = query.filter(AgentCheckpoint.agent_id == agent_id)
    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.post("/tasks/{task_id}/checkpoints", response_model=AgentCheckpointResponse, status_code=status.HTTP_201_CREATED)
async def create_task_checkpoint(
    task_id: int,
    checkpoint: AgentCheckpointCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Create or update a durable resume checkpoint for a task session."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if checkpoint.task_id != task_id:
        raise HTTPException(status_code=422, detail="Path task_id and body task_id must match")

    agent_id = checkpoint.agent_id or current_entity.id
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only checkpoint your own work")

    task_result = await db.execute(select(Task).filter(Task.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.project_id != checkpoint.project_id:
        raise HTTPException(status_code=422, detail="Checkpoint project_id does not match task")

    existing_result = await db.execute(
        select(AgentCheckpoint)
        .filter(
            AgentCheckpoint.task_id == task_id,
            AgentCheckpoint.agent_id == agent_id,
            AgentCheckpoint.session_id == checkpoint.session_id,
        )
        .order_by(desc(AgentCheckpoint.updated_at))
        .limit(1)
    )
    db_checkpoint = existing_result.scalar_one_or_none()
    now = datetime.now(UTC)
    if db_checkpoint:
        db_checkpoint.workspace_path = checkpoint.workspace_path
        db_checkpoint.summary = checkpoint.summary
        db_checkpoint.terminal_tail = checkpoint.terminal_tail
        db_checkpoint.payload_json = checkpoint.payload_json
        db_checkpoint.updated_at = now
    else:
        db_checkpoint = AgentCheckpoint(
            agent_id=agent_id,
            project_id=checkpoint.project_id,
            task_id=task_id,
            session_id=checkpoint.session_id,
            workspace_path=checkpoint.workspace_path,
            summary=checkpoint.summary,
            terminal_tail=checkpoint.terminal_tail,
            payload_json=checkpoint.payload_json,
            created_at=now,
            updated_at=now,
        )
        db.add(db_checkpoint)

    await db.commit()
    await db.refresh(db_checkpoint)
    return db_checkpoint


@router.get("/projects/{project_id}/leases", response_model=List[TaskLeaseResponse])
async def get_project_leases(
    project_id: int,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db)
):
    now = datetime.now(UTC)
    query = (
        select(TaskLease)
        .join(Task, Task.id == TaskLease.task_id)
        .filter(Task.project_id == project_id)
        .order_by(desc(TaskLease.created_at))
    )
    if active_only:
        query = query.filter(and_(TaskLease.status == LeaseStatus.ACTIVE, TaskLease.expires_at > now))
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/tasks/{task_id}/lease", response_model=TaskLeaseResponse, status_code=status.HTTP_201_CREATED)
async def claim_task_lease(
    task_id: int,
    lease: TaskLeaseCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    agent_id = lease.agent_id or current_entity.id
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only claim your own lease")
    if lease.task_id != task_id:
        raise HTTPException(status_code=422, detail="Path task_id and body task_id must match")

    task_result = await db.execute(select(Task).filter(Task.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    now = datetime.now(UTC)
    active_result = await db.execute(
        select(TaskLease).filter(
            TaskLease.task_id == task_id,
            TaskLease.status == LeaseStatus.ACTIVE,
            TaskLease.expires_at > now,
            TaskLease.agent_id != agent_id,
        )
    )
    active = active_result.scalar_one_or_none()
    if active:
        raise HTTPException(status_code=409, detail=f"Task is already leased by agent {active.agent_id}")

    # Release this agent's older leases on the same task.
    existing_result = await db.execute(
        select(TaskLease).filter(TaskLease.task_id == task_id, TaskLease.agent_id == agent_id, TaskLease.status == LeaseStatus.ACTIVE)
    )
    for existing in existing_result.scalars().all():
        existing.status = LeaseStatus.RELEASED
        existing.released_at = now
    await db.flush()

    db_lease = TaskLease(
        task_id=task_id,
        agent_id=agent_id,
        session_id=lease.session_id,
        status=LeaseStatus.ACTIVE,
        expires_at=now + timedelta(seconds=max(60, lease.ttl_seconds)),
    )
    db.add(db_lease)
    await db.flush()
    event_bus.enqueue(db,
        EventType.TASK_LEASE_UPDATED.value,
        {
            "lease_id": db_lease.id,
            "task_id": task_id,
            "agent_id": agent_id,
            "session_id": lease.session_id,
            "status": db_lease.status.value,
            "expires_at": db_lease.expires_at.isoformat(),
        },
        project_id=task.project_id,
        entity_id=agent_id
    )
    await db.commit()
    await db.refresh(db_lease)
    return db_lease


@router.patch("/leases/{lease_id}/release", response_model=TaskLeaseResponse)
async def release_task_lease(
    lease_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    result = await db.execute(select(TaskLease).filter(TaskLease.id == lease_id).options(selectinload(TaskLease.task)))
    lease = result.scalar_one_or_none()
    if not lease:
        raise HTTPException(status_code=404, detail="Lease not found")
    if not is_owner_or_manager(current_entity) and current_entity.id != lease.agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only release your own lease")

    lease.status = LeaseStatus.RELEASED
    lease.released_at = datetime.now(UTC)
    event_bus.enqueue(db,
        EventType.TASK_LEASE_UPDATED.value,
        {"lease_id": lease.id, "task_id": lease.task_id, "agent_id": lease.agent_id, "status": lease.status.value},
        project_id=lease.task.project_id if lease.task else None,
        entity_id=lease.agent_id
    )
    await db.commit()
    await db.refresh(lease)
    return lease
