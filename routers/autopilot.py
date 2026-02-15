from fastapi import APIRouter, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from database import get_db
from models import Task, Entity, TaskStatus
import asyncio
from typing import Optional
from pydantic import BaseModel

router = APIRouter()

class AutopilotConfig(BaseModel):
    enabled: bool = False
    manager_id: Optional[int] = None

# In-memory config storage for MVP
current_config = AutopilotConfig()

@router.get("/ui/autopilot/config", response_model=AutopilotConfig)
async def get_config():
    return current_config

@router.post("/ui/autopilot/config", response_model=AutopilotConfig)
async def update_config(config: AutopilotConfig):
    global current_config
    current_config = config
    return current_config



from database import engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession
from models import Task, Entity, TaskStatus, EntityType, TaskLog
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from websocket_manager import manager, create_notification

# Create async session factory for background tasks
async_session_factory = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def run_autopilot_loop():
    """Background task that runs periodically to manage the board."""
    while True:
        try:
            if current_config.enabled and current_config.manager_id:
                async with async_session_factory() as session:
                    # 1. Get manager entity
                    res_manager = await session.execute(select(Entity).filter(Entity.id == current_config.manager_id))
                    manager_entity = res_manager.scalar_one_or_none()
                    
                    if manager_entity:
                        # 2. Find unassigned pending tasks
                        res_tasks = await session.execute(
                            select(Task)
                            .filter(Task.status == TaskStatus.PENDING)
                            .options(selectinload(Task.assignees))
                        )
                        tasks = res_tasks.scalars().all()
                        
                        processed_tasks = []
                        for task in tasks:
                            if not task.assignees:
                                # Assign to manager
                                task.assignees.append(manager_entity)
                                
                                # Create log
                                log_msg = f"Autopilot: Manager {manager_entity.name} self-assigned task '{task.title}'"
                                log = TaskLog(task_id=task.id, message=log_msg, log_type="action")
                                session.add(log)
                                
                                processed_tasks.append((task, log_msg))
                        
                        if processed_tasks:
                            await session.commit()
                            print(f"Autopilot: Assigned {len(processed_tasks)} tasks to {manager_entity.name}")
                            
                            # Broadcast updates via WebSocket
                            for task, msg in processed_tasks:
                                # Notify project channel
                                notification = create_notification("task_update", {
                                    "task_id": task.id,
                                    "type": "log",
                                    "message": msg,
                                    "log_type": "action",
                                    "timestamp": datetime.utcnow().isoformat()
                                }, task.project_id)
                                
                                await manager.broadcast_to_project(notification, task.project_id)
            
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Autopilot error: {e}")
            await asyncio.sleep(5)


