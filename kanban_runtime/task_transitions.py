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

from models import Entity, EntityType, Stage, Task, TaskStatus


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
    if status == TaskStatus.IN_PROGRESS and task.status != TaskStatus.IN_PROGRESS:
        predecessor_error = await check_predecessor(task, db)
        if predecessor_error:
            return predecessor_error

    if new_stage_id is None or new_stage_id == task.stage_id:
        return None

    try:
        from kanban_runtime.stage_policy import (
            gather_transition_context,
            get_stage_policy_for_stage,
            validate_transition,
        )
    except ImportError:
        return None

    from_policy = await get_stage_policy_for_stage(db, task.project_id, task.stage_id)
    to_policy = await get_stage_policy_for_stage(db, task.project_id, new_stage_id)
    move_initiator = "human" if actor.entity_type == EntityType.HUMAN else actor.name
    ctx = await gather_transition_context(db, task.id, task.project_id)
    warning = validate_transition(
        from_policy=from_policy,
        to_policy=to_policy,
        move_initiator=move_initiator,
        has_diff_review=ctx["has_diff_review"],
        has_required_outputs=True,
        is_critical=ctx["is_critical"],
    )
    if warning and not (allow_human_policy_warning and actor.entity_type == EntityType.HUMAN):
        return warning
    return warning


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
        task.stage_id = stage_id
        stage_result = await db.execute(select(Stage).filter(Stage.id == stage_id))
        task.stage = stage_result.scalar_one_or_none()

    coerced_status = coerce_task_status(status)
    if coerced_status is not None:
        task.status = coerced_status

    if task.status == TaskStatus.COMPLETED and task.completed_at is None:
        task.completed_at = datetime.now(UTC)
        completed_now = old_status != TaskStatus.COMPLETED

    task.updated_at = datetime.now(UTC)
    return TaskTransitionState(
        old_stage_id=old_stage_id,
        old_status=old_status,
        completed_now=completed_now,
    )
