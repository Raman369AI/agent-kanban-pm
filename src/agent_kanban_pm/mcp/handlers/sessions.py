"""MCP tool handlers: sessions."""

import logging
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Project, Role,
    AgentSession, AgentSessionStatus, AgentActivity, ActivityType
)
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)


class SessionHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_start_agent_session(self, args: dict) -> dict:
        """Start a durable CLI-agent session for visibility."""
        agent_id = self._target_agent_id(args)
        project_id = args["project_id"]

        async with async_session_maker() as db:
            project_result = await db.execute(select(Project).filter(Project.id == project_id))
            project = project_result.scalar_one_or_none()
            if not project:
                return {"error": "Project not found"}

            workspace_path = args.get("workspace_path") or project.path
            if not workspace_path:
                return {"error": "workspace_path is required when the project has no path"}

            task_id = args.get("task_id")
            if task_id is not None:
                existing = await db.scalar(
                    select(AgentSession).filter(
                        AgentSession.agent_id == agent_id,
                        AgentSession.task_id == task_id,
                        AgentSession.ended_at.is_(None),
                    )
                )
                if existing:
                    return {
                        "error": "An active session already exists for this assignment",
                        "session_id": existing.id,
                    }

            session = AgentSession(
                agent_id=agent_id,
                project_id=project_id,
                task_id=task_id,
                workspace_path=workspace_path,
                command=args.get("command"),
                model=args.get("model"),
                mode=args.get("mode"),
                status=AgentSessionStatus.ACTIVE,
            )
            db.add(session)
            try:
                await db.flush()
                event_bus.enqueue(db,
                    EventType.AGENT_ACTIVITY_LOGGED.value,
                    {
                        "agent_id": agent_id,
                        "session_id": session.id,
                        "project_id": project_id,
                        "task_id": session.task_id,
                        "activity_type": "session_started",
                        "message": f"Session started in {workspace_path}",
                        "workspace_path": workspace_path,
                        "command": session.command,
                    },
                    project_id=project_id,
                    entity_id=agent_id
                )
                await db.commit()
            except IntegrityError:
                await db.rollback()
                if task_id is not None:
                    existing = await db.scalar(
                        select(AgentSession).filter(
                            AgentSession.agent_id == agent_id,
                            AgentSession.task_id == task_id,
                            AgentSession.ended_at.is_(None),
                        )
                    )
                    if existing:
                        return {
                            "error": "An active session was created concurrently for this assignment",
                            "session_id": existing.id,
                        }
                raise
            await db.refresh(session)

            return {
                "success": True,
                "session_id": session.id,
                "agent_id": agent_id,
                "project_id": project_id,
                "task_id": session.task_id,
                "workspace_path": workspace_path,
                "status": session.status.value,
            }

    async def _handle_end_agent_session(self, args: dict) -> dict:
        """End or update a durable CLI-agent session."""
        session_id = args["session_id"]
        status_value = args.get("status", "done")

        async with async_session_maker() as db:
            result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
            session = result.scalar_one_or_none()
            if not session:
                return {"error": "Session not found"}
            if session.agent_id != self.caller_entity.id:
                self._require_role(Role.MANAGER)

            session.status = AgentSessionStatus(status_value)
            session.last_seen_at = datetime.now(UTC)
            if session.status in (AgentSessionStatus.DONE, AgentSessionStatus.ERROR):
                session.ended_at = datetime.now(UTC)

            if args.get("message"):
                activity = AgentActivity(
                    agent_id=session.agent_id,
                    session_id=session.id,
                    project_id=session.project_id,
                    task_id=session.task_id,
                    activity_type=ActivityType.RESULT if session.status == AgentSessionStatus.DONE else ActivityType.ERROR,
                    source="session_update",
                    message=args["message"],
                    workspace_path=session.workspace_path,
                )
                db.add(activity)

            event_bus.enqueue(db,
                EventType.AGENT_STATUS_UPDATED.value,
                {
                    "agent_id": session.agent_id,
                    "session_id": session.id,
                    "project_id": session.project_id,
                    "task_id": session.task_id,
                    "status_type": session.status.value,
                    "message": args.get("message"),
                    "workspace_path": session.workspace_path,
                },
                project_id=session.project_id,
                entity_id=session.agent_id
            )
            await db.commit()

            return {"success": True, "session_id": session.id, "status": session.status.value}

    async def _handle_get_agent_sessions(self, args: dict) -> list:
        """Get active or recent agent sessions."""
        async with async_session_maker() as db:
            query = select(AgentSession).order_by(AgentSession.last_seen_at.desc())
            if "agent_id" in args:
                query = query.filter(AgentSession.agent_id == args["agent_id"])
            if "project_id" in args:
                query = query.filter(AgentSession.project_id == args["project_id"])
            if "task_id" in args:
                query = query.filter(AgentSession.task_id == args["task_id"])
            if args.get("active_only"):
                query = query.filter(AgentSession.ended_at.is_(None))
            result = await db.execute(query.limit(args.get("limit", 50)))
            sessions = result.scalars().all()
            return [
                {
                    "id": s.id,
                    "agent_id": s.agent_id,
                    "project_id": s.project_id,
                    "task_id": s.task_id,
                    "workspace_path": s.workspace_path,
                    "status": s.status.value,
                    "command": s.command,
                    "model": s.model,
                    "mode": s.mode,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                }
                for s in sessions
            ]

    async def _handle_get_project_activity(self, args: dict) -> list:
        """Get structured orchestration feed for a project."""
        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentActivity)
                .filter(AgentActivity.project_id == args["project_id"])
                .order_by(AgentActivity.created_at.desc())
                .limit(args.get("limit", 100))
            )
            activities = result.scalars().all()
            return [
                {
                    "id": a.id,
                    "agent_id": a.agent_id,
                    "session_id": a.session_id,
                    "project_id": a.project_id,
                    "task_id": a.task_id,
                    "activity_type": a.activity_type.value,
                    "source": a.source,
                    "message": a.message,
                    "workspace_path": a.workspace_path,
                    "file_path": a.file_path,
                    "command": a.command,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in activities
            ]
