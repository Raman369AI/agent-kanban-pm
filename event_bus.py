"""
Event Bus for publishing and subscribing to events across the system.
Used for real-time notifications between components and agents.

Supports:
- Exact event type subscriptions (e.g., "task_created")
- Wildcard subscription ("*") for all events
- Persistent event queue for MCP agents via database
- Async queue-based processing to avoid SQLAlchemy greenlet conflicts
"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Dict, List, Optional, Callable, Any, Set
from enum import Enum
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import async_session_maker
from models import PendingEvent, AgentConnection, ProtocolType

logger = logging.getLogger(__name__)


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
    TASK_COMPLETED = "task_completed"

    # Project events
    PROJECT_CREATED = "project_created"
    PROJECT_UPDATED = "project_updated"
    PROJECT_DELETED = "project_deleted"
    PROJECT_APPROVED = "project_approved"

    # Entity events
    ENTITY_REGISTERED = "entity_registered"
    ENTITY_UPDATED = "entity_updated"

    # Connection events
    AGENT_CONNECTED = "agent_connected"
    AGENT_DISCONNECTED = "agent_disconnected"

    # Staleness events
    AGENT_STALE = "agent_stale"

    # Agent activity events
    AGENT_STATUS_UPDATED = "agent_status_updated"
    AGENT_ACTIVITY_LOGGED = "agent_activity_logged"
    ORCHESTRATION_DECISION_LOGGED = "orchestration_decision_logged"
    TASK_LEASE_UPDATED = "task_lease_updated"
    ACTIVITY_SUMMARY_CREATED = "activity_summary_created"
    USER_CONTRIBUTION_LOGGED = "user_contribution_logged"
    DIFF_REVIEW_REQUESTED = "diff_review_requested"
    DIFF_REVIEW_COMPLETED = "diff_review_completed"
    CHAT_TASK_CREATED = "chat_task_created"

    # Approval queue events
    AGENT_APPROVAL_REQUESTED = "agent_approval_requested"
    AGENT_APPROVAL_RESOLVED = "agent_approval_resolved"

    # Stage policy events
    STAGE_POLICY_CREATED = "stage_policy_created"
    STAGE_POLICY_UPDATED = "stage_policy_updated"
    TASK_TRANSITION_BLOCKED = "task_transition_blocked"

    # Catch-all wildcard
    ALL = "*"


class EventBus:
    """
    Event bus for publishing events to subscribers.
    Supports both in-memory subscriptions and persistent agent queues.

    Uses an internal async queue to process events in a dedicated
    background task, avoiding SQLAlchemy greenlet conflicts when
    event handlers create their own database sessions.
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._websocket_manager = None
        self._queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None

    def start(self):
        """Start the background event processing worker."""
        if self._worker_task is None or self._worker_task.done():
            self._queue = asyncio.Queue()
            self._worker_task = asyncio.create_task(self._process_events())
            logger.info("Event bus worker started")

    def stop(self):
        """Stop the background event processing worker."""
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None
            logger.info("Event bus worker stopped")

    def reset(self):
        """Reset internal state for test isolation.

        This singleton is process-scoped by design (local-first SQLite
        operation).  Call reset() in test fixtures to clear subscribers
        and stop the worker between tests.
        """
        self.stop()
        self._subscribers.clear()
        self._websocket_manager = None
        self._queue = None

    def set_websocket_manager(self, manager):
        """Set the websocket manager for real-time broadcasts"""
        self._websocket_manager = manager

    def subscribe(self, event_type: str, callback: Callable):
        """Subscribe to an event type. Use '*' for all events."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        if callback not in self._subscribers[event_type]:
            self._subscribers[event_type].append(callback)
            logger.debug(f"Subscriber registered for event_type={event_type}")

    def unsubscribe(self, event_type: str, callback: Callable):
        """Unsubscribe from an event type"""
        if event_type in self._subscribers:
            try:
                self._subscribers[event_type].remove(callback)
            except ValueError:
                pass

    async def publish(
        self,
        event_type: str,
        data: Dict[str, Any],
        project_id: Optional[int] = None,
        entity_id: Optional[int] = None
    ):
        """
        Publish an event by placing it on the internal queue.
        The actual dispatch (subscribers, DB, WebSocket) happens in the background.
        """
        event_payload = {
            "event_type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "project_id": project_id,
            "entity_id": entity_id,
            "data": data
        }

        if self._queue is None:
            self.start()

        # Just queue the event; background worker handles the rest
        await self._queue.put(event_payload)

    async def _process_events(self):
        """Background worker that processes events from the queue."""
        while True:
            try:
                event_payload = await self._queue.get()
                await self._handle_event(event_payload)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in event bus worker loop: {e}")

    async def _handle_event(self, event_payload: dict):
        """Dispatch an event to all channels in parallel."""
        event_type = event_payload.get("event_type")
        project_id = event_payload.get("project_id")
        
        tasks = []

        # 1. In-memory subscribers
        callbacks = self._subscribers.get(event_type, [])
        wildcard_subs = self._subscribers.get(EventType.ALL.value, [])
        for callback in set(callbacks + wildcard_subs):
            tasks.append(self._safe_call(callback, event_payload))

        # 2. WebSocket broadcasts
        if self._websocket_manager:
            tasks.append(self._broadcast_websocket(event_payload))

        # 3. Persistent DB queue for MCP agents
        tasks.append(self._persist_for_agents(event_type, event_payload, project_id))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_call(self, callback: Callable, payload: Dict):
        """Call a subscriber callback safely, supporting both sync and async."""
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(payload)
            else:
                callback(payload)
        except Exception as e:
            logger.error(f"Error in event subscriber {callback.__name__}: {e}")

    async def _broadcast_websocket(self, payload: Dict):
        """Broadcast event to UI clients via WebSockets."""
        if not self._websocket_manager:
            return
        
        project_id = payload.get("project_id")
        try:
            if project_id:
                await self._websocket_manager.broadcast_to_project(payload, project_id)
            await self._websocket_manager.broadcast_to_all(payload)
        except Exception as e:
            logger.error(f"WebSocket broadcast error: {e}")

    async def _persist_for_agents(
        self,
        event_type: str,
        payload: Dict,
        project_id: Optional[int]
    ):
        """Persist event for agents who subscribe via MCP polling"""
        async with async_session_maker() as session:
            # Only query online MCP agents
            result = await session.execute(
                select(AgentConnection).filter(
                    AgentConnection.protocol == ProtocolType.MCP,
                    AgentConnection.status == "online"
                )
            )
            connections = result.scalars().all()

            for conn in connections:
                try:
                    # Check subscription criteria
                    sub_events = json.loads(conn.subscribed_events or "[]")
                    sub_projects = json.loads(conn.subscribed_projects or "null")

                    if event_type not in sub_events and EventType.ALL.value not in sub_events:
                        continue
                    if sub_projects and project_id not in sub_projects:
                        continue

                    session.add(PendingEvent(
                        agent_id=conn.entity_id,
                        event_type=event_type,
                        payload=json.dumps(payload),
                        project_id=project_id
                    ))
                except Exception as e:
                    logger.error(f"Error checking MCP subscription for agent {conn.entity_id}: {e}")

            await session.commit()


# Global event bus instance
event_bus = EventBus()
