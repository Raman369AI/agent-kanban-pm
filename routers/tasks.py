from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime
from database import get_db
from models import Task, Project, Comment, Entity, TaskStatus, TaskLog
from schemas import (
    TaskCreate, TaskUpdate, TaskResponse, TaskDetailResponse,
    CommentCreate, CommentResponse, TaskLogResponse
)

router = APIRouter(tags=["tasks"])

@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    task: TaskCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new task or subtask"""
    # Verify project exists
    result = await db.execute(select(Project).filter(Project.id == task.project_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Project not found")
    
    db_task = Task(**task.model_dump())
    db.add(db_task)
    await db.commit()
    await db.refresh(db_task, ["assignees"])
    return db_task


@router.get("/tasks", response_model=List[TaskResponse])
async def list_tasks(
    project_id: Optional[int] = None,
    stage_id: Optional[int] = None,
    status: Optional[TaskStatus] = None,
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
    
    result = await db.execute(query.order_by(Task.priority.desc(), Task.created_at.desc()))
    tasks = result.scalars().all()
    
    return tasks


@router.get("/tasks/available", response_model=List[TaskResponse])
async def get_available_tasks(
    db: AsyncSession = Depends(get_db)
):
    """Get all open tasks (local mode - no auth filtering)"""
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
    """Get detailed task information including subtasks and comments"""
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
    db: AsyncSession = Depends(get_db)
):
    """Update task details"""
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    update_data = task_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)
    
    # Mark as completed if status changed to completed
    if task_update.status == TaskStatus.COMPLETED and task.completed_at is None:
        task.completed_at = datetime.utcnow()
    
    task.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a task"""
    result = await db.execute(select(Task).filter(Task.id == task_id))
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    await db.delete(task)
    await db.commit()


# ============================================================================
# TASK ASSIGNMENT
# ============================================================================

@router.post("/tasks/{task_id}/assign", response_model=TaskResponse)
async def assign_task(
    task_id: int,
    entity_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Assign a task to an entity (human or agent)"""
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()
    
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    if entity not in task.assignees:
        task.assignees.append(entity)
        await db.commit()
        await db.refresh(task)
    
    return task


@router.post("/tasks/{task_id}/self-assign", response_model=TaskResponse)
async def self_assign_task(
    task_id: int,
    entity_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Assign an entity to a task (local mode)"""
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    if entity not in task.assignees:
        task.assignees.append(entity)
        await db.commit()
        await db.refresh(task)
    
    return task


@router.delete("/tasks/{task_id}/unassign/{entity_id}", response_model=TaskResponse)
async def unassign_task(
    task_id: int,
    entity_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Unassign an entity from a task"""
    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    
    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()
    
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    
    if entity in task.assignees:
        task.assignees.remove(entity)
        await db.commit()
        await db.refresh(task)
    
    return task


# ============================================================================
# COMMENTS
# ============================================================================

@router.post("/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    comment: CommentCreate,
    db: AsyncSession = Depends(get_db)
):
    """Add a comment to a task"""
    result = await db.execute(select(Task).filter(Task.id == comment.task_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Task not found")
    
    db_comment = Comment(
        content=comment.content,
        task_id=comment.task_id,
        author_id=1
    )
    db.add(db_comment)
    await db.commit()
    await db.refresh(db_comment)
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
