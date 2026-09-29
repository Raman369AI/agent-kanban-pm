"""MCP tool handlers: coordination."""

import json
import logging
from datetime import UTC, datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Task, Role,
    OrchestrationDecision, DecisionType, TaskLease, LeaseStatus,
    ActivitySummary, UserContribution, ContributionType
)
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)


class CoordinationHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_record_decision(self, args: dict) -> dict:
        """Record a durable manager decision and rationale."""
        self._require_role(Role.MANAGER)
        project_id = args["project_id"]
        affected_task_ids = args.get("affected_task_ids")
        affected_agent_ids = args.get("affected_agent_ids")
        async with async_session_maker() as db:
            decision = OrchestrationDecision(
                project_id=project_id,
                manager_agent_id=self.caller_entity.id,
                decision_type=DecisionType(args.get("decision_type", "other")),
                input_summary=args.get("input_summary"),
                rationale=args["rationale"],
                affected_task_ids=json.dumps(affected_task_ids) if affected_task_ids is not None else None,
                affected_agent_ids=json.dumps(affected_agent_ids) if affected_agent_ids is not None else None,
            )
            db.add(decision)
            await db.flush()
            event_bus.enqueue(db,
                EventType.ORCHESTRATION_DECISION_LOGGED.value,
                {
                    "decision_id": decision.id,
                    "project_id": project_id,
                    "decision_type": decision.decision_type.value,
                    "rationale": decision.rationale,
                    "affected_task_ids": decision.affected_task_ids,
                    "affected_agent_ids": decision.affected_agent_ids,
                },
                project_id=project_id,
                entity_id=self.caller_entity.id
            )
            await db.commit()
            await db.refresh(decision)
            return {"success": True, "decision_id": decision.id}

    async def _handle_claim_task(self, args: dict) -> dict:
        """Claim an active task lease."""
        self._require_role(Role.WORKER)
        agent_id = self._target_agent_id(args)
        task_id = args["task_id"]
        now = datetime.now(UTC)
        async with async_session_maker() as db:
            task_result = await db.execute(select(Task).filter(Task.id == task_id))
            task = task_result.scalar_one_or_none()
            if not task:
                return {"error": "Task not found"}

            active_result = await db.execute(
                select(TaskLease).filter(
                    TaskLease.task_id == task_id,
                    TaskLease.status == LeaseStatus.ACTIVE,
                    TaskLease.expires_at > now,
                    TaskLease.agent_id != agent_id,
                )
            )
            active = active_result.scalar_one_or_none()
            if active:
                return {"error": f"Task is already leased by agent {active.agent_id}", "lease_id": active.id}

            existing_result = await db.execute(
                select(TaskLease).filter(
                    TaskLease.task_id == task_id,
                    TaskLease.agent_id == agent_id,
                    TaskLease.status == LeaseStatus.ACTIVE,
                )
            )
            for existing in existing_result.scalars().all():
                existing.status = LeaseStatus.RELEASED
                existing.released_at = now
            await db.flush()

            lease = TaskLease(
                task_id=task_id,
                agent_id=agent_id,
                session_id=args.get("session_id"),
                status=LeaseStatus.ACTIVE,
                expires_at=now + timedelta(seconds=max(60, args.get("ttl_seconds", 1800))),
            )
            db.add(lease)
            await db.flush()
            event_bus.enqueue(db,
                EventType.TASK_LEASE_UPDATED.value,
                {
                    "lease_id": lease.id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "session_id": lease.session_id,
                    "status": lease.status.value,
                    "expires_at": lease.expires_at.isoformat(),
                },
                project_id=task.project_id,
                entity_id=agent_id
            )
            await db.commit()
            await db.refresh(lease)
            return {"success": True, "lease_id": lease.id, "expires_at": lease.expires_at.isoformat()}

    async def _handle_release_task(self, args: dict) -> dict:
        """Release a task lease."""
        async with async_session_maker() as db:
            result = await db.execute(select(TaskLease).filter(TaskLease.id == args["lease_id"]).options(selectinload(TaskLease.task)))
            lease = result.scalar_one_or_none()
            if not lease:
                return {"error": "Lease not found"}
            if lease.agent_id != self.caller_entity.id:
                self._require_role(Role.MANAGER)
            lease.status = LeaseStatus.RELEASED
            lease.released_at = datetime.now(UTC)
            event_bus.enqueue(db,
                EventType.TASK_LEASE_UPDATED.value,
                {"lease_id": lease.id, "task_id": lease.task_id, "agent_id": lease.agent_id, "status": lease.status.value},
                project_id=lease.task.project_id if lease.task else None,
                entity_id=lease.agent_id
            )
            await db.commit()
            return {"success": True, "lease_id": lease.id, "status": lease.status.value}

    async def _handle_summarize_activity(self, args: dict) -> dict:
        """Store a summarized activity timeline entry."""
        agent_id = args.get("agent_id") or self.caller_entity.id
        if agent_id != self.caller_entity.id:
            self._require_role(Role.MANAGER)
        async with async_session_maker() as db:
            summary = ActivitySummary(
                project_id=args["project_id"],
                task_id=args.get("task_id"),
                agent_id=agent_id,
                summary=args["summary"],
                from_activity_id=args.get("from_activity_id"),
                to_activity_id=args.get("to_activity_id"),
            )
            db.add(summary)
            await db.flush()
            event_bus.enqueue(db,
                EventType.ACTIVITY_SUMMARY_CREATED.value,
                {"summary_id": summary.id, "project_id": summary.project_id, "summary": summary.summary},
                project_id=summary.project_id,
                entity_id=agent_id
            )
            await db.commit()
            await db.refresh(summary)
            return {"success": True, "summary_id": summary.id}

    async def _handle_log_contribution(self, args: dict) -> dict:
        """Record a GitHub/SCM contribution."""
        entity_id = args.get("entity_id") or self.caller_entity.id
        if entity_id != self.caller_entity.id:
            self._require_role(Role.MANAGER)
        async with async_session_maker() as db:
            contribution = UserContribution(
                project_id=args["project_id"],
                entity_id=entity_id,
                contribution_type=ContributionType(args["contribution_type"]),
                provider=args.get("provider", "github"),
                external_id=args.get("external_id"),
                title=args["title"],
                url=args.get("url"),
                status=args.get("status"),
            )
            db.add(contribution)
            await db.flush()
            event_bus.enqueue(db,
                EventType.USER_CONTRIBUTION_LOGGED.value,
                {
                    "contribution_id": contribution.id,
                    "project_id": contribution.project_id,
                    "entity_id": contribution.entity_id,
                    "contribution_type": contribution.contribution_type.value,
                    "title": contribution.title,
                    "url": contribution.url,
                    "status": contribution.status,
                },
                project_id=contribution.project_id,
                entity_id=entity_id
            )
            await db.commit()
            await db.refresh(contribution)
            return {"success": True, "contribution_id": contribution.id}
