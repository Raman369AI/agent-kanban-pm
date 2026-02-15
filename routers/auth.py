from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import timedelta
from database import get_db
from auth import (
    authenticate_entity, create_access_token, ACCESS_TOKEN_EXPIRE_MINUTES
)
from schemas import Token

router = APIRouter(tags=["auth"])

@router.post("/auth/token", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    """Login endpoint for humans to get JWT token"""
    entity = await authenticate_entity(db, form_data.username, form_data.password)
    if not entity:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": str(entity.id)}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}
