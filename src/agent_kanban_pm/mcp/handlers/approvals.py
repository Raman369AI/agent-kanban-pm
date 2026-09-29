"""MCP tool handlers: approvals."""

import logging
from datetime import UTC, datetime
from sqlalchemy import select

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Role,
    AgentSession, AgentSessionStatus, AgentApproval, AgentApprovalStatus, ApprovalType
)
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)


class ApprovalHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_request_approval(self, args: dict) -> dict:
        """Create an approval queue entry blocking the agent session until resolved."""
        project_id = args["project_id"]
        try:
            approval_type = ApprovalType(args.get("approval_type", "other"))
        except ValueError:
            approval_type = ApprovalType.OTHER
        async with async_session_maker() as db:
            from agent_kanban_pm.services.coordination import validate_approval_scope
            task_id = await validate_approval_scope(db, project_id, args.get("task_id"), args.get("session_id"), self.caller_entity.id)
            approval = AgentApproval(
                project_id=project_id,
                task_id=task_id,
                session_id=args.get("session_id"),
                agent_id=self.caller_entity.id,
                approval_type=approval_type,
                title=args["title"],
                message=args["message"],
                command=args.get("command"),
                diff_content=args.get("diff_content"),
                payload_json=args.get("payload_json"),
                status=AgentApprovalStatus.PENDING,
            )
            db.add(approval)
            await db.flush()
            await db.refresh(approval)

            session_id = args.get("session_id")
            if session_id:
                sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
                session_row = sess_result.scalar_one_or_none()
                if session_row and session_row.status != AgentSessionStatus.BLOCKED:
                    session_row.status = AgentSessionStatus.BLOCKED
                    session_row.last_seen_at = datetime.now(UTC)

            event_bus.enqueue(db,
                EventType.AGENT_APPROVAL_REQUESTED.value,
                {
                    "approval_id": approval.id,
                    "project_id": project_id,
                    "task_id": approval.task_id,
                    "session_id": approval.session_id,
                    "agent_id": approval.agent_id,
                    "approval_type": approval.approval_type.value,
                    "title": approval.title,
                    "message": approval.message,
                    "command": approval.command,
                },
                project_id=project_id,
                entity_id=approval.agent_id
            )
            await db.commit()
            await db.refresh(approval)
            return {
                "success": True,
                "approval_id": approval.id,
                "status": approval.status.value,
                "approval_type": approval.approval_type.value,
            }

    async def _handle_get_pending_approvals(self, args: dict) -> list:
        """List pending approvals scoped by project/agent/task/session."""
        async with async_session_maker() as db:
            query = (
                select(AgentApproval)
                .filter(AgentApproval.status == AgentApprovalStatus.PENDING)
                .order_by(AgentApproval.requested_at.desc())
            )
            for field, column in (
                ("project_id", AgentApproval.project_id),
                ("agent_id", AgentApproval.agent_id),
                ("task_id", AgentApproval.task_id),
                ("session_id", AgentApproval.session_id),
            ):
                if field in args and args[field] is not None:
                    query = query.filter(column == args[field])
            limit = args.get("limit", 50)
            result = await db.execute(query.limit(limit))
            approvals = result.scalars().all()
            return [
                {
                    "id": a.id,
                    "project_id": a.project_id,
                    "task_id": a.task_id,
                    "session_id": a.session_id,
                    "agent_id": a.agent_id,
                    "approval_type": a.approval_type.value,
                    "title": a.title,
                    "message": a.message,
                    "command": a.command,
                    "diff_content": a.diff_content,
                    "status": a.status.value,
                    "requested_at": a.requested_at.isoformat() if a.requested_at else None,
                }
                for a in approvals
            ]

    async def _handle_resolve_approval(self, args: dict) -> dict:
        """Resolve an approval. Used by managers/diff reviewers and the supervisor."""
        approval_id = args["approval_id"]
        try:
            decision = AgentApprovalStatus(args["decision"])
        except ValueError:
            return {"error": f"invalid decision: {args.get('decision')}"}
        if decision not in (
            AgentApprovalStatus.APPROVED,
            AgentApprovalStatus.REJECTED,
            AgentApprovalStatus.CANCELLED,
        ):
            return {"error": "decision must be approved, rejected, or cancelled"}

        async with async_session_maker() as db:
            result = await db.execute(select(AgentApproval).filter(AgentApproval.id == approval_id))
            approval = result.scalar_one_or_none()
            if not approval:
                return {"error": "Approval not found"}
            if approval.status != AgentApprovalStatus.PENDING:
                return {"error": f"Approval already {approval.status.value}"}

            if decision == AgentApprovalStatus.CANCELLED:
                if approval.agent_id != self.caller_entity.id:
                    self._require_role(Role.MANAGER)
            else:
                self._require_role(Role.MANAGER)

            # Optimistic concurrency: CAS on update_version
            expected_version = approval.update_version
            from sqlalchemy import update as sa_update
            cas_result = await db.execute(
                sa_update(AgentApproval)
                .where(AgentApproval.id == approval_id, AgentApproval.update_version == expected_version)
                .values(
                    status=decision,
                    resolved_at=datetime.now(UTC),
                    resolved_by_entity_id=self.caller_entity.id,
                    response_message=args.get("response_message"),
                    update_version=expected_version + 1,
                )
            )
            if cas_result.rowcount == 0:
                return {"error": "Approval was resolved by another request (concurrent modification)"}

            if approval.session_id:
                sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == approval.session_id))
                session_row = sess_result.scalar_one_or_none()
                if session_row and session_row.status == AgentSessionStatus.BLOCKED:
                    session_row.status = AgentSessionStatus.ACTIVE
                    session_row.last_seen_at = datetime.now(UTC)

            event_bus.enqueue(db,
                EventType.AGENT_APPROVAL_RESOLVED.value,
                {
                    "approval_id": approval.id,
                    "project_id": approval.project_id,
                    "task_id": approval.task_id,
                    "session_id": approval.session_id,
                    "agent_id": approval.agent_id,
                    "approval_type": approval.approval_type.value,
                    "status": approval.status.value,
                    "resolved_by_entity_id": self.caller_entity.id,
                    "response_message": approval.response_message,
                },
                project_id=approval.project_id,
                entity_id=approval.agent_id
            )
            await db.commit()
            await db.refresh(approval)
            return {
                "success": True,
                "approval_id": approval.id,
                "status": approval.status.value,
            }
