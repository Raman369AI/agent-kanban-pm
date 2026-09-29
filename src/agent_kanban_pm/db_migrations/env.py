"""Alembic environment.

``db.init_db`` passes a live sync connection through ``config.attributes``.
Running ``alembic`` from the command line (for example to autogenerate a
revision) builds a synchronous engine from the configured database URL.
"""
from alembic import context
from sqlalchemy import create_engine, pool

from agent_kanban_pm.models import Base

config = context.config
target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite cannot ALTER most things in place; batch mode rebuilds tables.
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return

    from agent_kanban_pm.db import DATABASE_URL

    engine = create_engine(DATABASE_URL.replace("+aiosqlite", ""), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline (--sql) migrations are not supported for SQLite batch mode.")
run_migrations_online()
