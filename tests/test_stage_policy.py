"""Tests for Stage Policy data model, REST APIs, MCP tools, and architecture cleanup.

Covers AGENTS.md §11 tasks:
- B: StagePolicy model, schemas, seeding
- C: REST + MCP stage policy endpoints
- D: Transition validation
- A: No server-side auto-assignment, no auto-completion of cards
"""

import json
import pytest
from datetime import UTC, datetime

from models import (
    StagePolicy, ReviewMode, Project, Stage, Task, TaskStatus,
    Entity, EntityType, Role, ApprovalStatus, AgentSession,
    AgentSessionStatus, OrchestrationDecision, DecisionType,
)
from schemas import StagePolicyCreate, StagePolicyUpdate, StagePolicyResponse
from kanban_runtime.stage_policy import (
    normalize_stage_key, DEFAULT_POLICIES, validate_transition,
    seed_default_policies, policy_roles, policy_outputs,
)


class TestStagePolicyModel:
    def test_review_mode_enum_values(self):
        assert ReviewMode.NONE.value == "none"
        assert ReviewMode.AUTO.value == "auto"
        assert ReviewMode.HUMAN.value == "human"
        assert ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL.value == "auto_then_human_for_critical"

    def test_stage_policy_model_defaults(self):
        policy = StagePolicy(
            project_id=1,
            stage_id=1,
            stage_key="to_do",
            on_enter_roles_json="[]",
            required_outputs_json="[]",
            allow_parallel=False,
            requires_orchestrator_move=True,
        )
        assert policy.on_enter_roles_json == "[]"
        assert policy.required_outputs_json == "[]"
        assert policy.allow_parallel is False
        assert policy.requires_orchestrator_move is True

    def test_decision_type_stage_policy(self):
        assert DecisionType.STAGE_POLICY.value == "stage_policy"


class TestStagePolicySchemas:
    def test_create_schema_defaults(self):
        data = StagePolicyCreate(project_id=1, stage_id=1, stage_key="review")
        assert data.on_enter_roles == []
        assert data.required_outputs == []
        assert data.review_mode == ReviewMode.NONE
        assert data.allow_parallel is False
        assert data.requires_orchestrator_move is True

    def test_create_schema_with_data(self):
        data = StagePolicyCreate(
            project_id=1,
            stage_id=1,
            stage_key="review",
            on_enter_roles=["diff_review", "test"],
            required_outputs=["test_result", "diff_review_result"],
            review_mode=ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL,
            allow_parallel=True,
            requires_orchestrator_move=True,
        )
        assert data.on_enter_roles == ["diff_review", "test"]
        assert data.review_mode == ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL

    def test_update_schema_partial(self):
        data = StagePolicyUpdate(review_mode=ReviewMode.HUMAN)
        assert data.stage_key is None
        assert data.review_mode == ReviewMode.HUMAN

    def test_response_from_model(self):
        policy = StagePolicy(
            id=1,
            project_id=1,
            stage_id=2,
            stage_key="in_progress",
            on_enter_roles_json=json.dumps(["worker"]),
            required_outputs_json=json.dumps(["code_changes"]),
            review_mode=ReviewMode.NONE,
            allow_parallel=False,
            requires_orchestrator_move=True,
            created_at=datetime.now(UTC),
        )
        response = StagePolicyResponse.from_model(policy)
        assert response.id == 1
        assert response.stage_key == "in_progress"
        assert response.on_enter_roles == ["worker"]
        assert response.required_outputs == ["code_changes"]


class TestStagePolicyHelpers:
    def test_normalize_stage_key(self):
        assert normalize_stage_key("To Do") == "to_do"
        assert normalize_stage_key("todo") == "to_do"
        assert normalize_stage_key("In Progress") == "in_progress"
        assert normalize_stage_key("in_progress") == "in_progress"
        assert normalize_stage_key("Review") == "review"
        assert normalize_stage_key("Done") == "done"
        assert normalize_stage_key("Backlog") == "backlog"

    def test_default_policies_keys(self):
        expected = {"backlog", "to_do", "in_progress", "review", "done"}
        assert set(DEFAULT_POLICIES.keys()) == expected

    def test_default_policy_review_structure(self):
        review = DEFAULT_POLICIES["review"]
        assert review["review_mode"] == ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL
        assert "diff_review" in review["on_enter_roles"]
        assert "test" in review["on_enter_roles"]

    def test_default_policy_done_structure(self):
        done = DEFAULT_POLICIES["done"]
        assert "git_pr" in done["on_enter_roles"]
        assert done["requires_orchestrator_move"] is True

    def test_policy_roles_from_json(self):
        policy = StagePolicy(
            id=1, project_id=1, stage_id=1, stage_key="review",
            on_enter_roles_json=json.dumps(["diff_review", "test"]),
        )
        assert policy_roles(policy) == ["diff_review", "test"]

    def test_policy_roles_none(self):
        assert policy_roles(None) == []

    def test_policy_outputs_from_json(self):
        policy = StagePolicy(
            id=1, project_id=1, stage_id=1, stage_key="review",
            required_outputs_json=json.dumps(["test_result"]),
        )
        assert policy_outputs(policy) == ["test_result"]


