from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database import get_db
from models import Project, Stage
from schemas import StageCreate, StageUpdate, StageResponse

router = APIRouter(tags=["stages"])

@router.post("/projects/{project_id}/stages", response_model=StageResponse, status_code=status.HTTP_201_CREATED)
async def create_stage(
    project_id: int,
    stage: StageCreate,
    db: AsyncSession = Depends(get_db)
):
    """Add a new stage to a project"""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    db_stage = Stage(project_id=project_id, **stage.model_dump())
    db.add(db_stage)
    await db.commit()
    await db.refresh(db_stage)
    return db_stage


@router.patch("/stages/{stage_id}", response_model=StageResponse)
async def update_stage(
    stage_id: int,
    stage_update: StageUpdate,
    db: AsyncSession = Depends(get_db)
):
    """Update stage details"""
    result = await db.execute(select(Stage).filter(Stage.id == stage_id))
    stage = result.scalar_one_or_none()
    
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")
    
    update_data = stage_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(stage, field, value)
    
    await db.commit()
    await db.refresh(stage)
    return stage


@router.delete("/stages/{stage_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stage(
    stage_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Delete a stage"""
    result = await db.execute(select(Stage).filter(Stage.id == stage_id))
    stage = result.scalar_one_or_none()
    
    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")
    
    await db.delete(stage)
    await db.commit()
