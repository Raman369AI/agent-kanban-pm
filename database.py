from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text
from models import Base
import os
import json
import logging
from datetime import datetime
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./kanban.db")

engine = create_async_engine(DATABASE_URL, echo=True, future=True)
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
    """Add missing columns for RBAC, audit trail, and agent visibility."""
    from models import EntityType, Role
    async with engine.begin() as conn:
        # Entities table: add role
        if not await _column_exists(conn, "entities", "role"):
            logger.info("Migrating: adding 'role' column to entities")
            await conn.execute(text("ALTER TABLE entities ADD COLUMN role VARCHAR(10) DEFAULT 'WORKER'"))

        # Tasks table: add created_by, version
        if not await _column_exists(conn, "tasks", "created_by"):
            logger.info("Migrating: adding 'created_by' column to tasks")
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN created_by INTEGER"))
        if not await _column_exists(conn, "tasks", "version"):
            logger.info("Migrating: adding 'version' column to tasks")
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN version INTEGER DEFAULT 0"))

        # Agent activities: structured visibility fields.
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
                logger.info("Migrating: adding '%s' column to agent_activities", column)
                await conn.execute(text(f"ALTER TABLE agent_activities ADD COLUMN {column} {ddl_type}"))

        # Fix any existing lowercase role values before ORM loads them
        await conn.execute(text("UPDATE entities SET role = UPPER(role) WHERE role IS NOT NULL"))
        # Set NULL roles to WORKER as a safe default (backfill will correct human vs agent)
        await conn.execute(text("UPDATE entities SET role = 'WORKER' WHERE role IS NULL"))
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
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS agent_checkpoints (
                id INTEGER PRIMARY KEY,
                agent_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                task_id INTEGER NOT NULL,
                session_id INTEGER,
                workspace_path TEXT,
                summary TEXT NOT NULL,
                terminal_tail TEXT,
                payload_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(agent_id) REFERENCES entities(id) ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY(session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL
            )
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_checkpoints_agent_id ON agent_checkpoints(agent_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_checkpoints_project_id ON agent_checkpoints(project_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_checkpoints_task_id ON agent_checkpoints(task_id)"))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_agent_checkpoints_session_id ON agent_checkpoints(session_id)"))

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
                select(AgentConnection).filter(AgentConnection.entity_id == agent.id)
            )
            existing = conn_result.scalar_one_or_none()
            if not existing:
                connection = AgentConnection(
                    entity_id=agent.id,
                    protocol=ProtocolType.MCP,
                    config=json.dumps({}),
                    subscribed_events=json.dumps(all_events),
                    subscribed_projects=None,
                    status=ConnectionStatus.OFFLINE,
                    last_seen=datetime.utcnow()
                )
                session.add(connection)
                logger.info(f"Backfilled AgentConnection for agent '{agent.name}' (id={agent.id})")

        await session.commit()
