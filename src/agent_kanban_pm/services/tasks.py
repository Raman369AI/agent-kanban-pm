"""Shared task authorization, mutations, audit records and durable events.

Adapters own response formatting and the final commit. The service adds state,
audit and outbox records to that same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agent_kanban_pm.models import (Entity, EntityType, Stage, Task, TaskStatus,
                                    Project, Role, ApprovalStatus, TaskLog, task_assignments)
from agent_kanban_pm.events import event_bus, EventType
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


async def authorize_task_mutation(db, actor, project_id, task=None, *, assignment=False):
    if not actor or not actor.is_active or actor.role == Role.VIEWER:
        raise PermissionError("An active worker or manager is required")
    project = await db.get(Project, project_id)
    if not project:
        raise TaskReferenceError("Project not found")
    if actor.role in (Role.OWNER, Role.MANAGER):
        return
    if project.approval_status != ApprovalStatus.APPROVED:
        raise PermissionError("Project must be approved before mutation")
    if task is not None and not assignment and task.created_by != actor.id:
        assigned = await db.scalar(select(task_assignments.c.entity_id).where(
            task_assignments.c.task_id == task.id, task_assignments.c.entity_id == actor.id))
        if assigned is None:
            raise PermissionError("You can only modify tasks you created or are assigned to")


async def assign_task_record(db, task, actor, entity, *, role=None, unassign=False, override_reason=None):
    await authorize_task_mutation(db, actor, task.project_id, task, assignment=True)
    if actor.role not in (Role.OWNER, Role.MANAGER) and actor.id != entity.id:
        raise PermissionError("You can only assign or unassign yourself")
    if not unassign and not entity.is_active:
        raise TaskReferenceError("Cannot assign an inactive entity")
    await db.refresh(task, ["assignees"])
    present = entity in task.assignees
    if unassign and present:
        task.assignees.remove(entity)
    elif not unassign and not present:
        task.assignees.append(entity)
    elif not role:
        return False
    task.version += 1
    action = "Unassigned" if unassign else "Assigned"
    db.add(TaskLog(task_id=task.id, log_type="action", message=f"{action} {entity.name} by {actor.name}"))
    event_data = {"task_id": task.id, "entity_id": entity.id, "role": role, "stage_id": task.stage_id}
    if override_reason:
        event_data["override_reason"] = str(override_reason).strip()
    event_bus.enqueue(db, EventType.TASK_UNASSIGNED.value if unassign else EventType.TASK_ASSIGNED.value,
                      event_data, project_id=task.project_id, entity_id=actor.id)
    return True


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
    actor = await db.get(Entity, created_by)
    await authorize_task_mutation(db, actor, project_id)
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
    await db.flush()
    db.add(TaskLog(task_id=task.id, log_type="action", message=f"Task created by {actor.name}"))
    event_bus.enqueue(db, EventType.TASK_CREATED.value, {"task_id": task.id, "title": task.title},
                      project_id=project_id, entity_id=created_by)
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
    override_reason: Optional[str] = None,
) -> TaskMutationResult:
    """Validate and apply an update in the caller's transaction.

    allow_human_policy_warning is retained for Python caller compatibility;
    human decisions use the same actor-based rule through every adapter.
    """
    await authorize_task_mutation(db, actor, task.project_id, task)
    await validate_task_references(
        db,
        project_id=task.project_id,
        stage_id=stage_id,
    )
    from agent_kanban_pm.runtime.task_transitions import resolve_transition_target, check_predecessor
    try:
        stage_id, status = await resolve_transition_target(db, task, stage_id, status)
    except ValueError as exc:
        raise TaskTransitionError(str(exc)) from exc
    if status == TaskStatus.IN_PROGRESS and task.status != TaskStatus.IN_PROGRESS:
        predecessor = await check_predecessor(task, db)
        if predecessor:
            raise TaskTransitionError(predecessor)
    warning = await validate_task_transition(
        db,
        task,
        actor,
        new_stage_id=stage_id,
        new_status=status,
        allow_human_policy_warning=allow_human_policy_warning,
    )
    # A human's explicit move is a policy decision through every interface.
    # Reference, authorization, consistency, and predecessor errors remain hard.
    human_override = actor.entity_type == EntityType.HUMAN
    reason = (override_reason or "").strip()
    if warning and not human_override:
        raise TaskTransitionError(warning)
    if warning and len(reason) < 3:
        raise TaskTransitionError(
            f"{warning}. A human override reason is required to continue"
        )
    if warning:
        db.add(TaskLog(
            task_id=task.id,
            log_type="handoff",
            message=(
                f"Human transition override by {actor.name}: {warning}. "
                f"Reason: {reason}"
            ),
        ))

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
    db.add(TaskLog(task_id=task.id, log_type="action", message=f"Task updated by {actor.name} (version {task.version})"))
    data = {"task_id": task.id, "title": task.title, "status": task.status, "stage_id": task.stage_id}
    event_bus.enqueue(db, EventType.TASK_UPDATED.value, data, project_id=task.project_id, entity_id=actor.id)
    if transition.completed_now:
        event_bus.enqueue(db, EventType.TASK_COMPLETED.value, data,
                          project_id=task.project_id, entity_id=actor.id)
    if transition.old_stage_id != task.stage_id:
        event_bus.enqueue(db, EventType.TASK_MOVED.value,
                          dict(data, from_stage_id=transition.old_stage_id, to_stage_id=task.stage_id),
                          project_id=task.project_id, entity_id=actor.id)
        stage = await db.get(Stage, task.stage_id)
        if stage:
            from agent_kanban_pm.runtime.stage_entry import enqueue_stage_entry
            await enqueue_stage_entry(db, task, stage, actor_id=actor.id)
    return TaskMutationResult(transition=transition, warning=warning)