class TestTransitionValidation:
    def test_transition_orchestrator_allowed_to_orchestrator_only_stage(self):
        to_policy = StagePolicy(
            project_id=1, stage_id=1, stage_key="backlog",
            on_enter_roles_json="[]", required_outputs_json="[]",
            requires_orchestrator_move=True,
        )
        result = validate_transition(
            from_policy=None,
            to_policy=to_policy,
            move_initiator="orchestrator",
        )
        assert result is None

    def test_transition_worker_blocked_from_orchestrator_move_stage(self):
        to_policy = StagePolicy(
            project_id=1, stage_id=1, stage_key="review",
            on_enter_roles_json="[]", required_outputs_json="[]",
            requires_orchestrator_move=True,
        )
        result = validate_transition(
            from_policy=None,
            to_policy=to_policy,
            move_initiator="worker",
        )
        assert result is not None
        assert "orchestrator" in result.lower() or "human" in result.lower()

    def test_transition_human_always_allowed(self):
        to_policy = StagePolicy(
            project_id=1, stage_id=1, stage_key="review",
            on_enter_roles_json="[]", required_outputs_json="[]",
            requires_orchestrator_move=True,
        )
        result = validate_transition(
            from_policy=None,
            to_policy=to_policy,
            move_initiator="human",
        )
        assert result is None

    def test_transition_critical_review_requires_human(self):
        to_policy = StagePolicy(
            project_id=1, stage_id=1, stage_key="done",
            on_enter_roles_json="[]", required_outputs_json="[]",
            review_mode=ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL,
            requires_orchestrator_move=True,
        )
        result = validate_transition(
            from_policy=None,
            to_policy=to_policy,
            move_initiator="worker",
            is_critical=True,
        )
        assert result is not None
        assert "human" in result.lower()

    def test_transition_missing_required_outputs(self):
        to_policy = StagePolicy(
            project_id=1, stage_id=1, stage_key="in_progress",
            on_enter_roles_json="[]",
            required_outputs_json=json.dumps(["implementation_plan"]),
            requires_orchestrator_move=True,
        )
        result = validate_transition(
            from_policy=None,
            to_policy=to_policy,
            move_initiator="orchestrator",
            has_required_outputs=False,
        )
        assert result is not None
        assert "implementation_plan" in result

    def test_transition_review_mode_human_blocks_worker(self):
        to_policy = StagePolicy(
            project_id=1, stage_id=1, stage_key="done",
            on_enter_roles_json="[]", required_outputs_json="[]",
            review_mode=ReviewMode.HUMAN,
            requires_orchestrator_move=True,
        )
        result = validate_transition(
            from_policy=None,
            to_policy=to_policy,
            move_initiator="orchestrator",
        )
        assert result is not None
        assert "human" in result.lower()


class TestArchitectureCleanup:
    def test_select_role_for_task_always_returns_worker(self):
        from kanban_runtime.assignment_launcher import _select_role_for_task

        class FakeTask:
            title = "Implement OAuth login UI test"
            description = "Add auth and frontend review"
            required_skills = "test ui auth"

        result = _select_role_for_task(FakeTask())
        assert result == "worker"

    def test_select_role_for_task_no_text_matching(self):
        from kanban_runtime.assignment_launcher import _select_role_for_task

        class FakeTask:
            title = "Fix typo"
            description = None
            required_skills = None

        result = _select_role_for_task(FakeTask())
        assert result == "worker"

    def test_finalize_session_does_not_complete_task(self):
        """Verify _finalize_completed_session does not change task status
        or stage — it only marks the session done and records activity."""
        from kanban_runtime.session_streamer import _finalize_completed_session
        import inspect
        source = inspect.getsource(_finalize_completed_session)
        assert "TaskStatus.COMPLETED" not in source
        assert "task.status" not in source or "task.status == TaskStatus.COMPLETED" in source
        assert "task.stage_id" not in source

    def test_assign_orphaned_tasks_is_report_only(self):
        """Verify assign_orphaned_tasks is report-only: does not auto-assign."""
        from kanban_runtime.assignment_launcher import AssignmentLauncher
        import inspect
        source = inspect.getsource(AssignmentLauncher.assign_orphaned_tasks)
        assert "Auto-assigned" not in source or "Reported" in source
        assert "Orchestrator or human should assign" in source

    def test_scan_and_advance_does_not_move_cards(self):
        """Verify scan_and_advance_completed_tasks does not move cards."""
        from kanban_runtime.assignment_launcher import AssignmentLauncher
        import inspect
        source = inspect.getsource(AssignmentLauncher.scan_and_advance_completed_tasks)
        assert "task.stage_id" not in source
        assert "Auto-advanced" not in source

    def test_assignment_launcher_supports_non_git_workspace_fallback(self):
        from kanban_runtime.assignment_launcher import AssignmentLauncher
        import inspect
        source = inspect.getsource(AssignmentLauncher.launch_for_assignment)
        assert "workspace_path = project.path" in source
        assert "project workspace" in source

    def test_assignment_launcher_uses_assigned_role_env(self):
        from kanban_runtime.assignment_launcher import AssignmentLauncher
        import inspect
        source = inspect.getsource(AssignmentLauncher.handle_event)
        assert "assigned_role=data.get(\"role\")" in source
        source = inspect.getsource(AssignmentLauncher.launch_for_assignment)
        assert "env[\"KANBAN_AGENT_ROLE\"] = matching_role_name" in source
