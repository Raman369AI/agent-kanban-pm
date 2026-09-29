from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from datetime import UTC, datetime

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Project, Entity, Stage, ApprovalStatus,
    ProjectWorkspace,
)
from agent_kanban_pm.schemas import ProjectResponse
from agent_kanban_pm.auth import get_current_entity, require_owner, require_manager, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.runtime.default_stages import DEFAULT_STAGES

router = APIRouter(include_in_schema=False)


@router.patch("/ui/projects/{project_id}/edit")
async def ui_edit_project(
    project_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager),
):
    """Edit a project from the UI"""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()

    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Approval status changes require MANAGER+
    if "approval_status" in body and not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers/owners can change approval status")

    # Non-managers can only edit projects they created
    if not is_owner_or_manager(current_entity) and project.creator_id != current_entity.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit projects you created")

    for field in ["name", "description", "path", "approval_status", "is_demo"]:
        if field in body:
            setattr(project, field, body[field])

    project.updated_at = datetime.now(UTC)
    event_bus.enqueue(db,
        EventType.PROJECT_UPDATED.value,
        {"project_id": project.id, "name": project.name},
        project_id=project.id,
        entity_id=current_entity.id
    )
    await db.commit()

    return {"ok": True}


@router.delete("/ui/projects/{project_id}")
async def ui_delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_owner),
):
    """Delete a project from the UI. Only MANAGER+ can delete projects."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers/owners can delete projects")

    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_id_to_delete = project.id
    await db.delete(project)
    event_bus.enqueue(db,
        EventType.PROJECT_DELETED.value,
        {"project_id": project_id_to_delete},
        project_id=project_id_to_delete,
        entity_id=current_entity.id
    )
    await db.commit()

    return {"ok": True}


@router.post("/ui/projects/create", response_model=ProjectResponse)
async def ui_create_project(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create a new project from the UI. Defaults to PENDING; MANAGER+ auto-approves."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()
    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    path = body.get("path", "").strip() or None
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Project name is required")

    # MANAGER+ projects are auto-approved; others start as PENDING
    approval_status = ApprovalStatus.APPROVED if is_owner_or_manager(current_entity) else ApprovalStatus.PENDING

    project = Project(
        name=name,
        description=description,
        path=path,
        creator_id=current_entity.id,
        approval_status=approval_status
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    if project.path:
        db.add(ProjectWorkspace(
            project_id=project.id,
            root_path=project.path,
            label="Primary workspace",
            is_primary=True,
        ))

    for stage_data in DEFAULT_STAGES:
        stage = Stage(
            name=stage_data["name"],
            description=stage_data["description"],
            order=stage_data["order"],
            project_id=project.id
        )
        db.add(stage)
    event_bus.enqueue(db,
        EventType.PROJECT_CREATED.value,
        {"project_id": project.id, "name": project.name},
        project_id=project.id,
        entity_id=current_entity.id
    )
    await db.commit()

    return project
