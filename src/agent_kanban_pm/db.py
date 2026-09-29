from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text, event
from sqlalchemy.pool import NullPool
from agent_kanban_pm.models import Base
import os
import json
import logging
from datetime import UTC, datetime
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

_DEFAULT_DB_URL = "sqlite+aiosqlite:///./kanban.db"


def _resolve_database_url() -> str:
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url
    try:
        from agent_kanban_pm.runtime.instance import get_database_url
        return get_database_url()
    except Exception:
        return _DEFAULT_DB_URL


DATABASE_URL = _resolve_database_url()
SQLALCHEMY_ECHO = os.getenv("SQLALCHEMY_ECHO", "").lower() in {"1", "true", "yes", "on"}

_ENGINE_KWARGS = {"echo": SQLALCHEMY_ECHO, "future": True}
if DATABASE_URL.startswith("sqlite+aiosqlite:"):
    _ENGINE_KWARGS["poolclass"] = NullPool

engine = create_async_engine(DATABASE_URL, **_ENGINE_KWARGS)

@event.listens_for(engine.sync_engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()
    except Exception as e:
        logger.debug("Failed to set SQLite PRAGMAs: %s", e)

async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """Dependency for getting database session"""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Create or upgrade the database schema (see ``agent_kanban_pm.db_migrations``)."""
    from agent_kanban_pm import db_migrations
    from agent_kanban_pm.db_migrations.legacy import run_legacy_migrations

    async with engine.begin() as conn:
        if DATABASE_URL.startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=5000"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
        state = await conn.run_sync(db_migrations.detect_state)

    if state == "empty":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(db_migrations.stamp, "head")
    elif state == "legacy":
        # Pre-Alembic database: add any new tables, run the frozen chain, then
        # adopt Alembic at the baseline and apply anything newer.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await run_legacy_migrations(engine, async_session_maker)
        async with engine.begin() as conn:
            await conn.run_sync(db_migrations.stamp, db_migrations.BASELINE_REVISION)
            await conn.run_sync(db_migrations.upgrade)
    else:
        async with engine.begin() as conn:
            await conn.run_sync(db_migrations.upgrade)

    # Backfill: ensure every agent has at least one AgentConnection
    await backfill_agent_connections()


async def backfill_agent_connections():
    """Create MCP AgentConnection for any agent that doesn't have one yet."""
    from agent_kanban_pm.models import Entity, EntityType, AgentConnection, ProtocolType, ConnectionStatus
    from agent_kanban_pm.events import EventType

    async with async_session_maker() as session:
        # Find agents without any connection
        result = await session.execute(
            select(Entity).filter(Entity.entity_type == EntityType.AGENT, Entity.is_active == True)
        )
        agents = result.scalars().all()

        all_events = [et.value for et in EventType]

        for agent in agents:
            conn_result = await session.execute(
                select(AgentConnection)
                .filter(AgentConnection.entity_id == agent.id)
                .order_by(AgentConnection.id.asc())
                .limit(1)
            )
            existing = conn_result.scalars().first()
            if not existing:
                connection = AgentConnection(
                    entity_id=agent.id,
                    protocol=ProtocolType.MCP,
                    config=json.dumps({}),
                    subscribed_events=json.dumps(all_events),
                    subscribed_projects=None,
                    status=ConnectionStatus.OFFLINE,
                    last_seen=datetime.now(UTC)
                )
                session.add(connection)
                logger.info(f"Backfilled AgentConnection for agent '{agent.name}' (id={agent.id})")

        await session.commit()
