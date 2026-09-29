from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, update as sa_update
from sqlalchemy.exc import IntegrityError
from typing import List, Optional, Literal
from datetime import UTC, datetime
from pydantic import BaseModel, Field
import logging
import json
import uuid

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    AgentActivity, AgentSession, Entity, Project, ActivityType, AgentSessionStatus,
)
from agent_kanban_pm.schemas import (
    AgentSessionCreate, AgentSessionUpdate, AgentSessionResponse,
    AgentTerminalResponse,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.runtime.handoff_protocol import read_status_file, status_matches_session, status_identity_matches_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/sessions/{session_id}/terminal", response_model=AgentTerminalResponse)
async def get_agent_terminal(
    session_id: int,
    limit: int = 200,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    activity_result = await db.execute(
        select(AgentActivity)
        .filter(AgentActivity.session_id == session_id)
        .order_by(AgentActivity.id.desc())
        .limit(limit)
    )
    # Fetch the newest entries, then display them in reading order. Taking the
    # oldest rows made a busy agent's terminal appear to stop updating.
    activities = list(reversed(activity_result.scalars().all()))
    return {"session": session, "activities": activities}


class SessionHandoffSubmit(BaseModel):
    project_id: int
    task_id: int
    run_token: str = Field(min_length=1)
    state: Literal["done", "completed", "review"]
    summary: str = Field(min_length=1, max_length=4000)
    outputs: list[str] = Field(default_factory=list)
    artifacts: list[dict] = Field(default_factory=list)


@router.post("/sessions/{session_id}/handoff")
async def submit_agent_session_handoff(
    session_id: int,
    handoff: SessionHandoffSubmit,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Record a run-scoped handoff durably; the streamer advances it once."""
    if not current_entity:
        raise HTTPException(status_code=401, detail="Authentication required")
    session = await db.get(AgentSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_owner_or_manager(current_entity) and current_entity.id != session.agent_id:
        raise HTTPException(status_code=403, detail="You can only submit your own handoff")
    if (
        session.project_id != handoff.project_id
        or session.task_id != handoff.task_id
        or not session.run_token
        or session.run_token != handoff.run_token
    ):
        raise HTTPException(status_code=409, detail="Handoff identity does not match the active run")
    if session.ended_at is not None:
        raise HTTPException(status_code=409, detail="Session has already ended")
    if not handoff.summary.strip():
        raise HTTPException(status_code=422, detail="summary must contain text")
    result = await db.execute(
        sa_update(AgentSession)
        .where(
            AgentSession.id == session_id,
            AgentSession.ended_at.is_(None),
            AgentSession.handoff_received_at.is_(None),
            AgentSession.run_token == handoff.run_token,
        )
        .values(
            handoff_state=handoff.state,
            handoff_outputs_json=json.dumps(handoff.outputs),
            handoff_artifacts_json=json.dumps(handoff.artifacts),
            handoff_summary=handoff.summary.strip(),
            handoff_received_at=datetime.now(UTC),
        )
    )
    if result.rowcount == 1:
        await db.commit()
        return {"session_id": session_id, "accepted": True}
    await db.commit()
    existing = await db.get(AgentSession, session_id, populate_existing=True)
    if (
        existing and existing.ended_at is None
        and existing.handoff_state == handoff.state
        and existing.handoff_summary == handoff.summary.strip()
        and json.loads(existing.handoff_outputs_json or "[]") == handoff.outputs
        and json.loads(existing.handoff_artifacts_json or "[]") == handoff.artifacts
    ):
        return {"session_id": session_id, "accepted": True}
    raise HTTPException(status_code=409, detail="A different handoff was already submitted")


@router.get("/sessions/{session_id}/handoff")
async def get_agent_session_handoff(
    session_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Return the parsed worktree-local STATUS.md for a session."""
    result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    status_data = read_status_file(session.workspace_path)
    status_data["identity_matches_session"] = status_identity_matches_session(status_data, session)
    status_data["matches_session"] = status_matches_session(status_data, session)
    status_data["durable"] = {
        "state": session.handoff_state,
        "summary": session.handoff_summary,
        "outputs": json.loads(session.handoff_outputs_json or "[]"),
        "artifacts": json.loads(session.handoff_artifacts_json or "[]"),
        "received_at": session.handoff_received_at.isoformat() if session.handoff_received_at else None,
    }
    return status_data


@router.get("/tasks/{task_id}/active-session", response_model=Optional[AgentSessionResponse])
async def get_active_session_for_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return the most recent active agent session bound to a task, or null.

    Used by the kanban board to jump from a task card straight into the
    terminal tab for whichever role is currently executing it.
    """
    result = await db.execute(
        select(AgentSession)
        .filter(AgentSession.task_id == task_id, AgentSession.ended_at.is_(None))
        .order_by(desc(AgentSession.last_seen_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/sessions", response_model=List[AgentSessionResponse])
async def get_agent_sessions(
    agent_id: Optional[int] = None,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    active_only: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get agent CLI sessions, optionally scoped to a project or task."""
    query = select(AgentSession).order_by(desc(AgentSession.last_seen_at))
    if agent_id:
        query = query.filter(AgentSession.agent_id == agent_id)
    if project_id:
        query = query.filter(AgentSession.project_id == project_id)
    if task_id:
        query = query.filter(AgentSession.task_id == task_id)
    if active_only:
        query = query.filter(AgentSession.ended_at.is_(None))

    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.post("/{agent_id}/sessions", response_model=AgentSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_agent_session(
    agent_id: int,
    session: AgentSessionCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Start a durable visibility session for a CLI agent run."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only start your own session")

    agent_result = await db.execute(select(Entity).filter(Entity.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    project_result = await db.execute(select(Project).filter(Project.id == session.project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    workspace_path = session.workspace_path or project.path
    if not workspace_path:
        raise HTTPException(status_code=422, detail="workspace_path is required when project has no path")

    if session.task_id is not None:
        existing = await db.scalar(
            select(AgentSession).filter(
                AgentSession.agent_id == agent_id,
                AgentSession.task_id == session.task_id,
                AgentSession.ended_at.is_(None),
            )
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"An active session already exists for this assignment: {existing.id}",
            )

    db_session = AgentSession(
        agent_id=agent_id,
        project_id=session.project_id,
        task_id=session.task_id,
        workspace_path=workspace_path,
        assigned_role="worker",
        run_token=uuid.uuid4().hex,
        command=session.command,
        model=session.model,
        mode=session.mode,
        status=AgentSessionStatus.ACTIVE,
    )
    db.add(db_session)
    try:
        await db.flush()
        event_bus.enqueue(db,
            EventType.AGENT_ACTIVITY_LOGGED.value,
            {
                "agent_id": agent_id,
                "session_id": db_session.id,
                "project_id": db_session.project_id,
                "task_id": db_session.task_id,
                "activity_type": "session_started",
                "message": f"Session started in {db_session.workspace_path}",
                "workspace_path": db_session.workspace_path,
            },
            project_id=db_session.project_id,
            entity_id=agent_id
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if session.task_id is not None:
            existing = await db.scalar(
                select(AgentSession).filter(
                    AgentSession.agent_id == agent_id,
                    AgentSession.task_id == session.task_id,
                    AgentSession.ended_at.is_(None),
                )
            )
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"An active session was created concurrently for this assignment: {existing.id}",
                )
        raise
    await db.refresh(db_session)
    return db_session


@router.patch("/sessions/{session_id}", response_model=AgentSessionResponse)
async def update_agent_session(
    session_id: int,
    update: AgentSessionUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Update or end a durable CLI-agent session."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
    db_session = result.scalar_one_or_none()
    if not db_session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_owner_or_manager(current_entity) and current_entity.id != db_session.agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own session")

    db_session.status = update.status
    if update.task_id is not None:
        db_session.task_id = update.task_id
    db_session.last_seen_at = datetime.now(UTC)
    if update.status in (AgentSessionStatus.DONE, AgentSessionStatus.ERROR):
        db_session.ended_at = datetime.now(UTC)

    if update.message:
        activity = AgentActivity(
            agent_id=db_session.agent_id,
            session_id=db_session.id,
            project_id=db_session.project_id,
            task_id=db_session.task_id,
            activity_type=ActivityType.RESULT if update.status == AgentSessionStatus.DONE else ActivityType.ACTION,
            source="session_update",
            message=update.message,
            workspace_path=db_session.workspace_path,
        )
        db.add(activity)

    event_bus.enqueue(db,
        EventType.AGENT_STATUS_UPDATED.value,
        {
            "agent_id": db_session.agent_id,
            "session_id": db_session.id,
            "project_id": db_session.project_id,
            "task_id": db_session.task_id,
            "status_type": update.status.value,
            "message": update.message,
            "workspace_path": db_session.workspace_path,
        },
        project_id=db_session.project_id,
        entity_id=db_session.agent_id
    )
    await db.commit()
    await db.refresh(db_session)

    return db_session
