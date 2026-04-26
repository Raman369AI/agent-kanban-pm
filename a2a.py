"""
Agent-to-Agent (A2A) Communication Protocol
Enables agents to communicate and collaborate with each other.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import json

from database import get_db, async_session_maker
from models import Entity, EntityType, AgentConnection, ProtocolType
from event_bus import event_bus, EventType

router = APIRouter(prefix="/a2a", tags=["agent-to-agent"])


# ============================================================================
# SCHEMAS
# ============================================================================

class A2AMessage(BaseModel):
    """Message from one agent to another"""
    recipient_id: int
    message_type: str
    content: Dict[str, Any]
    reference_task_id: Optional[int] = None
    reference_project_id: Optional[int] = None


class A2ABroadcast(BaseModel):
    """Broadcast message to multiple agents"""
    message_type: str
    content: Dict[str, Any]
    recipient_skills: Optional[str] = None  # Filter by skills
    reference_project_id: Optional[int] = None


class A2AMessageResponse(BaseModel):
    """Response for A2A messages"""
    id: int
    sender_id: int
    recipient_id: int
    message_type: str
    content: Dict[str, Any]
    created_at: datetime


# In-memory message queue (for demo purposes)
# In production, this would be persisted to database
_message_queue: Dict[int, List[Dict]] = {}  # recipient_id -> list of messages
_message_counter = 0


# ============================================================================
# ENDPOINTS
# ============================================================================

@router.post("/message", status_code=status.HTTP_201_CREATED)
async def send_a2a_message(
    message: A2AMessage,
    sender_id: int = 1,  # In production, get from auth
    db: AsyncSession = Depends(get_db)
):
    """Send a direct message from one agent to another"""
    global _message_counter
    
    # Verify recipient exists and is an agent
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
    
    # Create message
    _message_counter += 1
    msg = {
        "id": _message_counter,
        "sender_id": sender_id,
        "recipient_id": message.recipient_id,
        "message_type": message.message_type,
        "content": message.content,
        "reference_task_id": message.reference_task_id,
        "reference_project_id": message.reference_project_id,
        "created_at": datetime.utcnow().isoformat()
    }
    
    # Add to queue
    if message.recipient_id not in _message_queue:
        _message_queue[message.recipient_id] = []
    _message_queue[message.recipient_id].append(msg)
    
    # Publish event for real-time notification
    await event_bus.publish(
        "a2a_message",
        {
            "message_id": msg["id"],
            "sender_id": sender_id,
            "recipient_id": message.recipient_id,
            "message_type": message.message_type
        },
        project_id=message.reference_project_id
    )
    
    return {"ok": True, "message_id": msg["id"]}


@router.get("/messages", response_model=List[A2AMessageResponse])
async def get_a2a_messages(
    agent_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get pending messages for an agent"""
    messages = _message_queue.get(agent_id, [])[-limit:]
    
    # Clear the queue after reading
    if agent_id in _message_queue:
        _message_queue[agent_id] = []
    
    return messages


@router.post("/broadcast", status_code=status.HTTP_201_CREATED)
async def broadcast_a2a_message(
    broadcast: A2ABroadcast,
    sender_id: int = 1,
    db: AsyncSession = Depends(get_db)
):
    """Broadcast a message to all agents (optionally filtered by skills)"""
    global _message_counter
    
    # Find matching agents
    query = select(Entity).filter(
        Entity.entity_type == EntityType.AGENT,
        Entity.is_active == True
    )
    
    result = await db.execute(query)
    agents = result.scalars().all()
    
    # Filter by skills if specified
    if broadcast.recipient_skills:
        required_skills = set(s.strip().lower() for s in broadcast.recipient_skills.split(","))
        agents = [
            a for a in agents
            if required_skills & set(s.strip().lower() for s in (a.skills or "").split(","))
        ]
    
    sent_count = 0
    for agent in agents:
        if agent.id == sender_id:
            continue  # Don't send to self
        
        _message_counter += 1
        msg = {
            "id": _message_counter,
            "sender_id": sender_id,
            "recipient_id": agent.id,
            "message_type": broadcast.message_type,
            "content": broadcast.content,
            "reference_project_id": broadcast.reference_project_id,
            "created_at": datetime.utcnow().isoformat()
        }
        
        if agent.id not in _message_queue:
            _message_queue[agent.id] = []
        _message_queue[agent.id].append(msg)
        sent_count += 1
    
    return {"ok": True, "recipients": sent_count}


@router.get("/agents")
async def list_available_agents(
    skills: Optional[str] = None,
    db: AsyncSession = Depends(get_db)
):
    """List all available agents (optionally filtered by skills)"""
    query = select(Entity).filter(
        Entity.entity_type == EntityType.AGENT,
        Entity.is_active == True
    )
    
    result = await db.execute(query)
    agents = result.scalars().all()
    
    # Filter by skills if specified
    if skills:
        required_skills = set(s.strip().lower() for s in skills.split(","))
        agents = [
            a for a in agents
            if required_skills & set(s.strip().lower() for s in (a.skills or "").split(","))
        ]
    
    return [
        {
            "id": a.id,
            "name": a.name,
            "skills": a.skills,
            "created_at": a.created_at.isoformat() if a.created_at else None
        }
        for a in agents
    ]


@router.post("/handoff/{task_id}")
async def handoff_task(
    task_id: int,
    new_agent_id: int,
    reason: str = "",
    sender_id: int = 1,
    db: AsyncSession = Depends(get_db)
):
    """Hand off a task from one agent to another"""
    from models import Task, TaskLog
    from sqlalchemy.orm import selectinload
    
    # Get the task
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    # Verify new agent exists
    result = await db.execute(
        select(Entity).filter(
            Entity.id == new_agent_id,
            Entity.entity_type == EntityType.AGENT,
            Entity.is_active == True
        )
    )
    new_agent = result.scalar_one_or_none()
    
    if not new_agent:
        raise HTTPException(status_code=404, detail="New agent not found")
    
    # Remove old assignees and add new
    old_assignees = [a.name for a in task.assignees]
    task.assignees.clear()
    task.assignees.append(new_agent)
    
    # Log the handoff
    log = TaskLog(
        task_id=task_id,
        message=f"Task handed off from {', '.join(old_assignees)} to {new_agent.name}. Reason: {reason}",
        log_type="action"
    )
    db.add(log)
    await db.commit()
    
    # Notify via event bus
    await event_bus.publish(
        EventType.TASK_ASSIGNED.value,
        {
            "task_id": task_id,
            "entity_id": new_agent_id,
            "handoff": True,
            "reason": reason
        },
        project_id=task.project_id
    )
    
    return {
        "ok": True,
        "task_id": task_id,
        "new_agent": new_agent.name,
        "previous_assignees": old_assignees
    }
