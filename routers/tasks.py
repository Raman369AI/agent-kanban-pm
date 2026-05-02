from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import UTC, datetime
import logging

from database import get_db
from models import Task, Project, Comment, Entity, EntityType, TaskStatus, TaskLog
from schemas import (
    TaskCreate, TaskUpdate, TaskResponse, TaskDetailResponse,
    CommentCreate, CommentResponse, TaskLogResponse
)
from auth import (
    get_current_entity, require_worker, require_manager,
    is_owner_or_manager, require_project_approval_for_mutation, require_task_access
)
from event_bus import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tasks"])

def _actor_id(entity: Optional[Entity]) -> int:
    return entity.id if entity else 1

async def _check_predecessor(task: Task, db: AsyncSession) -> Optional[str]:
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
    return f"Cannot start: predecessor task(s) not yet completed — {titles}"

@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    task: TaskCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create a new task or subtask. Project must be approved (or user must be MANAGER+)."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(select(Project).filter(Project.id == task.project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Enforce approval gate: only MANAGER+ can create tasks in non-approved projects
    await require_project_approval_for_mutation(project, current_entity)

    task_data = task.model_dump()
    db_task = Task(**task_data)
    db_task.created_by = current_entity.id
    db.add(db_task)
    await db.commit()
    await db.refresh(db_task, ["assignees"])

    # Auto-audit log
    log = TaskLog(
        task_id=db_task.id,
        message=f"Task created by {current_entity.name}",
        log_type="action"
    )
    db.add(log)
    await db.commit()

    logger.info(f"Task created: {db_task.title} by {current_entity.name}")

    await event_bus.publish(
        EventType.TASK_CREATED.value,
        {"task_id": db_task.id, "title": db_task.title},
        project_id=db_task.project_id,
        entity_id=current_entity.id
    )

    return db_task


@router.get("/tasks", response_model=List[TaskResponse])
async def list_tasks(
    project_id: Optional[int] = None,
    stage_id: Optional[int] = None,
    status: Optional[TaskStatus] = None,
    entity_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db)
):
    """List tasks with optional filters"""
    query = select(Task).options(selectinload(Task.assignees))

    if project_id:
        query = query.filter(Task.project_id == project_id)
    if stage_id:
        query = query.filter(Task.stage_id == stage_id)
    if status:
        query = query.filter(Task.status == status)
    if entity_id:
        query = query.join(Task.assignees).filter(Entity.id == entity_id)

    result = await db.execute(query.order_by(Task.priority.desc(), Task.created_at.desc()))
    tasks = result.scalars().all()
    return tasks


