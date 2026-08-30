import asyncio
import getpass
import hmac
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from sqlalchemy import select
from agent_kanban_pm.db import init_db, async_session_maker
from agent_kanban_pm.models import Entity, EntityType, Role
from agent_kanban_pm.routers import auth, entities, projects, tasks, stages, websockets, ui, agent_activity
from agent_kanban_pm.adapters import register_adapters
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.runtime.adapter_loader import init_adapter_registry
from agent_kanban_pm.runtime.assignment_launcher import assignment_launcher
from agent_kanban_pm.runtime.paths import static_dir
from agent_kanban_pm.runtime.session_streamer import session_streamer_loop
from agent_kanban_pm.runtime.instance import get_port, get_api_base, get_tmux_prefix
from agent_kanban_pm.runtime._version import __version__
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
    from agent_kanban_pm.models import AgentHeartbeat, AgentSession, AgentSessionStatus, AgentStatusType, Entity
    from agent_kanban_pm.runtime.adapter_loader import load_all_adapters

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

                # Single JOIN query to fetch heartbeats with entity names
                result = await db.execute(
                    select(AgentHeartbeat, Entity.name)
                    .filter(AgentHeartbeat.status_type != AgentStatusType.IDLE)
                    .join(Entity, AgentHeartbeat.agent_id == Entity.id)
                )
                rows = result.all()

                for heartbeat, agent_name in rows:
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
    from agent_kanban_pm.models import PendingEvent
    from sqlalchemy import or_, and_, delete

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            now = datetime.now(UTC).replace(tzinfo=None)
            consumed_cutoff = now - timedelta(hours=1)
            unconsumed_cutoff = now - timedelta(hours=ttl_hours)
            async with async_session_maker() as db:
                stmt = delete(PendingEvent).where(
                    or_(
                        and_(
                            PendingEvent.consumed_at.isnot(None),
                            PendingEvent.consumed_at < consumed_cutoff,
                        ),
                        and_(
                            PendingEvent.consumed_at.is_(None),
                            PendingEvent.created_at < unconsumed_cutoff,
                        ),
                    )
                )
                result = await db.execute(stmt)
                if result.rowcount:
                    await db.commit()
                    logger.info("Purged %d stale PendingEvent rows", result.rowcount)
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
    from agent_kanban_pm.models import AgentSession, AgentSessionStatus

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
    await event_bus.stop_async()

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
    allow_headers=["Content-Type", "Authorization", "X-Entity-ID", "X-CSRF-Token"],
)

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _unauthorized(detail: str = "Unauthorized: Invalid or missing Kanban Auth Token") -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": detail})


@app.middleware("http")
async def token_auth_middleware(request: Request, call_next):
    if os.getenv("KANBAN_TESTING") == "1":
        return await call_next(request)

    path = request.url.path
    method = request.method.upper()

    # Exempt public, static, and websocket endpoints. WebSocket routes verify
    # the Kanban token themselves before subscribing (see routers/websockets.py).
    if path == "/health" or path.startswith("/static") or path.startswith("/ws"):
        return await call_next(request)

    from agent_kanban_pm.runtime.instance import get_auth_token, get_csrf_token

    # /ui/api/* are JSON endpoints, not pages: they always require the token.
    # HTML page GETs are exempt so a browser can load the UI and receive the
    # auth cookie; every other request — including /ui/* mutations — requires
    # the token.
    is_ui_api = path.startswith("/ui/api")
    is_html_route = (path == "/" or path.startswith("/ui")) and not is_ui_api
    is_safe = method in _SAFE_METHODS
    token_required = is_ui_api or not (is_html_route and is_safe)

    if token_required:
        expected_token = get_auth_token()

        # Header credentials (X-Kanban-Token / Authorization) cannot be set by
        # a cross-origin webpage without passing CORS preflight, so they do
        # not need CSRF protection.
        header_token = request.headers.get("x-kanban-token")
        auth_header = request.headers.get("authorization")
        if not header_token and auth_header:
            if auth_header.lower().startswith("bearer "):
                header_token = auth_header[7:]
            else:
                header_token = auth_header

        if header_token:
            if header_token != expected_token:
                return _unauthorized()
        else:
            cookie_token = request.cookies.get("kanban-token")
            if not cookie_token or cookie_token != expected_token:
                return _unauthorized()
            # Cookie-only auth is ambient (browsers attach cookies to any
            # same-site request, including forged form posts), so mutations
            # must also present the per-page CSRF token.
            if not is_safe:
                csrf_header = request.headers.get("x-csrf-token", "")
                if not hmac.compare_digest(csrf_header, get_csrf_token()):
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Forbidden: missing or invalid CSRF token"},
                    )

    response = await call_next(request)

    # Set the auth cookie on HTML page loads so same-origin browser calls
    # authenticate automatically. HttpOnly keeps it out of JS reach (the CSRF
    # token travels via a meta tag instead); SameSite=strict blocks cross-site
    # sending entirely.
    if is_html_route and is_safe:
        response.set_cookie(
            key="kanban-token",
            value=get_auth_token(),
            path="/",
            samesite="strict",
            httponly=True,
        )

    return response


# Reject requests with a non-loopback Host header before routing. Browsers
# will otherwise reach the loopback server for any domain whose DNS resolves
# to 127.0.0.1 (DNS rebinding). Added outermost so it runs before auth.
# Extra hosts can be whitelisted via KANBAN_ALLOWED_HOSTS (comma-separated).
if os.getenv("KANBAN_TESTING") != "1":
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from agent_kanban_pm.runtime.instance import ALLOWED_HOSTS

    _extra_hosts = [
        h.strip() for h in os.getenv("KANBAN_ALLOWED_HOSTS", "").split(",") if h.strip()
    ]
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[*ALLOWED_HOSTS, *_extra_hosts],
        www_redirect=False,
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
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("KANBAN_PORT", _default_port)))
