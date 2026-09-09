"""Shared task mutation services used by REST, UI, and MCP adapters.

Adapters retain transport concerns (authentication, response formatting, events,
and commits). This module owns data-integrity and lifecycle mutation rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_kanban_pm.models import Entity, EntityType, Stage, Task, TaskStatus
from agent_kanban_pm.runtime.task_transitions import (
    TaskTransitionState,
    apply_task_transition_fields,
    validate_task_transition,
)


class TaskReferenceError(ValueError):
    """A task points at a stage or parent outside its project."""


class TaskTransitionError(ValueError):
    """A requested lifecycle transition is not currently allowed."""


@dataclass(frozen=True)
class TaskMutationResult:
    transition: TaskTransitionState
    warning: Optional[str] = None


async def validate_task_references(
    db: AsyncSession,
    *,
    project_id: int,
    stage_id: Optional[int] = None,
    parent_task_id: Optional[int] = None,
) -> None:
    """Ensure optional stage and parent references belong to the task project."""
    if stage_id is not None:
        stage_result = await db.execute(
            select(Stage.id).filter(
                Stage.id == stage_id,
                Stage.project_id == project_id,
            )
        )
        if stage_result.scalar_one_or_none() is None:
            raise TaskReferenceError(
                f"Stage {stage_id} does not exist in project {project_id}"
            )

    if parent_task_id is not None:
        parent_result = await db.execute(
            select(Task.id).filter(
                Task.id == parent_task_id,
                Task.project_id == project_id,
            )
        )
        if parent_result.scalar_one_or_none() is None:
            raise TaskReferenceError(
                f"Parent task {parent_task_id} does not exist in "
                f"project {project_id}"
            )


async def create_task_record(
    db: AsyncSession,
    *,
    project_id: int,
    title: str,
    created_by: int,
    description: Optional[str] = None,
    stage_id: Optional[int] = None,
    parent_task_id: Optional[int] = None,
    required_skills: Optional[str] = None,
    priority: int = 0,
    sequence_order: Optional[int] = None,
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
    """Build and add a valid task without committing the adapter transaction."""
    await validate_task_references(
        db,
        project_id=project_id,
        stage_id=stage_id,
        parent_task_id=parent_task_id,
    )
    task = Task(
        title=title,
        description=description,
        status=status,
        project_id=project_id,
        stage_id=stage_id,
        parent_task_id=parent_task_id,
        required_skills=required_skills,
        priority=priority,
        sequence_order=sequence_order,
        created_by=created_by,
    )
    db.add(task)
    return task


async def update_task_record(
    db: AsyncSession,
    task: Task,
    actor: Entity,
    *,
    changes: Mapping[str, Any],
    stage_id: Optional[int] = None,
    status: TaskStatus | str | None = None,
    allow_human_policy_warning: bool = False,
) -> TaskMutationResult:
    """Validate and apply an update without committing or publishing events."""
    await validate_task_references(
        db,
        project_id=task.project_id,
        stage_id=stage_id,
    )
    warning = await validate_task_transition(
        db,
        task,
        actor,
        new_stage_id=stage_id,
        new_status=status,
        allow_human_policy_warning=allow_human_policy_warning,
    )
    human_override = (
        allow_human_policy_warning
        and actor.entity_type == EntityType.HUMAN
    )
    if warning and not human_override:
        raise TaskTransitionError(warning)

    for field in (
        "title",
        "description",
        "priority",
        "required_skills",
        "sequence_order",
    ):
        if field in changes:
            setattr(task, field, changes[field])

    transition = await apply_task_transition_fields(
        db,
        task,
        stage_id=stage_id,
        status=status,
    )
    task.version += 1
    return TaskMutationResult(transition=transition, warning=warning)
