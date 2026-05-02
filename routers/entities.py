from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from datetime import UTC, datetime
import json
import logging

from database import get_db
from models import Entity, EntityType, AgentConnection, ProtocolType, ConnectionStatus, Role
from schemas import EntityCreate, EntityResponse
from auth import get_current_entity, require_owner, require_worker, is_owner_or_manager
from event_bus import EventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/entities", tags=["entities"])


@router.post("/register/agent", response_model=dict, status_code=status.HTTP_201_CREATED)
async def register_agent(
    entity: EntityCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Register a new agent. Defaults to WORKER role.

    Role is clamped to WORKER unless the caller is MANAGER+.
    Unauthenticated callers are rejected.
    """
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required to register agents")
    if entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=400, detail="Entity type must be 'agent'")

    # Clamp role: non-managers can only register WORKER or VIEWER agents.
    requested_role = entity.role or Role.WORKER
    if requested_role in (Role.OWNER, Role.MANAGER):
        if not current_entity or not is_owner_or_manager(current_entity):
            requested_role = Role.WORKER

    db_entity = Entity(
        name=entity.name,
        entity_type=EntityType.AGENT,
        email=entity.email,
        skills=entity.skills,
        role=requested_role,
        is_active=True
    )
    db.add(db_entity)
    await db.commit()
    await db.refresh(db_entity)

    # Auto-create AgentConnection so the agent receives events immediately
    all_events = [et.value for et in EventType if et != EventType.ALL]
    connection = AgentConnection(
        entity_id=db_entity.id,
        protocol=ProtocolType.MCP,
        config=json.dumps({}),
        subscribed_events=json.dumps(all_events),
        subscribed_projects=None,
        status=ConnectionStatus.OFFLINE,
        last_seen=datetime.now(UTC)
    )
    db.add(connection)
    await db.commit()

    logger.info(f"Agent registered: {db_entity.name} (id={db_entity.id})")

    return {
        "id": db_entity.id,
        "name": db_entity.name,
        "entity_type": db_entity.entity_type,
        "message": "Agent registered successfully."
    }


@router.get("/me", response_model=EntityResponse)
async def get_current_entity_info(
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Get current default entity info"""
    if not current_entity:
        raise HTTPException(status_code=404, detail="No entities found. Register one first.")
    return current_entity


@router.get("", response_model=List[EntityResponse])
async def list_entities(
    entity_type: Optional[EntityType] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_worker),
):
    """List all entities, optionally filtered by type"""
    query = select(Entity).filter(Entity.is_active == True)
    if entity_type:
        query = query.filter(Entity.entity_type == entity_type)
    
    result = await db.execute(query)
    entities = result.scalars().all()
    return entities


@router.patch("/{entity_id}", response_model=EntityResponse)
async def update_entity(
    entity_id: int,
    updates: dict,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_owner)
):
    """Update an entity. Only OWNER can change roles."""
    result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = result.scalar_one_or_none()

    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    allowed = {"name", "skills", "email"}
    # Only owners can change roles
    if "role" in updates:
        if current_entity.role != Role.OWNER:
            raise HTTPException(status_code=403, detail="Only owners can change roles")
        try:
            updates["role"] = Role(updates["role"])
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid role. Choose from: {[r.value for r in Role]}")
        allowed.add("role")

    for key, value in updates.items():
        if key in allowed:
            setattr(entity, key, value)

    await db.commit()
    await db.refresh(entity)
    logger.info(f"Entity updated: {entity.name} (id={entity.id})")
    return entity


@router.delete("/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entity(
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_owner)
):
    """Delete an entity. Only OWNER can delete entities."""
    if entity_id == current_entity.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")

    result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = result.scalar_one_or_none()

    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    await db.delete(entity)
    await db.commit()
    logger.info(f"Entity deleted: {entity.name} (id={entity.id})")
