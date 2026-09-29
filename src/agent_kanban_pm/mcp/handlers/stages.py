"""MCP tool handlers: stages."""

import logging

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Role,
    OrchestrationDecision, DecisionType
)
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)


class StageHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_get_stage_policies(self, args: dict) -> dict:
        """Return stage policies for a project."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        async with async_session_maker() as db:
            from agent_kanban_pm.runtime.stage_policy import get_stage_policies
            policies = await get_stage_policies(db, project_id)
            from agent_kanban_pm.schemas import StagePolicyResponse
            return {"policies": [StagePolicyResponse.from_model(p).model_dump() for p in policies]}

    async def _handle_record_stage_policy_decision(self, args: dict) -> dict:
        """Record an orchestrator decision about a stage transition."""
        self._require_role(Role.MANAGER)
        project_id = args.get("project_id")
        task_id = args.get("task_id")
        from_stage_id = args.get("from_stage_id")
        to_stage_id = args.get("to_stage_id")
        selected_roles = args.get("selected_roles", [])
        rationale = args.get("rationale", "")
        if not project_id or not rationale:
            return {"error": "project_id and rationale are required"}
        async with async_session_maker() as db:
            decision = OrchestrationDecision(
                project_id=project_id,
                manager_agent_id=self.caller_entity.id,
                decision_type=DecisionType.STAGE_POLICY,
                input_summary=f"Stage transition from {from_stage_id} to {to_stage_id}",
                rationale=rationale,
                affected_task_ids=str(task_id) if task_id else None,
                affected_agent_ids=None,
            )
            db.add(decision)
            await db.flush()
            event_bus.enqueue(db,
                EventType.STAGE_POLICY_UPDATED.value,
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "from_stage_id": from_stage_id,
                    "to_stage_id": to_stage_id,
                    "selected_roles": selected_roles,
                    "decision_id": decision.id,
                },
                project_id=project_id,
                entity_id=self.caller_entity.id,
            )
            await db.commit()
            await db.refresh(decision)
            return {
                "success": True,
                "decision_id": decision.id,
                "message": "Stage transition decision recorded.",
            }

    async def _handle_get_transition_validation(self, args: dict) -> dict:
        """Validate a stage transition against project stage policies."""
        project_id = args.get("project_id")
        from_stage_id = args.get("from_stage_id")
        to_stage_id = args.get("to_stage_id")
        move_initiator = args.get("move_initiator", "orchestrator")
        has_required_outputs = args.get("has_required_outputs", True)
        has_diff_review = args.get("has_diff_review", False)
        is_critical = args.get("is_critical", False)
        if not project_id or not from_stage_id or not to_stage_id:
            return {"error": "project_id, from_stage_id, and to_stage_id are required"}
        from agent_kanban_pm.runtime.stage_policy import get_stage_policy_for_stage, validate_transition
        async with async_session_maker() as db:
            to_policy = await get_stage_policy_for_stage(db, project_id, to_stage_id)
            from_policy = await get_stage_policy_for_stage(db, project_id, from_stage_id)
            error = validate_transition(
                from_policy=from_policy,
                to_policy=to_policy,
                move_initiator=move_initiator,
                has_required_outputs=has_required_outputs,
                has_diff_review=has_diff_review,
                is_critical=is_critical,
            )
            if error:
                return {"valid": False, "reason": error}
            return {"valid": True}
