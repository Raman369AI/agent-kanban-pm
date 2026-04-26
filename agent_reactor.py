"""
Agent Reactor - Makes agents react to events automatically.
This enables autonomous agent behavior in response to system events.
"""

import asyncio
import json
from typing import Optional
from datetime import datetime

from event_bus import event_bus, EventType
from database import async_session_maker
from models import Entity, Task, AgentConnection, ProtocolType, TaskStatus, TaskLog
from sqlalchemy import select
from sqlalchemy.orm import selectinload


async def handle_task_created(event: dict):
    """React to task creation - agents may want to self-assign"""
    data = event.get("data", {})
    task_id = data.get("task_id")
    project_id = event.get("project_id")
    
    if not task_id:
        return
    
    async with async_session_maker() as session:
        # Get the task
        result = await session.execute(
            select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
        )
        task = result.scalar_one_or_none()
        
        if not task:
            return
        
        # Check if task already has assignees
        if task.assignees:
            return
        
        # Find agents with matching skills who are online
        required_skills = (task.required_skills or "").split(",")
        required_skills = [s.strip().lower() for s in required_skills if s.strip()]
        
        result = await session.execute(
            select(Entity).filter(Entity.entity_type == "agent", Entity.is_active == True)
        )
        agents = result.scalars().all()
        
        # Score agents by skill match
        best_agent = None
        best_score = -1
        
        for agent in agents:
            agent_skills = (agent.skills or "").lower().split(",")
            agent_skills = [s.strip() for s in agent_skills]
            
            # Calculate match score
            score = sum(1 for skill in required_skills if skill in agent_skills)
            
            if score > best_score:
                best_score = score
                best_agent = agent
        
        # Auto-assign if we found a good match (score > 0 or no skills required)
        if best_agent and (best_score > 0 or not required_skills):
            task.assignees.append(best_agent)
            
            # Log the auto-assignment
            log = TaskLog(
                task_id=task.id,
                message=f"Agent Reactor: Auto-assigned to {best_agent.name} (skill match: {best_score})",
                log_type="action"
            )
            session.add(log)
            await session.commit()
            print(f"Agent Reactor: Auto-assigned task {task_id} to {best_agent.name}")


async def handle_task_assigned(event: dict):
    """React to task assignment - notify the assigned agent"""
    data = event.get("data", {})
    task_id = data.get("task_id")
    entity_id = data.get("entity_id")
    
    # This could trigger notifications, emails, etc.
    print(f"Agent Reactor: Task {task_id} assigned to entity {entity_id}")


async def handle_project_created(event: dict):
    """React to project creation - set up initial structure"""
    project_id = event.get("project_id")
    data = event.get("data", {})
    
    print(f"Agent Reactor: Project {project_id} created - {data.get('name', 'Unknown')}")


async def handle_event(event: dict):
    """Main event handler that routes to specific handlers"""
    event_type = event.get("event_type")
    
    handlers = {
        EventType.TASK_CREATED.value: handle_task_created,
        EventType.TASK_ASSIGNED.value: handle_task_assigned,
        EventType.PROJECT_CREATED.value: handle_project_created,
    }
    
    handler = handlers.get(event_type)
    if handler:
        try:
            await handler(event)
        except Exception as e:
            print(f"Agent Reactor error handling {event_type}: {e}")


def register_agent_reactor():
    """
    Register the agent reactor with the event bus.
    Called during application startup.
    """
    # Subscribe to relevant events
    for event_type in [
        EventType.TASK_CREATED.value,
        EventType.TASK_ASSIGNED.value,
        EventType.PROJECT_CREATED.value,
        EventType.TASK_UPDATED.value,
        EventType.TASK_MOVED.value,
    ]:
        event_bus.subscribe(event_type, handle_event)
    
    print("Agent Reactor registered")
