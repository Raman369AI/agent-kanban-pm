"""Shared task transition helpers.

Keep route-specific concerns such as RBAC, HTTP errors, comments, and event
publishing in the caller. This module owns the common board-state rules so REST,
UI, and MCP moves cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_kanban_pm.models import Entity, EntityType, Stage, Task, TaskStatus


@dataclass(frozen=True)
class TaskTransitionState:
    old_stage_id: Optional[int]
    old_status: TaskStatus
    completed_now: bool


def coerce_task_status(value: TaskStatus | str | None) -> TaskStatus | None:
    if value is None or isinstance(value, TaskStatus):
        return value
    return TaskStatus(value)


async def check_predecessor(task: Task, db: AsyncSession) -> Optional[str]:
    """Block start if any earlier ordered sibling is not completed."""
    if task.sequence_order is None or task.sequence_order <= 1:
        return None
    result = await db.execute(
        select(Task).filter(
            Task.project_id == task.project_id,
            Task.parent_task_id == task.parent_task_id,
            Task.sequence_order < task.sequence_order,
            Task.status != TaskStatus.COMPLETED,
            Task.id != task.id,
        )
    )
    blockers = result.scalars().all()
    if not blockers:
        return None
    titles = ", ".join(f"#{b.id} '{b.title}'" for b in blockers[:3])
    return f"Cannot start: predecessor task(s) not yet completed - {titles}"


async def validate_task_transition(
    db: AsyncSession,
    task: Task,
    actor: Entity,
    *,
    new_stage_id: Optional[int] = None,
    new_status: TaskStatus | str | None = None,
    allow_human_policy_warning: bool = False,
) -> Optional[str]:
    """Return a transition warning/error string, or None when allowed."""
    status = coerce_task_status(new_status)

    if new_stage_id is not None and new_stage_id != task.stage_id:
        stage_result = await db.execute(
            select(Stage).filter(
                Stage.id == new_stage_id,
                Stage.project_id == task.project_id,
            )
        )
        if stage_result.scalar_one_or_none() is None:
            return (
                f"Stage {new_stage_id} does not exist in project "
                f"{task.project_id}"
            )

    if status == TaskStatus.IN_PROGRESS and task.status != TaskStatus.IN_PROGRESS:
        predecessor_error = await check_predecessor(task, db)
        if predecessor_error:
            return predecessor_error

    if new_stage_id is None or new_stage_id == task.stage_id:
        return None

    from agent_kanban_pm.models import AgentSession, AgentSessionStatus
    from agent_kanban_pm.runtime.review_gate import implementation_for_task, completion_blocker, is_reopening
    current = await db.get(Stage, task.stage_id) if task.stage_id else None
    target = await db.get(Stage, new_stage_id)
    implementation = await implementation_for_task(db, task.id)
    evidence = implementation
    if current and current.key == "review" and implementation:
        evidence = await db.scalar(select(AgentSession).where(
            AgentSession.task_id == task.id,
            AgentSession.source_session_id == implementation.id,
            AgentSession.work_revision == implementation.work_revision,
            AgentSession.status == AgentSessionStatus.DONE,
            AgentSession.assigned_role.in_(["test", "diff_review", "git_pr"]),
            AgentSession.handoff_received_at.is_not(None),
        ).order_by(AgentSession.id.desc()).limit(1))
    if implementation and not is_reopening(current, target):
        import asyncio
        from agent_kanban_pm.runtime.workspaces import git_revision, WorkspacePreparationError
        try:
            revision = await asyncio.to_thread(git_revision, implementation.workspace_path)
        except WorkspacePreparationError as exc:
            return str(exc)
        if revision != implementation.work_revision:
            return "Implementation changed after handoff"
    return await completion_blocker(db, evidence, task, current, target, actor=actor)


async def resolve_transition_target(db, task, stage_id, status):
    """Stage-only and status-only moves have identical policy boundaries."""
    from agent_kanban_pm.runtime.stage_identity import STAGE_STATUSES
    status = coerce_task_status(status)
    stage = await db.get(Stage, stage_id) if stage_id is not None else None
    if stage is not None:
        expected = STAGE_STATUSES.get(stage.key)
        if status is None and expected:
            status = TaskStatus(expected)
        elif expected and status != TaskStatus.BLOCKED and status.value != expected:
            raise ValueError("Task status does not match the destination stage")
    elif stage_id is None and status is not None and status != TaskStatus.BLOCKED:
        current = await db.get(Stage, task.stage_id) if task.stage_id else None
        if not current or STAGE_STATUSES.get(current.key) != status.value:
            keys = [key for key, value in STAGE_STATUSES.items() if value == status.value]
            stage = await db.scalar(select(Stage).where(
                Stage.project_id == task.project_id, Stage.workflow_key.in_(keys),
            ).order_by(Stage.order).limit(1))
            if not stage:
                raise ValueError("No workflow stage is configured for this status")
            stage_id = stage.id
    return stage_id, status


async def apply_task_transition_fields(
    db: AsyncSession,
    task: Task,
    *,
    stage_id: Optional[int] = None,
    status: TaskStatus | str | None = None,
) -> TaskTransitionState:
    """Apply shared board-state fields and return previous state."""
    old_stage_id = task.stage_id
    old_status = task.status
    completed_now = False

    if stage_id is not None:
        stage_result = await db.execute(
            select(Stage).filter(
                Stage.id == stage_id,
                Stage.project_id == task.project_id,
            )
        )
        stage = stage_result.scalar_one_or_none()
        if stage is None:
            raise ValueError(
                f"Stage {stage_id} does not exist in project {task.project_id}"
            )
        task.stage_id = stage_id
        task.stage = stage

    coerced_status = coerce_task_status(status)
    if coerced_status is not None:
        task.status = coerced_status

    if task.status == TaskStatus.COMPLETED and task.completed_at is None:
        task.completed_at = datetime.now(UTC)
        completed_now = old_status != TaskStatus.COMPLETED
    elif coerced_status is not None and task.status != TaskStatus.COMPLETED:
        # Reopened work is no longer complete. Clear the old timestamp so
        # reporting agrees with the visible task state.
        task.completed_at = None

    task.updated_at = datetime.now(UTC)
    return TaskTransitionState(
        old_stage_id=old_stage_id,
        old_status=old_status,
        completed_now=completed_now,
    )
