from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text
from models import Base
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
        from kanban_runtime.instance import get_database_url
        return get_database_url()
    except Exception:
        return _DEFAULT_DB_URL


DATABASE_URL = _resolve_database_url()
SQLALCHEMY_ECHO = os.getenv("SQLALCHEMY_ECHO", "").lower() in {"1", "true", "yes", "on"}

engine = create_async_engine(DATABASE_URL, echo=SQLALCHEMY_ECHO, future=True)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """Dependency for getting database session"""
    async with async_session_maker() as session:
        try:
            yield session
        finally:
            await session.close()


async def _column_exists(conn, table: str, column: str) -> bool:
    """Check if a column exists in a table (SQLite PRAGMA)."""
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    rows = result.fetchall()
    return any(row[1] == column for row in rows)


async def _migrate_db_schema():
    """Add missing columns for RBAC, audit trail, and agent visibility.

    Uses a `schema_migrations` table to track applied migrations by version
    number so each migration runs exactly once (P2-2).

    MIGRATION RULES:
    - NEVER add CREATE TABLE for a table that has a SQLAlchemy model.
      create_all() handles those. Only data-only migrations go here.
    - ALTER TABLE additions are for columns added after initial release.
      Guard with _column_exists() and _migration_applied().
    - Data migrations (INSERT/UPDATE) are always acceptable.
    - When adding a new model, do NOT add migration DDL — create_all()
      will create the table. Only add a migration version if there is
      data to backfill.
    """
    from models import EntityType, Role
    async with engine.begin() as conn:
        # Bootstrap the migration tracking table
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))

        async def _migration_applied(version: int) -> bool:
            result = await conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :v"),
                {"v": version},
            )
            return result.scalar() is not None

        async def _record_migration(version: int, name: str) -> None:
            await conn.execute(
                text("INSERT INTO schema_migrations (version, name) VALUES (:v, :n)"),
                {"v": version, "n": name},
            )
            logger.info("Applied migration v%d: %s", version, name)
        # --- Migration v1: Entities role column ---
        if not await _migration_applied(1):
            if not await _column_exists(conn, "entities", "role"):
                logger.info("Migrating: adding 'role' column to entities")
                await conn.execute(text("ALTER TABLE entities ADD COLUMN role VARCHAR(10) DEFAULT 'WORKER'"))
            await _record_migration(1, "entities_role_column")

        # --- Migration v2: Tasks created_by and version columns ---
        if not await _migration_applied(2):
            if not await _column_exists(conn, "tasks", "created_by"):
                await conn.execute(text("ALTER TABLE tasks ADD COLUMN created_by INTEGER"))
            if not await _column_exists(conn, "tasks", "version"):
                await conn.execute(text("ALTER TABLE tasks ADD COLUMN version INTEGER DEFAULT 0"))
            await _record_migration(2, "tasks_created_by_version")

        # --- Migration v3: Agent activities structured fields ---
        if not await _migration_applied(3):
            activity_columns = {
                "session_id": "INTEGER",
                "project_id": "INTEGER",
                "source": "VARCHAR(100)",
                "payload_json": "TEXT",
                "workspace_path": "TEXT",
                "file_path": "TEXT",
                "command": "TEXT",
            }
            for column, ddl_type in activity_columns.items():
                if not await _column_exists(conn, "agent_activities", column):
                    await conn.execute(text(f"ALTER TABLE agent_activities ADD COLUMN {column} {ddl_type}"))
            await _record_migration(3, "agent_activities_structured_fields")

        # Fix any existing lowercase role values before ORM loads them
        await conn.execute(text("UPDATE entities SET role = UPPER(role) WHERE role IS NOT NULL"))
        # Set NULL roles to WORKER as a safe default (backfill will correct human vs agent)
        await conn.execute(text("UPDATE entities SET role = 'WORKER' WHERE role IS NULL"))

        # --- Migration v4: Backfill workspaces ---
        # Tables agent_checkpoints and stage_policies are created by
        # create_all() via their SQLAlchemy models (AgentCheckpoint,
        # StagePolicy). No CREATE TABLE DDL is needed here.
        if not await _migration_applied(4):
            # Backfill primary workspace rows from the legacy projects.path field.
            await conn.execute(text("""
                INSERT INTO project_workspaces (project_id, root_path, label, is_primary, created_at)
                SELECT p.id, p.path, 'Primary workspace', 1, CURRENT_TIMESTAMP
                FROM projects p
                WHERE p.path IS NOT NULL
                  AND p.path != ''
                  AND NOT EXISTS (
                      SELECT 1 FROM project_workspaces w WHERE w.project_id = p.id AND w.root_path = p.path
                  )
            """))
            await _record_migration(4, "checkpoints_stage_policies")

        # --- Migration v5: Optimistic concurrency on approvals ---
        if not await _migration_applied(5):
            if not await _column_exists(conn, "agent_approvals", "update_version"):
                await conn.execute(text("ALTER TABLE agent_approvals ADD COLUMN update_version INTEGER NOT NULL DEFAULT 0"))
            await _record_migration(5, "approval_update_version")

        # --- Migration v6: Soft-delete on pending events ---
        if not await _migration_applied(6):
            if not await _column_exists(conn, "pending_events", "consumed_at"):
                await conn.execute(text("ALTER TABLE pending_events ADD COLUMN consumed_at DATETIME DEFAULT NULL"))
            await _record_migration(6, "pending_events_consumed_at")

        # --- Migration v7: Task sequence_order for ordered subtask enforcement ---
        if not await _migration_applied(7):
            if not await _column_exists(conn, "tasks", "sequence_order"):
                await conn.execute(text("ALTER TABLE tasks ADD COLUMN sequence_order INTEGER"))
            await _record_migration(7, "tasks_sequence_order")

    # Backfill default roles
    async with async_session_maker() as session:
        from models import Entity
        result = await session.execute(select(Entity))
        entities = result.scalars().all()
        for entity in entities:
            try:
                _ = Role(entity.role.value)  # validate
            except (ValueError, AttributeError):
                if entity.entity_type == EntityType.HUMAN:
                    entity.role = Role.OWNER
                else:
                    entity.role = Role.WORKER
                logger.info(f"Backfilled role for entity {entity.name}: {entity.role.value}")
        await session.commit()


async def init_db():
    """Initialize database tables and migrate schema"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Run custom migrations for existing databases
    await _migrate_db_schema()

    # Backfill: ensure every agent has at least one AgentConnection
    await backfill_agent_connections()


async def backfill_agent_connections():
    """Create MCP AgentConnection for any agent that doesn't have one yet."""
    from models import Entity, EntityType, AgentConnection, ProtocolType, ConnectionStatus
    from event_bus import EventType

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