@router.get("/tasks/available", response_model=List[TaskResponse])
async def get_available_tasks(
    db: AsyncSession = Depends(get_db)
):
    """Get all open tasks available for assignment"""
    query = select(Task).options(selectinload(Task.assignees)).filter(
        Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS])
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/tasks/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed task information"""
    result = await db.execute(
        select(Task)
        .filter(Task.id == task_id)
        .options(
            selectinload(Task.assignees),
            selectinload(Task.subtasks),
            selectinload(Task.comments),
            selectinload(Task.logs)
        )
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    return task


@router.patch("/tasks/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int,
    task_update: TaskUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Update task details. Enforces RBAC and optimistic locking."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Optimistic locking
    if task_update.version is not None and task.version != task_update.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Task was modified by another process. Current version: {task.version}"
        )

    # Enforce task-level access
    await require_task_access(task, current_entity, db, require_write=True)

    # Enforce project approval for non-managers
    project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
    project = project_result.scalar_one_or_none()
    if project:
        await require_project_approval_for_mutation(project, current_entity)

    old_stage_id = task.stage_id
    old_status = task.status

    # Sequence order enforcement: block moving to in_progress until predecessors complete
    if task_update.status == TaskStatus.IN_PROGRESS and task.status != TaskStatus.IN_PROGRESS:
        err = await _check_predecessor(task, db)
        if err:
            raise HTTPException(status_code=409, detail=err)

    # Stage policy transition validation (P0-3)
    if task_update.stage_id is not None and task_update.stage_id != old_stage_id:
        try:
            from kanban_runtime.stage_policy import get_stage_policy_for_stage, validate_transition, gather_transition_context
            from_policy = await get_stage_policy_for_stage(db, task.project_id, old_stage_id)
            to_policy = await get_stage_policy_for_stage(db, task.project_id, task_update.stage_id)
            move_initiator = "human" if current_entity.entity_type == EntityType.HUMAN else current_entity.name
            ctx = await gather_transition_context(db, task_id, task.project_id)
            transition_warning = validate_transition(
                from_policy=from_policy,
                to_policy=to_policy,
                move_initiator=move_initiator,
                has_diff_review=ctx["has_diff_review"],
                has_required_outputs=True,
                is_critical=ctx["is_critical"],
            )
            if transition_warning:
                raise HTTPException(status_code=409, detail=transition_warning)
        except ImportError:
            pass  # stage_policy not available

    update_data = task_update.model_dump(exclude_unset=True)
    # Don't allow version/created_by to be set via update
    update_data.pop("version", None)
    update_data.pop("created_by", None)

    for field, value in update_data.items():
        setattr(task, field, value)

    if task_update.status == TaskStatus.COMPLETED and task.completed_at is None:
        task.completed_at = datetime.now(UTC)

    task.version += 1
    task.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(task)

    # Auto-audit log
    log = TaskLog(
        task_id=task.id,
        message=f"Task updated by {current_entity.name} (version {task.version})",
        log_type="action"
    )
    db.add(log)
    await db.commit()

    logger.info(f"Task updated: {task.title} by {current_entity.name}")

    if task_update.stage_id is not None and task_update.stage_id != old_stage_id:
        await event_bus.publish(
            EventType.TASK_MOVED.value,
            {
                "task_id": task.id,
                "title": task.title,
                "from_stage_id": old_stage_id,
                "to_stage_id": task.stage_id,
                "status": task.status
            },
            project_id=task.project_id,
            entity_id=current_entity.id
        )

    if task_update.status == TaskStatus.COMPLETED and old_status != TaskStatus.COMPLETED:
        await event_bus.publish(
            EventType.TASK_COMPLETED.value,
            {"task_id": task.id, "title": task.title},
            project_id=task.project_id,
            entity_id=current_entity.id
        )

    await event_bus.publish(
        EventType.TASK_UPDATED.value,
        {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "stage_id": task.stage_id
        },
        project_id=task.project_id,
        entity_id=current_entity.id
    )

    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Delete a task. Only creator, assignee, or MANAGER+ can delete."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Enforce access: MANAGER+ can delete any task; WORKER can delete only their own
    await require_task_access(task, current_entity, db, require_write=True)

    task_id_to_delete = task.id
    project_id = task.project_id
    await db.delete(task)
    await db.commit()

    logger.info(f"Task deleted: {task.title} by {current_entity.name}")

    await event_bus.publish(
        EventType.TASK_DELETED.value,
        {"task_id": task_id_to_delete},
        project_id=project_id,
        entity_id=current_entity.id
    )


# ============================================================================
# TASK ASSIGNMENT
# ============================================================================

@router.post("/tasks/{task_id}/assign", response_model=TaskResponse)
async def assign_task(
    task_id: int,
    request: Request,
    entity_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Assign a task to an entity. MANAGER+ can assign anyone; WORKER can only self-assign."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    if entity_id is None:
        body = await request.json()
        entity_id = body.get("entity_id")
    if entity_id is None:
        raise HTTPException(status_code=400, detail="entity_id is required")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Enforce project approval
    project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
    project = project_result.scalar_one_or_none()
    if project:
        await require_project_approval_for_mutation(project, current_entity)

    # RBAC: non-managers can only self-assign
    if not is_owner_or_manager(current_entity) and entity_id != current_entity.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only assign tasks to yourself"
        )

    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()

    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if entity not in task.assignees:
        task.assignees.append(entity)
        task.version += 1
        await db.commit()
        await db.refresh(task)

        # Auto-audit log
        log = TaskLog(
            task_id=task.id,
            message=f"Assigned to {entity.name} by {current_entity.name}",
            log_type="action"
        )
        db.add(log)
        await db.commit()

        logger.info(f"Task assigned: {task.title} -> {entity.name} by {current_entity.name}")

        await event_bus.publish(
            EventType.TASK_ASSIGNED.value,
            {"task_id": task.id, "entity_id": entity_id},
            project_id=task.project_id,
            entity_id=current_entity.id
        )

    return task


@router.post("/tasks/{task_id}/self-assign", response_model=TaskResponse)
async def self_assign_task(
    task_id: int,
    request: Request,
    entity_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Self-assign to a task. Any authenticated user can self-assign to approved projects."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    if entity_id is None:
        body = await request.json()
        entity_id = body.get("entity_id")
    if entity_id is None:
        entity_id = current_entity.id

    # Can only self-assign as yourself (unless MANAGER+)
    if not is_owner_or_manager(current_entity) and entity_id != current_entity.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Can only self-assign as yourself"
        )

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Enforce project approval
    project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
    project = project_result.scalar_one_or_none()
    if project:
        await require_project_approval_for_mutation(project, current_entity)

    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if entity not in task.assignees:
        task.assignees.append(entity)
        task.version += 1
        await db.commit()
        await db.refresh(task)

        # Auto-audit log
        log = TaskLog(
            task_id=task.id,
            message=f"Self-assigned by {entity.name}",
            log_type="action"
        )
        db.add(log)
        await db.commit()

        logger.info(f"Task self-assigned: {task.title} -> {entity.name}")

        await event_bus.publish(
            EventType.TASK_ASSIGNED.value,
            {"task_id": task.id, "entity_id": entity_id, "self_assigned": True},
            project_id=task.project_id,
            entity_id=entity_id
        )

    return task


@router.delete("/tasks/{task_id}/unassign/{entity_id}", response_model=TaskResponse)
async def unassign_task(
    task_id: int,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Unassign an entity from a task. MANAGER+ can unassign anyone; others can only unassign themselves."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # RBAC: non-managers can only unassign themselves
    if not is_owner_or_manager(current_entity) and entity_id != current_entity.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only unassign yourself"
        )

    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()

    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if entity in task.assignees:
        task.assignees.remove(entity)
        task.version += 1
        await db.commit()
        await db.refresh(task)

        # Auto-audit log
        log = TaskLog(
            task_id=task.id,
            message=f"Unassigned {entity.name} by {current_entity.name}",
            log_type="action"
        )
        db.add(log)
        await db.commit()

        logger.info(f"Task unassigned: {task.title} <- {entity.name} by {current_entity.name}")

        await event_bus.publish(
            EventType.TASK_UNASSIGNED.value,
            {"task_id": task.id, "entity_id": entity_id},
            project_id=task.project_id,
            entity_id=current_entity.id
        )

    return task


# ============================================================================
# COMMENTS
# ============================================================================

@router.post("/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    comment: CommentCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Add a comment to a task"""
    result = await db.execute(select(Task).filter(Task.id == comment.task_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Task not found")

    db_comment = Comment(
        content=comment.content,
        task_id=comment.task_id,
        author_id=_actor_id(current_entity)
    )
    db.add(db_comment)
    await db.commit()
    await db.refresh(db_comment)

    task_res = await db.execute(select(Task).filter(Task.id == db_comment.task_id))
    task = task_res.scalar_one_or_none()
    if task:
        await event_bus.publish(
            EventType.TASK_COMMENTED.value,
            {"task_id": task.id, "comment": db_comment.content},
            project_id=task.project_id,
            entity_id=_actor_id(current_entity)
        )

    return db_comment


@router.get("/tasks/{task_id}/comments", response_model=List[CommentResponse])
async def get_task_comments(
    task_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get all comments for a task"""
    result = await db.execute(
        select(Comment)
        .filter(Comment.task_id == task_id)
        .order_by(Comment.created_at.asc())
    )
    comments = result.scalars().all()
    return comments


@router.get("/tasks/{task_id}/logs", response_model=List[TaskLogResponse])
async def get_task_logs(
    task_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get all logs for a task"""
    result = await db.execute(
        select(TaskLog)
        .filter(TaskLog.task_id == task_id)
        .order_by(TaskLog.created_at.desc())
    )
    return result.scalars().all()
