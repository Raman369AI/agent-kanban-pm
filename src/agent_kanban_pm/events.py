"""Durable SQLite outbox shared by REST and MCP producers.

The single HTTP server dispatches committed events. Handlers can be retried;
execution intent is separately recorded by the scheduler before it launches.
"""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Dict, List, Optional, Callable, Any, Set
from enum import Enum
from sqlalchemy import select, update, or_
from sqlalchemy.ext.asyncio import AsyncSession

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (PendingEvent, AgentConnection, ProtocolType, ConnectionStatus,
                                    OutboxEvent, OutboxDelivery)

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
    PROJECT_REJECTED = "project_rejected"

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
    """Dispatch committed notifications with bounded retry delays."""

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = {}
        self._websocket_manager = None
        self._queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None

    def start(self):
        """Start the background event processing worker."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._process_events())
            logger.info("Event bus worker started")

    def stop(self):
        """Request background event processing worker shutdown.

        This synchronous method is intentionally best-effort for callers that
        are not inside async shutdown code. Async callers should prefer
        stop_async() so the cancelled worker is awaited before DB disposal.
        """
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None
            logger.info("Event bus worker stop requested")

    async def stop_async(self):
        """Drain queued events, then stop and await the background worker."""
        task = self._worker_task
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._worker_task = None
            self._queue = None
            try:
                await asyncio.wait_for(self.dispatch_pending(), timeout=10)
            except Exception:
                logger.warning("Undelivered events retained for the next server start")
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

    def enqueue(self, db, event_type, data, project_id=None, entity_id=None):
        """Add a notification to the caller's transaction. No delivery before commit."""
        from agent_kanban_pm.models import OutboxEvent
        row = OutboxEvent(payload=json.dumps({
            "event_type": event_type, "timestamp": datetime.now(UTC).isoformat(),
            "project_id": project_id, "entity_id": entity_id, "data": data,
        }, default=str))
        db.add(row)
        return row

    async def publish(self, event_type, data, project_id=None, entity_id=None):
        # Standalone telemetry uses its own transaction. Domain mutations use
        # enqueue(db, ...) so their state and notification commit atomically.
        async with async_session_maker() as db:
            self.enqueue(db, event_type, data, project_id, entity_id)
            await db.commit()

    async def _claim_pending(self):
        """Claim due events with a renewable database lease."""
        now = datetime.now(UTC)
        stale = now - timedelta(seconds=30)
        token = __import__("uuid").uuid4().hex
        async with async_session_maker() as db:
            ids = list((await db.execute(select(OutboxEvent.id).where(
                OutboxEvent.delivered_at.is_(None),
                or_(OutboxEvent.retry_at.is_(None), OutboxEvent.retry_at <= now),
                or_(OutboxEvent.claimed_at.is_(None), OutboxEvent.claimed_at < stale),
            ).order_by(OutboxEvent.id).limit(50))).scalars())
            claimed = []
            for event_id in ids:
                result = await db.execute(update(OutboxEvent).where(
                    OutboxEvent.id == event_id,
                    OutboxEvent.delivered_at.is_(None),
                    or_(OutboxEvent.claimed_at.is_(None), OutboxEvent.claimed_at < stale),
                ).values(claimed_at=now, claim_token=token))
                if result.rowcount == 1:
                    claimed.append(event_id)
            await db.commit()
        return claimed, token

    async def _renew_claim(self, event_ids: list[int], token: str):
        while True:
            await asyncio.sleep(10)
            async with async_session_maker() as db:
                await db.execute(update(OutboxEvent).where(
                    OutboxEvent.id.in_(event_ids),
                    OutboxEvent.claim_token == token,
                    OutboxEvent.delivered_at.is_(None),
                ).values(claimed_at=datetime.now(UTC)))
                await db.commit()

    async def dispatch_pending(self):
        event_ids, token = await self._claim_pending()
        for event_id in event_ids:
            async with async_session_maker() as db:
                row = await db.get(OutboxEvent, event_id)
                if row is None or row.claim_token != token or row.delivered_at is not None:
                    continue
                payload = json.loads(row.payload)
            payload["event_id"] = event_id
            error = None
            renewal = asyncio.create_task(self._renew_claim(event_ids, token))
            try:
                await self._handle_event(payload)
            except Exception as exc:
                error = str(exc)
                logger.exception("Event %s delivery failed", event_id)
            finally:
                renewal.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewal
            async with async_session_maker() as db:
                saved = await db.get(OutboxEvent, event_id)
                if saved is None or saved.claim_token != token:
                    continue
                saved.attempts += 1
                saved.last_error = error
                saved.claimed_at = None
                saved.claim_token = None
                if error:
                    saved.retry_at = datetime.now(UTC) + timedelta(seconds=min(60, 2 ** min(saved.attempts, 6)))
                else:
                    saved.retry_at = None
                    saved.delivered_at = datetime.now(UTC)
                await db.commit()
        return len(event_ids)

    async def _process_events(self):
        while True:
            try:
                await self.dispatch_pending()
                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('Outbox dispatcher failed')
                await asyncio.sleep(1)

    async def _deliver_channel(self, event_id: int, channel: str, handler):
        async with async_session_maker() as db:
            existing = await db.scalar(select(OutboxDelivery.id).where(
                OutboxDelivery.event_id == event_id, OutboxDelivery.channel == channel))
        if existing is not None:
            return
        await handler()
        async with async_session_maker() as db:
            db.add(OutboxDelivery(event_id=event_id, channel=channel))
            try:
                await db.commit()
            except Exception:
                await db.rollback()
                if await db.scalar(select(OutboxDelivery.id).where(
                        OutboxDelivery.event_id == event_id, OutboxDelivery.channel == channel)) is None:
                    raise

    async def _handle_event(self, event_payload: dict):
        """Dispatch each channel once; failed channels alone are retried."""
        event_type = event_payload.get("event_type")
        project_id = event_payload.get("project_id")
        event_id = event_payload["event_id"]
        callbacks = self._subscribers.get(event_type, [])
        wildcard_subs = self._subscribers.get(EventType.ALL.value, [])
        deliveries = []
        for callback in set(callbacks + wildcard_subs):
            identity = f"{callback.__module__}.{callback.__qualname__}"
            deliveries.append(self._deliver_channel(
                event_id, f"subscriber:{identity}",
                lambda callback=callback: self._safe_call(callback, event_payload),
            ))
        if self._websocket_manager:
            deliveries.append(self._deliver_channel(
                event_id, "websocket", lambda: self._broadcast_websocket(event_payload)))
        deliveries.append(self._deliver_channel(
            event_id, "mcp_queue",
            lambda: self._persist_for_agents(event_type, event_payload, project_id, event_id),
        ))
        results = await asyncio.gather(*deliveries, return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise RuntimeError("; ".join(str(error) for error in errors))

    async def _safe_call(self, callback: Callable, payload: Dict):
        """Call a subscriber callback safely, supporting both sync and async."""
        try:
            if asyncio.iscoroutinefunction(callback):
                await callback(payload)
            else:
                callback(payload)
        except Exception as e:
            logger.error(f"Error in event subscriber {callback.__name__}: {e}")
            raise

    async def _broadcast_websocket(self, payload: Dict):
        """Broadcast event to UI clients via WebSockets."""
        if not self._websocket_manager:
            return
        
        project_id = payload.get("project_id")
        if project_id:
            await self._websocket_manager.broadcast_to_project(payload, project_id)
        else:
            await self._websocket_manager.broadcast_to_all(payload)

    async def _persist_for_agents(
        self,
        event_type: str,
        payload: Dict,
        project_id: Optional[int],
        outbox_event_id: int,
    ):
        """Persist event for agents who subscribe via MCP polling"""
        async with async_session_maker() as session:
            # Only query online MCP agents
            result = await session.execute(
                select(AgentConnection).filter(
                    AgentConnection.protocol == ProtocolType.MCP,
                    AgentConnection.status == ConnectionStatus.ONLINE
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

                    existing = await session.scalar(select(PendingEvent.id).where(
                        PendingEvent.outbox_event_id == outbox_event_id,
                        PendingEvent.agent_id == conn.entity_id,
                    ))
                    if existing is not None:
                        continue
                    session.add(PendingEvent(
                        outbox_event_id=outbox_event_id,
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
