from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload, joinedload
from typing import List, Optional
from datetime import datetime
from database import get_db
from models import Project, Stage, Task, ApprovalStatus
from schemas import (
    ProjectCreate, ProjectUpdate, ProjectResponse, ProjectDetailResponse
)

router = APIRouter(prefix="/projects", tags=["projects"])

@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    project: ProjectCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create a new project (requires approval)"""
    db_project = Project(
        name=project.name,
        description=project.description,
        creator_id=1,
        approval_status=ApprovalStatus.PENDING
    )
    db.add(db_project)
    await db.commit()
    await db.refresh(db_project)
    
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
    await db.refresh(db_project)
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
            selectinload(Project.stages).selectinload(Stage.tasks).joinedload(Task.assignees),
            selectinload(Project.tasks).joinedload(Task.assignees)
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
    db: AsyncSession = Depends(get_db)
):
    """Update project details or approval status"""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Update fields
    update_data = project_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(project, field, value)
    
    project.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a project"""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    await db.delete(project)
    await db.commit()
