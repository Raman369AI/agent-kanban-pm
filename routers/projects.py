from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import UTC, datetime
import logging

from database import get_db
from models import Project, Stage, Task, ApprovalStatus, Entity, ProjectWorkspace
from schemas import (
    ProjectCreate, ProjectUpdate, ProjectResponse, ProjectDetailResponse
)
from auth import get_current_entity, require_manager, require_owner, is_owner_or_manager, require_project_approval_for_mutation
from event_bus import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])

def _creator_id(entity: Optional[Entity]) -> int:
    """Safely get entity id for creator tracking, or default to 1."""
    return entity.id if entity else 1

@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    project: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create a new project. Starts as PENDING until approved by a manager/owner."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    db_project = Project(
        name=project.name,
        description=project.description,
        path=project.path,
        creator_id=current_entity.id,
        approval_status=ApprovalStatus.PENDING
    )
    db.add(db_project)
    await db.commit()
    await db.refresh(db_project)

    if db_project.path:
        db.add(ProjectWorkspace(
            project_id=db_project.id,
            root_path=db_project.path,
            label="Primary workspace",
            is_primary=True,
        ))

    # Create default stages
    default_stages = [
        {"name": "Backlog", "description": "Tasks to be done", "order": 1},
        {"name": "To Do", "description": "Ready to start", "order": 2},
        {"name": "In Progress", "description": "Currently being worked on", "order": 3},
        {"name": "Review", "description": "Awaiting review", "order": 4},
        {"name": "Done", "description": "Completed tasks", "order": 5}
    ]

    for stage_data in default_stages:
        stage = Stage(project_id=db_project.id, **stage_data)
        db.add(stage)

    await db.commit()

    from kanban_runtime.stage_policy import seed_default_policies as _seed_policies
    try:
        await _seed_policies(db, db_project.id)
    except Exception:
        logger.warning("Could not seed default stage policies for project %s", db_project.id)

    await db.refresh(db_project)

    logger.info(f"Project created: {db_project.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_CREATED.value,
        {"project_id": db_project.id, "name": db_project.name},
        project_id=db_project.id,
        entity_id=current_entity.id
    )

    return db_project


@router.get("", response_model=List[ProjectResponse])
async def list_projects(
    approval_status: Optional[ApprovalStatus] = None,
    db: AsyncSession = Depends(get_db)
):
    """List all projects, optionally filtered by approval status"""
    query = select(Project)
    if approval_status:
        query = query.filter(Project.approval_status == approval_status)

    result = await db.execute(query.order_by(Project.created_at.desc()))
    projects = result.scalars().all()
    return projects


@router.get("/{project_id}", response_model=ProjectDetailResponse)
async def get_project(
    project_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed project information including stages and tasks"""
    result = await db.execute(
        select(Project)
        .filter(Project.id == project_id)
        .options(
            selectinload(Project.stages).selectinload(Stage.tasks).selectinload(Task.assignees),
            selectinload(Project.tasks).selectinload(Task.assignees)
        )
    )
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    return project


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: int,
    project_update: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Update project details. Only MANAGER+ can change approval_status."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    update_data = project_update.model_dump(exclude_unset=True)

    # Approval status changes require MANAGER or OWNER
    if "approval_status" in update_data and not is_owner_or_manager(current_entity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only managers and owners can change project approval status"
        )

    # Non-managers can only update projects they created
    if not is_owner_or_manager(current_entity) and project.creator_id != current_entity.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only update projects you created"
        )

    for field, value in update_data.items():
        setattr(project, field, value)

    project.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(project)

    logger.info(f"Project updated: {project.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_UPDATED.value,
        {"project_id": project.id, "name": project.name},
        project_id=project.id,
        entity_id=current_entity.id
    )

    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager)
):
    """Delete a project. Only MANAGER+ can delete projects."""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_id_to_delete = project.id
    await db.delete(project)
    await db.commit()

    logger.info(f"Project deleted: {project.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_DELETED.value,
        {"project_id": project_id_to_delete},
        project_id=project_id_to_delete,
        entity_id=current_entity.id
    )


@router.post("/{project_id}/approve", response_model=ProjectResponse)
async def approve_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager)
):
    """Approve a pending project."""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.approval_status = ApprovalStatus.APPROVED
    project.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(project)

    logger.info(f"Project approved: {project.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_APPROVED.value,
        {"project_id": project.id, "name": project.name, "status": "APPROVED"},
        project_id=project.id,
        entity_id=current_entity.id
    )

    return project


@router.post("/{project_id}/reject", response_model=ProjectResponse)
async def reject_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager)
):
    """Reject a pending project."""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project.approval_status = ApprovalStatus.REJECTED
    project.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(project)

    logger.info(f"Project rejected: {project.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_APPROVED.value,
        {"project_id": project.id, "name": project.name, "status": "REJECTED"},
        project_id=project.id,
        entity_id=current_entity.id
    )

    return project
