"""MCP tool handlers: projects."""

import logging
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Project, Task, Stage, ApprovalStatus, Role,
    OrchestrationDecision, TaskLease, LeaseStatus,
    ActivitySummary, UserContribution, ProjectWorkspace
)
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.runtime.default_stages import DEFAULT_STAGES

logger = logging.getLogger(__name__)


class ProjectHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_create_project(self, args: dict) -> dict:
        """Create a new project using ORM"""
        self._require_role(Role.MANAGER)
        creator_id = self.caller_entity.id
        async with async_session_maker() as db:
            project = Project(
                name=args["name"],
                description=args.get("description", ""),
                creator_id=creator_id,
                approval_status=ApprovalStatus.PENDING
            )
            db.add(project)
            await db.flush()

            # Create default stages
            for stage_data in DEFAULT_STAGES:
                stage = Stage(project_id=project.id, **stage_data)
                db.add(stage)

            event_bus.enqueue(db,
                EventType.PROJECT_CREATED.value,
                {"project_id": project.id, "name": project.name},
                project_id=project.id
            )
            await db.commit()
            await db.refresh(project)

            return {
                "success": True,
                "project_id": project.id,
                "message": f"Project '{project.name}' created successfully",
                "stages": len(DEFAULT_STAGES)
            }

    async def _handle_get_projects(self, args: dict) -> list:
        """Get all projects using ORM"""
        async with async_session_maker() as db:
            query = select(Project)
            if "status" in args:
                query = query.filter(Project.approval_status == args["status"].upper())

            result = await db.execute(query.order_by(Project.created_at.desc()))
            projects = result.scalars().all()

            return [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "approval_status": str(p.approval_status),
                    "created_at": p.created_at.isoformat() if p.created_at else None
                }
                for p in projects
            ]

    async def _handle_get_project_details(self, args: dict) -> dict:
        """Get detailed project information using ORM"""
        async with async_session_maker() as db:
            result = await db.execute(
                select(Project)
                .filter(Project.id == args["project_id"])
                .options(
                    selectinload(Project.stages),
                    selectinload(Project.tasks).selectinload(Task.assignees)
                )
            )
            project = result.scalar_one_or_none()

            if not project:
                return {"error": "Project not found"}

            return {
                "project": {
                    "id": project.id,
                    "name": project.name,
                    "description": project.description,
                    "approval_status": str(project.approval_status),
                },
                "stages": [
                    {"id": s.id, "name": s.name, "order": s.order}
                    for s in project.stages
                ],
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": str(t.status),
                        "priority": t.priority,
                        "stage_id": t.stage_id,
                        "assignees": [a.name for a in t.assignees]
                    }
                    for t in project.tasks
                ]
            }


    async def _handle_approve_project(self, args: dict) -> dict:
        """Approve a project using ORM and publish event"""
        self._require_role(Role.MANAGER)
        async with async_session_maker() as db:
            result = await db.execute(select(Project).filter(Project.id == args["project_id"]))
            project = result.scalar_one_or_none()
            if not project:
                return {"error": "Project not found"}

            project.approval_status = ApprovalStatus.APPROVED
            project.updated_at = datetime.now(UTC)
            event_bus.enqueue(db,
                EventType.PROJECT_UPDATED.value,
                {"project_id": project.id, "status": "approved"},
                project_id=project.id
            )
            await db.commit()

            return {"success": True, "project_id": args["project_id"], "status": "approved"}


    async def _handle_get_project_context(self, args: dict) -> dict:
        """Get the coordination context the manager and UI need."""
        project_id = args["project_id"]
        limit = args.get("limit", 20)
        now = datetime.now(UTC)
        async with async_session_maker() as db:
            workspaces = (await db.execute(
                select(ProjectWorkspace).filter(ProjectWorkspace.project_id == project_id).order_by(ProjectWorkspace.is_primary.desc(), ProjectWorkspace.created_at)
            )).scalars().all()
            decisions = (await db.execute(
                select(OrchestrationDecision).filter(OrchestrationDecision.project_id == project_id).order_by(OrchestrationDecision.created_at.desc()).limit(limit)
            )).scalars().all()
            leases = (await db.execute(
                select(TaskLease).join(Task, Task.id == TaskLease.task_id).filter(
                    Task.project_id == project_id,
                    TaskLease.status == LeaseStatus.ACTIVE,
                    TaskLease.expires_at > now,
                ).order_by(TaskLease.created_at.desc())
            )).scalars().all()
            summaries = (await db.execute(
                select(ActivitySummary).filter(ActivitySummary.project_id == project_id).order_by(ActivitySummary.created_at.desc()).limit(limit)
            )).scalars().all()
            contributions = (await db.execute(
                select(UserContribution).filter(UserContribution.project_id == project_id).order_by(UserContribution.recorded_at.desc()).limit(limit)
            )).scalars().all()
            return {
                "workspaces": [
                    {"id": w.id, "root_path": w.root_path, "label": w.label, "is_primary": w.is_primary}
                    for w in workspaces
                ],
                "decisions": [
                    {"id": d.id, "decision_type": d.decision_type.value, "rationale": d.rationale, "created_at": d.created_at.isoformat() if d.created_at else None}
                    for d in decisions
                ],
                "leases": [
                    {"id": l.id, "task_id": l.task_id, "agent_id": l.agent_id, "session_id": l.session_id, "expires_at": l.expires_at.isoformat() if l.expires_at else None}
                    for l in leases
                ],
                "summaries": [
                    {"id": s.id, "task_id": s.task_id, "agent_id": s.agent_id, "summary": s.summary, "created_at": s.created_at.isoformat() if s.created_at else None}
                    for s in summaries
                ],
                "contributions": [
                    {"id": c.id, "entity_id": c.entity_id, "contribution_type": c.contribution_type.value, "title": c.title, "url": c.url, "status": c.status}
                    for c in contributions
                ],
            }
