"""
Agent-to-Agent (A2A) Registry and Task Delegation

Provides agent discovery, task handoff, and lightweight messaging.
This is NOT a full Google A2A protocol implementation.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import json
import logging

from database import get_db
from models import Entity, EntityType, Task, TaskLog, Project, ApprovalStatus
from auth import get_current_entity, is_owner_or_manager, require_project_approval_for_mutation
from event_bus import event_bus, EventType
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/a2a", tags=["agent-to-agent"])


# ============================================================================
# SCHEMAS
# ============================================================================

class AgentCard(BaseModel):
    id: int
    name: str
    skills: List[str]
    status: str

class TaskHandoffRequest(BaseModel):
    new_agent_id: int
    reason: str = ""
    notes: Optional[str] = None

class TaskDelegationRequest(BaseModel):
    title: str
    description: str
    required_skills: Optional[str] = None
    priority: int = 0
    project_id: int

class AgentMessage(BaseModel):
    recipient_id: int
    message_type: str
    content: Dict[str, Any]
    reference_task_id: Optional[int] = None
    reference_project_id: Optional[int] = None


# ============================================================================
# AGENT DISCOVERY
# ============================================================================

@router.get("/agents", response_model=List[AgentCard])
async def list_available_agents(
    skills: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """List all available agents, optionally filtered by skills."""
    query = select(Entity).filter(
        Entity.entity_type == EntityType.AGENT,
        Entity.is_active == True
    )

    result = await db.execute(query)
    agents = result.scalars().all()

    if skills:
        required_skills = set(s.strip().lower() for s in skills.split(","))
        agents = [
            a for a in agents
            if required_skills & set(s.strip().lower() for s in (a.skills or "").split(","))
        ]

    return [
        AgentCard(
            id=a.id,
            name=a.name,
            skills=[s.strip() for s in (a.skills or "").split(",") if s.strip()],
            status="online"
        )
        for a in agents
    ]


@router.get("/agents/{agent_id}", response_model=AgentCard)
async def get_agent_card(
    agent_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed agent card"""
    result = await db.execute(
        select(Entity).filter(
            Entity.id == agent_id,
            Entity.entity_type == EntityType.AGENT,
            Entity.is_active == True
        )
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    return AgentCard(
        id=agent.id,
        name=agent.name,
        skills=[s.strip() for s in (agent.skills or "").split(",") if s.strip()],
        status="online"
    )


# ============================================================================
# TASK HANDOFF
# ============================================================================

@router.post("/handoff/{task_id}", status_code=status.HTTP_200_OK)
async def handoff_task(
    task_id: int,
    handoff: TaskHandoffRequest,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Hand off an existing task from one agent to another. Only MANAGER+ can handoff."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers/owners can handoff tasks")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Enforce project approval
    project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
    project = project_result.scalar_one_or_none()
    if project:
        await require_project_approval_for_mutation(project, current_entity)

    result = await db.execute(
        select(Entity).filter(
            Entity.id == handoff.new_agent_id,
            Entity.entity_type == EntityType.AGENT,
            Entity.is_active == True
        )
    )
    new_agent = result.scalar_one_or_none()

    if not new_agent:
        raise HTTPException(status_code=404, detail="New agent not found")

    old_assignees = [a.name for a in task.assignees]
    task.assignees.clear()
    task.assignees.append(new_agent)
    task.version += 1

    log = TaskLog(
        task_id=task_id,
        message=f"Task handed off from {', '.join(old_assignees) or 'unassigned'} to {new_agent.name} by {current_entity.name}. Reason: {handoff.reason}",
        log_type="action"
    )
    db.add(log)
    await db.commit()

    logger.info(f"Task handoff: {task.title} -> {new_agent.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.TASK_ASSIGNED.value,
        {
            "task_id": task_id,
            "entity_id": handoff.new_agent_id,
            "handoff": True,
            "reason": handoff.reason,
            "previous_assignees": old_assignees
        },
        project_id=task.project_id,
        entity_id=current_entity.id
    )

    return {
        "ok": True,
        "task_id": task_id,
        "new_agent": new_agent.name,
        "previous_assignees": old_assignees,
        "reason": handoff.reason
    }


# ============================================================================
# TASK DELEGATION
# ============================================================================

@router.post("/delegate", status_code=status.HTTP_201_CREATED)
async def delegate_task(
    delegation: TaskDelegationRequest,
    assign_to: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create a new task and optionally assign it to a specific agent."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    from models import Stage

    # Verify project exists
    from models import Project as ProjectModel
    proj_result = await db.execute(
        select(ProjectModel).filter(ProjectModel.id == delegation.project_id)
    )
    project = proj_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Enforce project approval
    await require_project_approval_for_mutation(project, current_entity)

    # Get default stage
    stage_result = await db.execute(
        select(Stage).filter(Stage.project_id == delegation.project_id).order_by(Stage.order)
    )
    stages = stage_result.scalars().all()
    stage_id = stages[1].id if len(stages) > 1 else (stages[0].id if stages else None)

    task = Task(
        title=delegation.title,
        description=delegation.description,
        status="pending",
        project_id=delegation.project_id,
        stage_id=stage_id,
        required_skills=delegation.required_skills,
        priority=delegation.priority,
        created_by=current_entity.id
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    assigned_agent_name = None
    if assign_to:
        agent_result = await db.execute(
            select(Entity).filter(
                Entity.id == assign_to,
                Entity.entity_type == EntityType.AGENT,
                Entity.is_active == True
            )
        )
        agent = agent_result.scalar_one_or_none()
        if agent:
            task.assignees.append(agent)
            task.version += 1
            assigned_agent_name = agent.name
            await db.commit()

    logger.info(f"Task delegated: {task.title} in project {delegation.project_id} by {current_entity.name}")

    await event_bus.publish(
        EventType.TASK_CREATED.value,
        {
            "task_id": task.id,
            "title": task.title,
            "assigned_to": assign_to,
            "agent_name": assigned_agent_name
        },
        project_id=task.project_id,
        entity_id=current_entity.id
    )

    return {
        "ok": True,
        "task_id": task.id,
        "title": task.title,
        "assigned_to": assigned_agent_name,
        "project_id": delegation.project_id
    }


# ============================================================================
# AGENT MESSAGING
# ============================================================================

_message_queue: Dict[int, List[Dict]] = {}
_message_counter = 0


@router.post("/message", status_code=status.HTTP_201_CREATED)
async def send_agent_message(
    message: AgentMessage,
    db: AsyncSession = Depends(get_db)
):
    """Send a message from one agent to another"""
    global _message_counter

    result = await db.execute(
        select(Entity).filter(
            Entity.id == message.recipient_id,
            Entity.entity_type == EntityType.AGENT,
            Entity.is_active == True
        )
    )
    recipient = result.scalar_one_or_none()

    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient agent not found")

    _message_counter += 1
    msg = {
        "id": _message_counter,
        "sender_id": 1,
        "sender_name": "System",
        "recipient_id": message.recipient_id,
        "message_type": message.message_type,
        "content": message.content,
        "reference_task_id": message.reference_task_id,
        "reference_project_id": message.reference_project_id,
        "created_at": datetime.utcnow().isoformat()
    }

    if message.recipient_id not in _message_queue:
        _message_queue[message.recipient_id] = []
    _message_queue[message.recipient_id].append(msg)

    logger.info(f"Agent message sent to {recipient.name} (type={message.message_type})")

    await event_bus.publish(
        EventType.ENTITY_UPDATED.value,
        {
            "message_id": msg["id"],
            "recipient_id": message.recipient_id,
            "message_type": message.message_type
        },
        project_id=message.reference_project_id
    )

    return {"ok": True, "message_id": msg["id"]}


@router.get("/messages")
async def get_agent_messages(
    agent_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get pending messages for an agent"""
    messages = _message_queue.get(agent_id, [])

    if agent_id in _message_queue:
        _message_queue[agent_id] = []

    return messages


@router.get("/messages/unread-count")
async def get_unread_message_count(
    agent_id: int
):
    """Get count of unread messages for an agent"""
    count = len(_message_queue.get(agent_id, []))
    return {"unread_count": count}
