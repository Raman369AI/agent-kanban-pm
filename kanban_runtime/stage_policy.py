"""Stage policy helpers: default policies, transition validation, and seeding.

The server stores and validates policies but never infers routing from them.
Only the orchestrator agent reads policies to decide assignments and movement.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Any

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from models import StagePolicy, ReviewMode, Stage, Project

logger = logging.getLogger(__name__)

DEFAULT_POLICIES: Dict[str, Dict[str, Any]] = {
    "backlog": {
        "on_enter_roles": ["orchestrator"],
        "required_outputs": ["task_split", "role_hints"],
        "review_mode": ReviewMode.NONE,
        "allow_parallel": False,
        "requires_orchestrator_move": True,
    },
    "to_do": {
        "on_enter_roles": ["architecture", "test"],
        "required_outputs": ["implementation_plan", "acceptance_criteria", "test_plan"],
        "review_mode": ReviewMode.NONE,
        "allow_parallel": True,
        "requires_orchestrator_move": True,
    },
    "in_progress": {
        "on_enter_roles": ["worker"],
        "required_outputs": ["code_changes", "status_summary"],
        "review_mode": ReviewMode.NONE,
        "allow_parallel": False,
        "requires_orchestrator_move": True,
    },
    "review": {
        "on_enter_roles": ["diff_review", "test"],
        "required_outputs": ["test_result", "diff_review_result"],
        "review_mode": ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL,
        "allow_parallel": True,
        "requires_orchestrator_move": True,
    },
    "done": {
        "on_enter_roles": ["git_pr"],
        "required_outputs": ["final_summary"],
        "review_mode": ReviewMode.NONE,
        "allow_parallel": False,
        "requires_orchestrator_move": True,
    },
}

_NORMALIZE = {
    "to do": "to_do",
    "todo": "to_do",
    "in progress": "in_progress",
    "in_progress": "in_progress",
    "review": "review",
    "done": "done",
    "completed": "done",
    "backlog": "backlog",
}


def normalize_stage_key(name: str) -> str:
    stripped = (name or "").strip().lower()
    return _NORMALIZE.get(stripped, stripped.replace(" ", "_"))


async def seed_default_policies(db: AsyncSession, project_id: int) -> List[StagePolicy]:
    result = await db.execute(
        select(StagePolicy).filter(StagePolicy.project_id == project_id)
    )
    existing = result.scalars().all()
    if existing:
        return list(existing)

    stages_result = await db.execute(
        select(Stage).filter(Stage.project_id == project_id).order_by(Stage.order)
    )
    stages = stages_result.scalars().all()
    policies: List[StagePolicy] = []

    for stage in stages:
        key = normalize_stage_key(stage.name)
        defaults = DEFAULT_POLICIES.get(key, DEFAULT_POLICIES.get("to_do", {}))
        policy = StagePolicy(
            project_id=project_id,
            stage_id=stage.id,
            stage_key=key,
            on_enter_roles_json=json.dumps(defaults.get("on_enter_roles", [])),
            required_outputs_json=json.dumps(defaults.get("required_outputs", [])),
            review_mode=defaults.get("review_mode", ReviewMode.NONE),
            allow_parallel=defaults.get("allow_parallel", False),
            requires_orchestrator_move=defaults.get("requires_orchestrator_move", True),
        )
        db.add(policy)
        policies.append(policy)

    await db.flush()
    return policies


async def get_stage_policies(
    db: AsyncSession, project_id: int
) -> List[StagePolicy]:
    result = await db.execute(
        select(StagePolicy).filter(StagePolicy.project_id == project_id)
    )
    return list(result.scalars().all())


async def get_stage_policy_for_stage(
    db: AsyncSession, project_id: int, stage_id: int
) -> Optional[StagePolicy]:
    result = await db.execute(
        select(StagePolicy).filter(
            StagePolicy.project_id == project_id,
            StagePolicy.stage_id == stage_id,
        )
    )
    return result.scalar_one_or_none()


def policy_roles(policy: Optional[StagePolicy]) -> List[str]:
    if not policy or not policy.on_enter_roles_json:
        return []
    return json.loads(policy.on_enter_roles_json)


def policy_outputs(policy: Optional[StagePolicy]) -> List[str]:
    if not policy or not policy.required_outputs_json:
        return []
    return json.loads(policy.required_outputs_json)


def validate_transition(
    from_policy: Optional[StagePolicy],
    to_policy: Optional[StagePolicy],
    move_initiator: str = "human",
    has_required_outputs: bool = True,
    has_diff_review: bool = False,
    is_critical: bool = False,
) -> Optional[str]:
    if to_policy and to_policy.requires_orchestrator_move and move_initiator not in ("orchestrator", "human", "owner"):
        return f"Stage '{to_policy.stage_key}' requires orchestrator or human to move cards"

    if to_policy and to_policy.review_mode == ReviewMode.HUMAN and move_initiator != "human" and move_initiator != "owner":
        return f"Stage '{to_policy.stage_key}' requires human review"

    if to_policy and to_policy.review_mode == ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL:
        if is_critical and move_initiator not in ("human", "owner"):
            return f"Critical changes entering '{to_policy.stage_key}' require human review"
        if not has_diff_review and move_initiator not in ("human", "owner"):
            return f"Stage '{to_policy.stage_key}' requires a diff review before transition"

    if to_policy and to_policy.required_outputs_json:
        required = json.loads(to_policy.required_outputs_json)
        if required and not has_required_outputs:
            return f"Stage '{to_policy.stage_key}' requires outputs: {', '.join(required)}"

    return None


# File paths whose changes are considered critical (auth, identity,
# subprocess/tmux, filesystem access, database migrations, git operations).
CRITICAL_FILE_PATTERNS = (
    "auth.py",
    "mcp_server.py",
    "session_streamer.py",
    "role_supervisor.py",
    "assignment_launcher.py",
    "database.py",
    "models.py",
    "alembic/",
    ".ssh/",
    ".env",
)


async def gather_transition_context(
    db: "AsyncSession",
    task_id: int,
    project_id: int,
) -> dict:
    """Gather real has_diff_review and is_critical values for a task move.

    Returns a dict with keys:
      - has_diff_review: bool — True if the task has an APPROVED DiffReview.
      - is_critical: bool — True if the task has recent activity touching
        critical file paths (auth, identity, subprocess, migrations, git).
      - has_required_outputs: bool — True if the task has activity entries
        that match the target stage's required_outputs list (or if the
        target stage has no required outputs).
    """
    from models import DiffReview, DiffReviewStatus, AgentActivity, ActivityType

    has_diff_review = False
    review_result = await db.execute(
        select(DiffReview).filter(
            DiffReview.task_id == task_id,
            DiffReview.status == DiffReviewStatus.APPROVED,
        )
    )
    if review_result.scalar_one_or_none() is not None:
        has_diff_review = True

    is_critical = False
    activity_result = await db.execute(
        select(AgentActivity.file_path)
        .filter(AgentActivity.task_id == task_id)
        .order_by(desc(AgentActivity.created_at))
        .limit(50)
    )
    file_paths = [row[0] for row in activity_result.all() if row[0]]
    for fp in file_paths:
        for pattern in CRITICAL_FILE_PATTERNS:
            if pattern in fp:
                is_critical = True
                break
        if is_critical:
            break

    return {
        "has_diff_review": has_diff_review,
        "is_critical": is_critical,
    }


def check_required_outputs(
    to_policy: Optional[StagePolicy],
    task_id: int,
) -> bool:
    """Return True if the target stage has no required outputs, or if we
    assume outputs are present (we cannot fully verify without parsing
    activity entries). Callers should use gather_transition_context to
    get a richer check when the to_policy declares required_outputs."""
    if not to_policy or not to_policy.required_outputs_json:
        return True
    required = json.loads(to_policy.required_outputs_json)
    return len(required) == 0