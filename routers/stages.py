from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
import logging

from database import get_db
from models import Project, Stage, Entity
from schemas import StageCreate, StageUpdate, StageResponse
from auth import get_current_entity, require_manager, is_owner_or_manager, require_project_approval_for_mutation
from event_bus import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter(tags=["stages"])

def _actor_id(entity: Optional[Entity]) -> int:
    return entity.id if entity else 1

@router.post("/projects/{project_id}/stages", response_model=StageResponse, status_code=status.HTTP_201_CREATED)
async def create_stage(
    project_id: int,
    stage: StageCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager)
):
    """Add a new stage to a project. Only MANAGER+ can manage stages."""
    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    db_stage = Stage(project_id=project_id, **stage.model_dump())
    db.add(db_stage)
    await db.commit()
    await db.refresh(db_stage)

    logger.info(f"Stage created: {db_stage.name} in project {project_id} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_UPDATED.value,
        {"project_id": project_id, "action": "stage_created", "stage_name": db_stage.name},
        project_id=project_id,
        entity_id=current_entity.id
    )

    return db_stage


@router.patch("/stages/{stage_id}", response_model=StageResponse)
async def update_stage(
    stage_id: int,
    stage_update: StageUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager)
):
    """Update stage details. Only MANAGER+ can manage stages."""
    result = await db.execute(select(Stage).filter(Stage.id == stage_id))
    stage = result.scalar_one_or_none()

    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")

    update_data = stage_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(stage, field, value)

    await db.commit()
    await db.refresh(stage)

    logger.info(f"Stage updated: {stage.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_UPDATED.value,
        {"project_id": stage.project_id, "action": "stage_updated", "stage_id": stage.id},
        project_id=stage.project_id,
        entity_id=current_entity.id
    )

    return stage


@router.delete("/stages/{stage_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_stage(
    stage_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager)
):
    """Delete a stage. Only MANAGER+ can manage stages."""
    result = await db.execute(select(Stage).filter(Stage.id == stage_id))
    stage = result.scalar_one_or_none()

    if not stage:
        raise HTTPException(status_code=404, detail="Stage not found")

    project_id = stage.project_id
    await db.delete(stage)
    await db.commit()

    logger.info(f"Stage deleted: {stage.name} by {current_entity.name}")

    await event_bus.publish(
        EventType.PROJECT_UPDATED.value,
        {"project_id": project_id, "action": "stage_deleted", "stage_id": stage_id},
        project_id=project_id,
        entity_id=current_entity.id
    )
