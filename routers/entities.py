from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List, Optional
from database import get_db
from models import Entity, EntityType
from schemas import EntityCreate, EntityResponse
from auth import get_password_hash, generate_api_key

router = APIRouter(prefix="/entities", tags=["entities"])

@router.post("/register/human", response_model=EntityResponse, status_code=status.HTTP_201_CREATED)
async def register_human(entity: EntityCreate, db: AsyncSession = Depends(get_db)):
    """Register a new human user"""
    if entity.entity_type != EntityType.HUMAN:
        raise HTTPException(status_code=400, detail="Entity type must be 'human'")
    
    if not entity.email or not entity.password:
        raise HTTPException(status_code=400, detail="Email and password required for humans")
    
    # Check if email already exists
    result = await db.execute(select(Entity).filter(Entity.email == entity.email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    db_entity = Entity(
        name=entity.name,
        entity_type=EntityType.HUMAN,
        email=entity.email,
        hashed_password=get_password_hash(entity.password),
        skills=entity.skills
    )
    db.add(db_entity)
    await db.commit()
    await db.refresh(db_entity)
    return db_entity


@router.post("/register/agent", response_model=dict, status_code=status.HTTP_201_CREATED)
async def register_agent(entity: EntityCreate, db: AsyncSession = Depends(get_db)):
    """Register a new agent and return API key"""
    if entity.entity_type != EntityType.AGENT:
        raise HTTPException(status_code=400, detail="Entity type must be 'agent'")
    
    api_key = generate_api_key()
    
    db_entity = Entity(
        name=entity.name,
        entity_type=EntityType.AGENT,
        email=entity.email,
        api_key=api_key,
        skills=entity.skills
    )
    db.add(db_entity)
    await db.commit()
    await db.refresh(db_entity)
    
    return {
        "id": db_entity.id,
        "name": db_entity.name,
        "entity_type": db_entity.entity_type,
        "api_key": api_key,
        "message": "Agent registered successfully. Save the API key securely."
    }


@router.get("/me", response_model=List[EntityResponse])
async def get_current_entity_info(db: AsyncSession = Depends(get_db)):
    """Get all entities (auth removed - local mode)"""
    result = await db.execute(select(Entity).filter(Entity.is_active == True))
    return result.scalars().all()


@router.get("", response_model=List[EntityResponse])
async def list_entities(
    entity_type: Optional[EntityType] = None,
    db: AsyncSession = Depends(get_db)
):
    """List all entities, optionally filtered by type"""
    query = select(Entity).filter(Entity.is_active == True)
    if entity_type:
        query = query.filter(Entity.entity_type == entity_type)
    
    result = await db.execute(query)
    entities = result.scalars().all()
    return entities
