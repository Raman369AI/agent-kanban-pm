from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import UTC, datetime
import logging

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    AgentHeartbeat, AgentActivity, Entity, Task, AgentStatusType, ActivityType,
)
from agent_kanban_pm.schemas import (
    AgentHeartbeatResponse, AgentActivityResponse, AgentActivityCreate,
    AgentStatusUpdate,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/status", response_model=List[AgentHeartbeatResponse])
async def get_agent_statuses(
    db: AsyncSession = Depends(get_db)
):
    """Get all current agent heartbeats."""
    result = await db.execute(
        select(AgentHeartbeat)
        .options(selectinload(AgentHeartbeat.agent))
        .filter(AgentHeartbeat.status_type != AgentStatusType.IDLE)
        .order_by(desc(AgentHeartbeat.updated_at))
    )
    return result.scalars().all()


@router.get("/activity", response_model=List[AgentActivityResponse])
async def get_activity_feed(
    agent_id: Optional[int] = None,
    project_id: Optional[int] = None,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    activity_type: Optional[ActivityType] = None,
    has_command: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get recent agent activity feed, optionally filtered.

    `activity_type` and `has_command` narrow the feed to an audit view: what
    the agents actually executed, rather than the full narration.
    """
    query = select(AgentActivity).order_by(desc(AgentActivity.created_at))

    if agent_id:
        query = query.filter(AgentActivity.agent_id == agent_id)
    if project_id:
        query = query.filter(AgentActivity.project_id == project_id)
    if session_id:
        query = query.filter(AgentActivity.session_id == session_id)
    if task_id:
        query = query.filter(AgentActivity.task_id == task_id)
    if activity_type:
        query = query.filter(AgentActivity.activity_type == activity_type)
    if has_command:
        query = query.filter(AgentActivity.command.isnot(None))

    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.post("/{agent_id}/status", response_model=AgentHeartbeatResponse)
async def update_agent_status(
    agent_id: int,
    update: AgentStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Update an agent's heartbeat status. Called by agents or server fallback."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own status")
    # Verify the agent exists
    result = await db.execute(select(Entity).filter(Entity.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Find existing heartbeat or create new
    result = await db.execute(
        select(AgentHeartbeat).filter(AgentHeartbeat.agent_id == agent_id)
    )
    heartbeat = result.scalar_one_or_none()

    if heartbeat:
        heartbeat.status_type = update.status_type
        heartbeat.message = update.message
        heartbeat.task_id = update.task_id
        heartbeat.updated_at = datetime.now(UTC)
    else:
        heartbeat = AgentHeartbeat(
            agent_id=agent_id,
            status_type=update.status_type,
            message=update.message,
            task_id=update.task_id
        )
        db.add(heartbeat)

    event_bus.enqueue(db,
        EventType.AGENT_STATUS_UPDATED.value,
        {
            "agent_id": agent_id,
            "status_type": update.status_type.value,
            "message": update.message,
            "task_id": update.task_id
        },
        entity_id=agent_id
    )
    await db.commit()
    await db.refresh(heartbeat)

    return heartbeat


@router.post("/{agent_id}/activity", response_model=AgentActivityResponse, status_code=status.HTTP_201_CREATED)
async def log_agent_activity(
    agent_id: int,
    activity: AgentActivityCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Log an activity entry for an agent."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only log your own activity")
    result = await db.execute(select(Entity).filter(Entity.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    db_activity = AgentActivity(
        agent_id=agent_id,
        session_id=activity.session_id,
        project_id=activity.project_id,
        task_id=activity.task_id,
        activity_type=activity.activity_type,
        source=activity.source,
        message=activity.message,
        payload_json=activity.payload_json,
        workspace_path=activity.workspace_path,
        file_path=activity.file_path,
        command=activity.command,
    )
    db.add(db_activity)

    # Derive project_id from task_id for per-project filtering
    project_id = activity.project_id
    if activity.task_id:
        task_result = await db.execute(select(Task).filter(Task.id == activity.task_id))
        task = task_result.scalar_one_or_none()
        if task:
            project_id = task.project_id
            if db_activity.project_id is None:
                db_activity.project_id = project_id

    await db.flush()
    event_bus.enqueue(db,
        EventType.AGENT_ACTIVITY_LOGGED.value,
        {
            "agent_id": agent_id,
            "activity_id": db_activity.id,
            "session_id": db_activity.session_id,
            "project_id": project_id,
            "activity_type": activity.activity_type.value,
            "message": activity.message,
            "task_id": activity.task_id,
            "source": activity.source,
            "workspace_path": activity.workspace_path,
            "file_path": activity.file_path,
            "command": activity.command,
        },
        project_id=project_id,
        entity_id=agent_id
    )
    await db.commit()
    await db.refresh(db_activity)

    return db_activity
