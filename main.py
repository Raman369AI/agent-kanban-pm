import asyncio
import getpass
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import select
from database import init_db, async_session_maker
from models import Entity, EntityType, Role
from routers import auth, entities, projects, tasks, stages, websockets, ui, agent_connections, agent_activity
from adapters import register_adapters
from event_bus import event_bus, EventType
from sync_agents import sync_cli_agents
from kanban_runtime.assignment_launcher import assignment_launcher
from kanban_runtime.session_streamer import session_streamer_loop
import a2a
import logging
import os

logger = logging.getLogger(__name__)


async def _ensure_local_owner():
    """Bootstrap the single local human owner from env / OS user.

    Local-first install has exactly one human — the person running the
    server. Idempotent: no-op if any active OWNER human already exists.
    """
    name = os.getenv("KANBAN_USER_NAME") or getpass.getuser() or "Local User"
    email = os.getenv("KANBAN_USER_EMAIL") or None

    async with async_session_maker() as db:
        existing = await db.execute(
            select(Entity).filter(
                Entity.entity_type == EntityType.HUMAN,
                Entity.role == Role.OWNER,
                Entity.is_active == True,
            ).limit(1)
        )
        if existing.scalar_one_or_none():
            return

        owner = Entity(
            name=name,
            entity_type=EntityType.HUMAN,
            email=email,
            role=Role.OWNER,
            is_active=True,
        )
        db.add(owner)
        await db.commit()
        logger.info("Bootstrapped local owner: %s", name)


async def _heartbeat_sweeper():
    """Background task: mark stale heartbeats as idle and emit AGENT_DISCONNECTED.

    Uses per-adapter heartbeat_interval when available (from adapter YAML
    reporting.heartbeat_interval), falling back to 60s global default.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import select
    from models import AgentHeartbeat, AgentSession, AgentSessionStatus, AgentStatusType, Entity
    from kanban_runtime.adapter_loader import load_all_adapters

    # Build per-agent staleness thresholds from adapter specs
    adapter_thresholds = {}
    global_default = 60
    try:
        for spec in load_all_adapters():
            interval = spec.reporting.heartbeat_interval if spec.reporting else 30
            threshold = max(interval * 2, global_default)  # 2x interval, at least 60s
            adapter_thresholds[spec.name] = threshold
    except Exception:
        pass  # Fall back to global default

    while True:
        try:
            await asyncio.sleep(30)  # Check every 30 seconds

            async with async_session_maker() as db:
                idle_result = await db.execute(
                    select(AgentHeartbeat).filter(
                        AgentHeartbeat.status_type == AgentStatusType.IDLE,
                        AgentHeartbeat.task_id.is_not(None),
                    )
                )
                for idle_heartbeat in idle_result.scalars().all():
                    active_result = await db.execute(
                        select(AgentSession).filter(
                            AgentSession.agent_id == idle_heartbeat.agent_id,
                            AgentSession.task_id == idle_heartbeat.task_id,
                            AgentSession.ended_at.is_(None),
                            AgentSession.status.in_([
                                AgentSessionStatus.ACTIVE,
                                AgentSessionStatus.BLOCKED,
                                AgentSessionStatus.STARTING,
                            ]),
                        )
                    )
                    if not active_result.scalars().first():
                        idle_heartbeat.task_id = None

                result = await db.execute(
                    select(AgentHeartbeat).filter(
                        AgentHeartbeat.status_type != AgentStatusType.IDLE
                    ).join(Entity, AgentHeartbeat.agent_id == Entity.id)
                )
                heartbeats = result.scalars().all()

                for heartbeat in heartbeats:
                    entity_result = await db.execute(
                        select(Entity).filter(Entity.id == heartbeat.agent_id)
                    )
                    entity = entity_result.scalar_one_or_none()
                    agent_name = entity.name if entity else "unknown"

                    threshold = adapter_thresholds.get(agent_name, global_default)
                    stale_time = datetime.utcnow() - timedelta(seconds=threshold)

                    if heartbeat.updated_at and heartbeat.updated_at < stale_time:
                        active_session_result = await db.execute(
                            select(AgentSession)
                            .filter(
                                AgentSession.agent_id == heartbeat.agent_id,
                                AgentSession.ended_at.is_(None),
                                AgentSession.status.in_([
                                    AgentSessionStatus.ACTIVE,
                                    AgentSessionStatus.BLOCKED,
                                    AgentSessionStatus.STARTING,
                                ]),
                            )
                            .order_by(AgentSession.last_seen_at.desc())
                        )
                        active_session = active_session_result.scalars().first()
                        if active_session:
                            status_type = (
                                AgentStatusType.WAITING
                                if active_session.status == AgentSessionStatus.BLOCKED
                                else AgentStatusType.WORKING
                            )
                            heartbeat.task_id = active_session.task_id
                            heartbeat.status_type = status_type
                            heartbeat.message = f"Active task session #{active_session.id} for task {active_session.task_id}"
                            heartbeat.updated_at = datetime.utcnow()
                            await event_bus.publish(
                                EventType.AGENT_STATUS_UPDATED.value,
                                {
                                    "agent_id": heartbeat.agent_id,
                                    "session_id": active_session.id,
                                    "project_id": active_session.project_id,
                                    "task_id": active_session.task_id,
                                    "status_type": status_type.value,
                                    "message": heartbeat.message,
                                    "workspace_path": active_session.workspace_path,
                                },
                                project_id=active_session.project_id,
                                entity_id=heartbeat.agent_id,
                            )
                            continue

                        heartbeat.status_type = AgentStatusType.IDLE
                        heartbeat.task_id = None
                        heartbeat.message = "Cleared stale heartbeat"
                        heartbeat.updated_at = datetime.utcnow()
                        logger.info(f"Heartbeat staleness: agent_id={heartbeat.agent_id} name={agent_name} threshold={threshold}s")

                await db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat sweeper error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup and shutdown events"""
    # Startup
    await init_db()
    # Prime SQLAlchemy async greenlet before starting background workers
    async with async_session_maker() as _session:
        pass
    # Bootstrap the single local human owner
    await _ensure_local_owner()
    # Sync adapter registry to DB entities
    await sync_cli_agents()
    # Start event bus background worker
    event_bus.start()
    # Register event adapters (WebSocket, Webhook broadcast)
    register_adapters()
    assignment_launcher.api_base = os.getenv("KANBAN_API_BASE", "http://127.0.0.1:8000")
    event_bus.subscribe(EventType.TASK_ASSIGNED.value, assignment_launcher.handle_event)
    startup_workspace = os.getenv("KANBAN_ACTIVE_WORKSPACE", os.getcwd())
    await assignment_launcher.resume_runnable_assignments(workspace_path=startup_workspace)
    # Start heartbeat staleness sweeper
    sweeper_task = asyncio.create_task(_heartbeat_sweeper())
    # Stream tmux pane output of per-task agent sessions into AgentActivity
    streamer_task = asyncio.create_task(session_streamer_loop())
    yield
    # Shutdown
    for t in (sweeper_task, streamer_task):
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
    event_bus.unsubscribe(EventType.TASK_ASSIGNED.value, assignment_launcher.handle_event)
    event_bus.stop()

app = FastAPI(
    title="Agent Kanban Project Management API",
    description="A platform-agnostic project management system for humans and AI agents",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(ui.router)
app.include_router(auth.router)
app.include_router(entities.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(stages.router)
app.include_router(websockets.router)
app.include_router(agent_connections.router)
app.include_router(agent_activity.router)
app.include_router(a2a.router)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
