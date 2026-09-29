"""MCP tool handlers: agents."""

import json
import logging
from datetime import UTC, datetime
from sqlalchemy import select

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Task, Entity, EntityType,
    AgentConnection, ProtocolType, ConnectionStatus, PendingEvent, Role,
    AgentActivity, ActivityType
)
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)


class AgentHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_get_pending_events(self, args: dict) -> list:
        """Poll for recent events for an agent (reads from shared DB).

        Uses soft-delete via consumed_at column — events are not deleted on read,
        allowing multi-consumer scenarios. The background sweeper handles cleanup.
        """
        agent_id = self._target_agent_id(args)
        limit = args.get("limit", 50)

        async with async_session_maker() as db:
            result = await db.execute(
                select(PendingEvent)
                .filter(
                    PendingEvent.agent_id == agent_id,
                    PendingEvent.consumed_at.is_(None),
                )
                .order_by(PendingEvent.created_at.asc())
                .limit(limit)
            )
            pending = result.scalars().all()

            events = []
            now = datetime.now(UTC)
            for pe in pending:
                try:
                    payload = json.loads(pe.payload)
                    payload["pending_event_id"] = pe.id
                    events.append(payload)
                    pe.consumed_at = now  # Soft-delete: mark consumed, don't delete
                except Exception as exc:
                    logger.warning(
                        "Skipping PendingEvent id=%s with malformed payload: %s",
                        pe.id, exc,
                    )
                    # Do NOT delete — preserve for debugging / manual cleanup

            await db.commit()

        return events

    async def _handle_register_subscription(self, args: dict) -> dict:
        """Register event subscriptions for an MCP agent"""
        agent_id = self._target_agent_id(args)
        events = args["events"]
        projects = args.get("projects")

        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentConnection).filter(
                    AgentConnection.entity_id == agent_id,
                    AgentConnection.protocol == ProtocolType.MCP
                )
            )
            conn = result.scalar_one_or_none()

            if not conn:
                conn = AgentConnection(
                    entity_id=agent_id,
                    protocol=ProtocolType.MCP,
                    config="{}",
                    status=ConnectionStatus.ONLINE
                )
                db.add(conn)

            conn.subscribed_events = json.dumps(events)
            conn.subscribed_projects = json.dumps(projects) if projects else None
            conn.last_seen = datetime.now(UTC)
            await db.commit()

        return {"success": True, "message": "Subscriptions updated", "events": events}

    async def _handle_list_agents(self, args: dict) -> list:
        """List all registered agents"""
        async with async_session_maker() as db:
            result = await db.execute(
                select(Entity).filter(Entity.entity_type == EntityType.AGENT, Entity.is_active == True)
            )
            agents = result.scalars().all()
            return [
                {"id": a.id, "name": a.name, "skills": a.skills}
                for a in agents
            ]

    async def _handle_list_entities(self, args: dict) -> list:
        """List all entities"""
        async with async_session_maker() as db:
            result = await db.execute(select(Entity).filter(Entity.is_active == True))
            entities = result.scalars().all()
            return [
                {"id": e.id, "name": e.name, "type": str(e.entity_type), "skills": e.skills}
                for e in entities
            ]

    async def _handle_report_status(self, args: dict) -> dict:
        """Upsert agent heartbeat"""
        from agent_kanban_pm.models import AgentHeartbeat, AgentStatusType
        agent_id = self._target_agent_id(args)
        status_type = AgentStatusType(args["status_type"])
        message = args.get("message")
        task_id = args.get("task_id")

        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentHeartbeat).filter(AgentHeartbeat.agent_id == agent_id)
            )
            heartbeat = result.scalar_one_or_none()

            if heartbeat:
                heartbeat.status_type = status_type
                heartbeat.message = message
                heartbeat.task_id = task_id
                heartbeat.updated_at = datetime.now(UTC)
            else:
                heartbeat = AgentHeartbeat(
                    agent_id=agent_id,
                    status_type=status_type,
                    message=message,
                    task_id=task_id
                )
                db.add(heartbeat)

            event_bus.enqueue(db,
                EventType.AGENT_STATUS_UPDATED.value,
                {
                    "agent_id": agent_id,
                    "status_type": status_type.value,
                    "message": message,
                    "task_id": task_id
                },
                entity_id=agent_id
            )
            await db.commit()
            await db.refresh(heartbeat)

            return {"success": True, "agent_id": agent_id, "status_type": status_type.value}

    async def _handle_log_activity(self, args: dict) -> dict:
        """Append agent activity log entry"""
        agent_id = self._target_agent_id(args)
        activity_type = ActivityType(args["activity_type"])
        message = args["message"]
        task_id = args.get("task_id")
        project_id = args.get("project_id")
        session_id = args.get("session_id")

        async with async_session_maker() as db:
            if task_id and project_id is None:
                task_result = await db.execute(select(Task).filter(Task.id == task_id))
                task = task_result.scalar_one_or_none()
                if task:
                    project_id = task.project_id

            activity = AgentActivity(
                agent_id=agent_id,
                session_id=session_id,
                project_id=project_id,
                activity_type=activity_type,
                message=message,
                task_id=task_id,
                source=args.get("source"),
                payload_json=args.get("payload_json"),
                workspace_path=args.get("workspace_path"),
                file_path=args.get("file_path"),
                command=args.get("command")
            )
            db.add(activity)
            await db.flush()
            event_bus.enqueue(db,
                EventType.AGENT_ACTIVITY_LOGGED.value,
                {
                    "agent_id": agent_id,
                    "activity_id": activity.id,
                    "session_id": session_id,
                    "project_id": project_id,
                    "activity_type": activity_type.value,
                    "message": message,
                    "task_id": task_id,
                    "source": args.get("source"),
                    "workspace_path": args.get("workspace_path"),
                    "file_path": args.get("file_path"),
                    "command": args.get("command")
                },
                project_id=project_id,
                entity_id=agent_id
            )
            await db.commit()
            await db.refresh(activity)

            return {"success": True, "activity_id": activity.id}


    async def _handle_get_agent_statuses(self, args: dict) -> list:
        """Get all agent heartbeats (manager polling)"""
        self._require_role(Role.MANAGER)
        from agent_kanban_pm.models import AgentHeartbeat
        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentHeartbeat).order_by(AgentHeartbeat.updated_at.desc())
            )
            heartbeats = result.scalars().all()
            return [
                {
                    "agent_id": h.agent_id,
                    "status_type": str(h.status_type),
                    "message": h.message,
                    "task_id": h.task_id,
                    "updated_at": h.updated_at.isoformat() if h.updated_at else None
                }
                for h in heartbeats
            ]

    async def _handle_get_activity_feed(self, args: dict) -> list:
        """Get activity feed with optional filters"""
        from agent_kanban_pm.models import AgentActivity
        from sqlalchemy import desc
        async with async_session_maker() as db:
            query = select(AgentActivity).order_by(desc(AgentActivity.created_at))
            if "agent_id" in args:
                query = query.filter(AgentActivity.agent_id == args["agent_id"])
            if "task_id" in args:
                query = query.filter(AgentActivity.task_id == args["task_id"])

            limit = args.get("limit", 50)
            result = await db.execute(query.limit(limit))
            activities = result.scalars().all()

            return [
                {
                    "id": a.id,
                    "agent_id": a.agent_id,
                    "activity_type": str(a.activity_type),
                    "message": a.message,
                    "task_id": a.task_id,
                    "created_at": a.created_at.isoformat() if a.created_at else None
                }
                for a in activities
            ]
