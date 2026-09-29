from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from datetime import UTC, datetime
import logging

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Entity, Stage,
    OrchestrationDecision, StagePolicy, DecisionType,
)
from agent_kanban_pm.schemas import (
    StagePolicyUpdate, StagePolicyResponse,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/projects/{project_id}/stage-policies", response_model=List[StagePolicyResponse])
async def list_stage_policies(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    result = await db.execute(select(StagePolicy).filter(StagePolicy.project_id == project_id))
    policies = result.scalars().all()
    return [StagePolicyResponse.from_model(p) for p in policies]


@router.put("/projects/{project_id}/stage-policies/{stage_id}", response_model=StagePolicyResponse)
async def update_stage_policy(
    project_id: int,
    stage_id: int,
    update: StagePolicyUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    if not current_entity or not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can update stage policies")

    result = await db.execute(
        select(StagePolicy).filter(StagePolicy.project_id == project_id, StagePolicy.stage_id == stage_id)
    )
    policy = result.scalar_one_or_none()
    if not policy:
        stage_result = await db.execute(select(Stage).filter(Stage.id == stage_id, Stage.project_id == project_id))
        stage = stage_result.scalar_one_or_none()
        if not stage:
            raise HTTPException(status_code=404, detail="Stage not found in this project")
        policy = StagePolicy(
            project_id=project_id,
            stage_id=stage_id,
            stage_key=update.stage_key or "",
            on_enter_roles_json="[]",
            required_outputs_json="[]",
        )
        db.add(policy)
        await db.flush()

    import json as _json
    if update.stage_key is not None:
        policy.stage_key = update.stage_key
    if update.on_enter_roles is not None:
        policy.on_enter_roles_json = _json.dumps(update.on_enter_roles)
    if update.required_outputs is not None:
        policy.required_outputs_json = _json.dumps(update.required_outputs)
    if update.review_mode is not None:
        policy.review_mode = update.review_mode
    if update.allow_parallel is not None:
        policy.allow_parallel = update.allow_parallel
    if update.requires_orchestrator_move is not None:
        policy.requires_orchestrator_move = update.requires_orchestrator_move
    policy.updated_at = datetime.now(UTC)
    await db.flush()
    event_bus.enqueue(db,
        EventType.STAGE_POLICY_UPDATED.value,
        {"project_id": project_id, "stage_id": stage_id, "policy_id": policy.id},
        project_id=project_id,
        entity_id=current_entity.id if current_entity else None,
    )

    db.add(OrchestrationDecision(
        project_id=project_id,
        manager_agent_id=current_entity.id if current_entity else None,
        decision_type=DecisionType.STAGE_POLICY,
        input_summary=f"Updated stage policy for stage_id={stage_id}",
        rationale=_json.dumps(update.model_dump(exclude_unset=True)),
        affected_task_ids=None,
        affected_agent_ids=None,
    ))
    await db.commit()
    await db.refresh(policy)

    return StagePolicyResponse.from_model(policy)


@router.post("/projects/{project_id}/stage-policies/defaults", response_model=List[StagePolicyResponse])
async def seed_default_policies(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    if not current_entity or not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can seed stage policies")

    from agent_kanban_pm.runtime.stage_policy import seed_default_policies as _seed
    policies = await _seed(db, project_id)
    await db.commit()
    return [StagePolicyResponse.from_model(p) for p in policies]
