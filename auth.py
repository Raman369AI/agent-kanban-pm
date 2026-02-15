from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import bcrypt
from fastapi import Depends, HTTPException, status, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import os
from dotenv import load_dotenv
import secrets

from database import get_db
from models import Entity, EntityType
from schemas import TokenData

load_dotenv()

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "30"))


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash"""
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8")
    )


def get_password_hash(password: str) -> str:
    """Hash a password"""
    return bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")


def generate_api_key() -> str:
    """Generate a secure API key for agents"""
    return secrets.token_urlsafe(32)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create a JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


async def authenticate_entity(db: AsyncSession, email: str, password: str) -> Optional[Entity]:
    """Authenticate a human user by email and password"""
    result = await db.execute(select(Entity).filter(Entity.email == email))
    entity = result.scalar_one_or_none()
    
    if not entity or not entity.hashed_password:
        return None
    if not verify_password(password, entity.hashed_password):
        return None
    return entity


async def authenticate_agent(db: AsyncSession, api_key: str) -> Optional[Entity]:
    """Authenticate an agent by API key"""
    result = await db.execute(select(Entity).filter(Entity.api_key == api_key))
    entity = result.scalar_one_or_none()
    
    if not entity or entity.entity_type != EntityType.AGENT:
        return None
    return entity


async def get_current_entity(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
) -> Entity:
    """
    Get current authenticated entity (human or agent)
    Supports both JWT tokens (Bearer) and API keys (X-API-Key header)
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # Try API key first (for agents)
    if x_api_key:
        entity = await authenticate_agent(db, x_api_key)
        if entity and entity.is_active:
            return entity
        raise credentials_exception
    
    # Try JWT token (for humans)
    if authorization and authorization.startswith("Bearer "):
        token = authorization.replace("Bearer ", "")
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            entity_id: int = payload.get("sub")
            if entity_id is None:
                raise credentials_exception
        except JWTError:
            raise credentials_exception
        
        result = await db.execute(select(Entity).filter(Entity.id == entity_id))
        entity = result.scalar_one_or_none()
        
        if entity is None or not entity.is_active:
            raise credentials_exception
        return entity
    
    raise credentials_exception


async def get_current_active_entity(
    current_entity: Entity = Depends(get_current_entity)
) -> Entity:
    """Verify entity is active"""
    if not current_entity.is_active:
        raise HTTPException(status_code=400, detail="Inactive entity")
    return current_entity
