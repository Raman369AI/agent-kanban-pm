import asyncio
import getpass
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import select
from database import init_db, async_session_maker
from models import Entity, EntityType, Role
from routers import auth, entities, projects, tasks, stages, websockets, ui, agent_activity
from adapters import register_adapters
from event_bus import event_bus, EventType
from kanban_runtime.adapter_loader import init_adapter_registry
from kanban_runtime.assignment_launcher import assignment_launcher
from kanban_runtime.paths import static_dir
from kanban_runtime.session_streamer import session_streamer_loop
from kanban_runtime.instance import get_port, get_api_base, get_tmux_prefix
from kanban_runtime._version import __version__
import logging
import os

logger = logging.getLogger(__name__)


async def _ensure_local_owner():
    """Bootstrap the single local human owner from env / OS user.

    Local-first install has exactly one human — the person running the
    server. Idempotent: no-op if any active OWNER human already exists.
    """
    def _git_config(key: str) -> str:
        try:
            import subprocess
            return subprocess.run(
                ["git", "config", key], capture_output=True, text=True
            ).stdout.strip() or ""
        except Exception:
            return ""

    name = os.getenv("KANBAN_USER_NAME") or _git_config("user.name") or getpass.getuser() or "Local User"
    email = os.getenv("KANBAN_USER_EMAIL") or _git_config("user.email") or None

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
    from datetime import UTC, datetime, timedelta
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
    except Exception as exc:
        logger.warning("Failed to load adapter specs for heartbeat thresholds: %s", exc)

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
                    stale_time = (datetime.now(UTC) - timedelta(seconds=threshold)).replace(tzinfo=None)

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
                            heartbeat.updated_at = datetime.now(UTC)
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
                        heartbeat.updated_at = datetime.now(UTC)
                        logger.info(f"Heartbeat staleness: agent_id={heartbeat.agent_id} name={agent_name} threshold={threshold}s")

                await db.commit()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat sweeper error: {e}")


async def _pending_event_sweeper(ttl_hours: int = 6, interval_seconds: int = 600):
    """Background task: purge stale PendingEvent rows.

    Consumed events (consumed_at IS NOT NULL) are deleted after 1 hour.
    Unconsumed events are deleted after `ttl_hours` (default 6) as a safety net
    for agents that went offline without draining their queue.
    """
    from datetime import UTC, datetime, timedelta
    from models import PendingEvent
    from sqlalchemy import or_, and_

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            now = datetime.now(UTC).replace(tzinfo=None)
            consumed_cutoff = now - timedelta(hours=1)
            unconsumed_cutoff = now - timedelta(hours=ttl_hours)
            async with async_session_maker() as db:
                result = await db.execute(
                    select(PendingEvent).filter(
                        or_(
                            # Consumed events older than 1 hour
                            and_(
                                PendingEvent.consumed_at.isnot(None),
                                PendingEvent.consumed_at < consumed_cutoff,
                            ),
                            # Unconsumed events older than ttl_hours
                            and_(
                                PendingEvent.consumed_at.is_(None),
                                PendingEvent.created_at < unconsumed_cutoff,
                            ),
                        )
                    )
                )
                stale = result.scalars().all()
                if stale:
                    for row in stale:
                        await db.delete(row)
                    await db.commit()
                    logger.info("Purged %d stale PendingEvent rows", len(stale))
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"PendingEvent sweeper error: {e}")


async def _orphaned_session_sweeper(staleness_seconds: int = 300, interval_seconds: int = 120):
    """Background task: mark orphaned AgentSession rows as DONE.

    A session is considered orphaned if:
      - ended_at is NULL (still marked active)
      - last_seen_at is older than staleness_seconds
      - The tmux session no longer exists (if command was set)
    """
    import shutil
    import subprocess
    from datetime import UTC, datetime, timedelta
    from models import AgentSession, AgentSessionStatus

    has_tmux = shutil.which("tmux") is not None

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=staleness_seconds)
            async with async_session_maker() as db:
                result = await db.execute(
                    select(AgentSession).filter(
                        AgentSession.ended_at.is_(None),
                        AgentSession.last_seen_at < cutoff,
                    )
                )
                stale_sessions = result.scalars().all()
                cleaned = 0
                for session in stale_sessions:
                    # If the session had a tmux command, verify the tmux session is gone
                    if session.command and has_tmux:
                        tmux_name = f"{get_tmux_prefix()}-task-{session.task_id}" if session.task_id else None
                        if tmux_name:
                            try:
                                check = subprocess.run(
                                    ["tmux", "has-session", "-t", tmux_name],
                                    capture_output=True, timeout=5,
                                )
                                if check.returncode == 0:
                                    continue  # tmux session still alive, skip
                            except Exception:
                                pass  # tmux check failed, assume gone

                    session.status = AgentSessionStatus.DONE
                    session.ended_at = datetime.now(UTC).replace(tzinfo=None)
                    cleaned += 1

                if cleaned:
                    await db.commit()
                    logger.info("Marked %d orphaned sessions as DONE (stale >%ds)", cleaned, staleness_seconds)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Orphaned session sweeper error: {e}")


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
    await init_adapter_registry()
    # Start event bus background worker
    event_bus.start()
    # Register event adapters (WebSocket, Webhook broadcast)
    register_adapters()
    assignment_launcher.api_base = os.getenv("KANBAN_API_BASE", get_api_base())
    event_bus.subscribe(EventType.TASK_ASSIGNED.value, assignment_launcher.handle_event)
    startup_workspace = os.getenv("KANBAN_ACTIVE_WORKSPACE", os.getcwd())
    await assignment_launcher.resume_runnable_assignments(workspace_path=startup_workspace)
    # Start heartbeat staleness sweeper
    sweeper_task = asyncio.create_task(_heartbeat_sweeper())
    # Stream tmux pane output of per-task agent sessions into AgentActivity
    streamer_task = asyncio.create_task(session_streamer_loop())
    # Purge stale PendingEvent rows for offline MCP agents
    event_sweeper_task = asyncio.create_task(_pending_event_sweeper())
    # Mark orphaned sessions as DONE when tmux session is gone
    session_sweeper_task = asyncio.create_task(_orphaned_session_sweeper())
    yield
    # Shutdown
    for t in (sweeper_task, streamer_task, event_sweeper_task, session_sweeper_task):
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
    version=__version__,
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(static_dir())), name="static")

# CORS middleware
_port = get_port()
_allowed_origins = [
    f"http://localhost:{_port}",
    f"http://127.0.0.1:{_port}",
]
if _port != 8000:
    _allowed_origins.extend(["http://localhost:8000", "http://127.0.0.1:8000"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Entity-ID"],
)

# Include routers
app.include_router(ui.router)
app.include_router(auth.router)
app.include_router(entities.router)
app.include_router(projects.router)
app.include_router(tasks.router)
app.include_router(stages.router)
app.include_router(websockets.router)
app.include_router(agent_activity.router)


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn
    _default_port = get_port()
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("KANBAN_PORT", _default_port)))
