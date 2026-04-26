"""
Event Bus for publishing and subscribing to events across the system.
Used for real-time notifications between components and agents.
"""

import asyncio
import json
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from enum import Enum
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_maker
from models import PendingEvent, AgentConnection, ProtocolType


class EventType(str, Enum):
    """Event types that can be published/subscribed to"""
    # Task events
    TASK_CREATED = "task_created"
    TASK_UPDATED = "task_updated"
    TASK_DELETED = "task_deleted"
    TASK_MOVED = "task_moved"
    TASK_ASSIGNED = "task_assigned"
    TASK_UNASSIGNED = "task_unassigned"
    TASK_COMMENTED = "task_commented"
    
    # Project events
    PROJECT_CREATED = "project_created"
    PROJECT_UPDATED = "project_updated"
    PROJECT_DELETED = "project_deleted"
    
    # Entity events
    ENTITY_REGISTERED = "entity_registered"
    ENTITY_UPDATED = "entity_updated"
    
    # Connection events
    AGENT_CONNECTED = "agent_connected"
    AGENT_DISCONNECTED = "agent_disconnected"


class EventBus:
    """
    Event bus for publishing events to subscribers.
    Supports both in-memory subscriptions and persistent agent queues.
    """
    
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._websocket_manager = None
    
    def set_websocket_manager(self, manager):
        """Set the websocket manager for real-time broadcasts"""
        self._websocket_manager = manager
    
    def subscribe(self, event_type: str, callback: Callable):
        """Subscribe to an event type"""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(callback)
    
    def unsubscribe(self, event_type: str, callback: Callable):
        """Unsubscribe from an event type"""
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(callback)
    
    async def publish(
        self, 
        event_type: str, 
        data: Dict[str, Any], 
        project_id: Optional[int] = None
    ):
        """
        Publish an event to all subscribers and persist for offline agents.
        """
        event_payload = {
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "project_id": project_id,
            "data": data
        }
        
        # 1. Call in-memory subscribers
        if event_type in self._subscribers:
            for callback in self._subscribers[event_type]:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(event_payload)
                    else:
                        callback(event_payload)
                except Exception as e:
                    print(f"Error in event subscriber: {e}")
        
        # 2. Broadcast via WebSocket if manager is set
        if self._websocket_manager:
            try:
                if project_id:
                    await self._websocket_manager.broadcast_to_project(
                        event_payload, project_id
                    )
                await self._websocket_manager.broadcast_to_all(event_payload)
            except Exception as e:
                print(f"Error broadcasting via WebSocket: {e}")
        
        # 3. Persist to database for MCP agents to poll
        await self._persist_for_agents(event_type, event_payload, project_id)
    
    async def _persist_for_agents(
        self, 
        event_type: str, 
        payload: Dict, 
        project_id: Optional[int]
    ):
        """Persist event for agents who subscribe via MCP polling"""
        async with async_session_maker() as session:
            # Find all MCP agents subscribed to this event type
            result = await session.execute(
                select(AgentConnection).filter(
                    AgentConnection.protocol == ProtocolType.MCP,
                    AgentConnection.status == "online"
                )
            )
            connections = result.scalars().all()
            
            for conn in connections:
                # Check if agent is subscribed to this event type
                try:
                    subscribed_events = json.loads(conn.subscribed_events or "[]")
                    subscribed_projects = json.loads(conn.subscribed_projects or "null")
                    
                    # Check event subscription
                    if event_type not in subscribed_events and "*" not in subscribed_events:
                        continue
                    
                    # Check project subscription (null = all projects)
                    if subscribed_projects and project_id not in subscribed_projects:
                        continue
                    
                    # Create pending event
                    pending = PendingEvent(
                        agent_id=conn.entity_id,
                        event_type=event_type,
                        payload=json.dumps(payload),
                        project_id=project_id
                    )
                    session.add(pending)
                    
                except Exception as e:
                    print(f"Error checking subscription for agent {conn.entity_id}: {e}")
            
            await session.commit()


# Global event bus instance
event_bus = EventBus()
