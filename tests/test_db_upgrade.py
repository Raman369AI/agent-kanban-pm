"""Schema upgrade regression tests.

`init_db()` runs `create_all()` and then a hand-rolled, version-tracked
migration chain (see `db._migrate_db_schema`). `create_all()` only ever
*creates* missing tables — it never alters an existing one — so every column
added after a release depends entirely on that chain running correctly against
a database that already holds a user's data.

Nothing else in the suite exercises that path: every other test starts from an
empty file, where `create_all()` builds the current schema in one shot and the
migrations are no-ops. These tests start from a database shaped like an older
release and assert the upgrade both completes and preserves the rows.

The old shape is produced by building the current schema and dropping the
columns the migrations add, rather than by hand-writing historical DDL — the
point is to test the migration chain, not to maintain a second copy of the
schema that would drift.

Each upgrade runs in a subprocess because `db.engine` binds to DATABASE_URL at
import time; a fresh interpreter per upgrade is also exactly how this code runs
in production.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


# Columns added by migrations v1-v8, keyed by table. Dropping these from a
# freshly built schema reproduces the pre-migration shape.
MIGRATED_COLUMNS = {
    "entities": ["role"],
    "tasks": ["created_by", "version", "sequence_order"],
    "agent_activities": [
        "session_id", "project_id", "source", "payload_json",
        "workspace_path", "file_path", "command",
    ],
    "agent_approvals": ["update_version"],
    "pending_events": ["consumed_at"],
    "projects": ["is_demo"],
}

SRC_DIR = Path(__file__).resolve().parent.parent / "src"


def _run_init_db(db_path: Path) -> subprocess.CompletedProcess:
    """Run init_db() against *db_path* in a clean interpreter."""
    script = textwrap.dedent(
        """
        import asyncio
        from agent_kanban_pm.db import init_db, engine

        async def main():
            await init_db()
            await engine.dispose()

        asyncio.run(main())
        print("INIT_OK")
        """
    )
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    env["PYTHONPATH"] = str(SRC_DIR)
    env["KANBAN_TESTING"] = "1"
    # init_db() also syncs adapters; keep that off the developer's real home.
    env["HOME"] = str(db_path.parent)
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=180, env=env,
    )


def _recreate_without(conn: sqlite3.Connection, table: str, drop: set[str]) -> None:
    """Rebuild *table* without the columns in *drop*.

    SQLite refuses `DROP COLUMN` for a column named in a foreign key
    (tasks.created_by is one), so those tables are rebuilt the long way.
    Constraints and indexes are not reproduced: this only has to stand in for
    an older column layout, and the migrations under test add columns rather
    than inspect constraints.
    """
    info = list(conn.execute(f"PRAGMA table_info({table})"))
    keep = [row for row in info if row[1] not in drop]
    defs = []
    for _cid, name, ctype, notnull, default, pk in keep:
        piece = f'"{name}" {ctype or "TEXT"}'
        if pk:
            piece += " PRIMARY KEY"
        if notnull:
            piece += " NOT NULL"
        if default is not None:
            piece += f" DEFAULT {default}"
        defs.append(piece)
    names = ", ".join(f'"{row[1]}"' for row in keep)

    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(f'CREATE TABLE "{table}__legacy" ({", ".join(defs)})')
    conn.execute(f'INSERT INTO "{table}__legacy" ({names}) SELECT {names} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{table}__legacy" RENAME TO "{table}"')


def _build_legacy_db(db_path: Path) -> None:
    """Create a database shaped like a pre-migration release, with data."""
    # Build the current schema first...
    result = _run_init_db(db_path)
    assert "INIT_OK" in result.stdout, f"baseline build failed:\n{result.stderr}"

    # ...then strip it back to the old shape.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS schema_migrations")
        for table, columns in MIGRATED_COLUMNS.items():
            existing = {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if not existing:
                continue  # table not in this build; nothing to strip
            targets = {c for c in columns if c in existing}
            stubborn = set()
            for column in targets:
                try:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                except sqlite3.OperationalError:
                    stubborn.add(column)
            if stubborn:
                _recreate_without(conn, table, stubborn)
        conn.commit()
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _rows(db_path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    """A pre-migration database holding one project, task, and entity."""
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(db_path)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, is_active) "
            "VALUES (1, 'Local Human', 'HUMAN', 1)"
        )
        conn.execute(
            "INSERT INTO entities (id, name, entity_type, is_active) "
            "VALUES (2, 'claude', 'AGENT', 1)"
        )
        conn.execute(
            "INSERT INTO projects (id, name, description, path) "
            "VALUES (1, 'Legacy Project', 'Made by an older release', '/srv/legacy')"
        )
        conn.execute(
            "INSERT INTO tasks (id, title, description, project_id, status) "
            "VALUES (1, 'Legacy Task', 'Must survive the upgrade', 1, 'PENDING')"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_legacy_fixture_really_is_missing_the_columns(legacy_db):
    """Guard the guard: if the strip stopped working these tests prove nothing."""
    assert "role" not in _columns(legacy_db, "entities")
    assert "sequence_order" not in _columns(legacy_db, "tasks")
    assert "is_demo" not in _columns(legacy_db, "projects")


def test_upgrade_adds_every_migrated_column(legacy_db):
    result = _run_init_db(legacy_db)
    assert "INIT_OK" in result.stdout, f"upgrade failed:\n{result.stderr}"

    for table, columns in MIGRATED_COLUMNS.items():
        present = _columns(legacy_db, table)
        if not present:
            continue
        for column in columns:
            assert column in present, f"{table}.{column} missing after upgrade"


def test_upgrade_preserves_existing_rows(legacy_db):
    result = _run_init_db(legacy_db)
    assert "INIT_OK" in result.stdout, f"upgrade failed:\n{result.stderr}"

    projects = _rows(legacy_db, "SELECT id, name, path FROM projects WHERE id = 1")
    assert projects == [(1, "Legacy Project", "/srv/legacy")]

    tasks = _rows(legacy_db, "SELECT id, title, project_id FROM tasks WHERE id = 1")
    assert tasks == [(1, "Legacy Task", 1)]

    names = {row[0] for row in _rows(legacy_db, "SELECT name FROM entities")}
    assert {"Local Human", "claude"} <= names


def test_upgrade_backfills_roles_not_nulls(legacy_db):
    """Migration v1 adds entities.role; the backfill must leave no NULLs."""
    result = _run_init_db(legacy_db)
    assert "INIT_OK" in result.stdout, f"upgrade failed:\n{result.stderr}"

    roles = _rows(legacy_db, "SELECT name, role FROM entities")
    assert roles, "entities table emptied by upgrade"
    for name, role in roles:
        assert role, f"entity {name!r} left with an empty role"
        assert role == role.upper(), f"entity {name!r} role not normalised: {role!r}"


def test_upgrade_backfills_primary_workspace_from_project_path(legacy_db):
    """Migration v4 turns the legacy projects.path into a workspace row."""
    result = _run_init_db(legacy_db)
    assert "INIT_OK" in result.stdout, f"upgrade failed:\n{result.stderr}"

    workspaces = _rows(
        legacy_db,
        "SELECT root_path, is_primary FROM project_workspaces WHERE project_id = 1",
    )
    assert ("/srv/legacy", 1) in workspaces


def test_upgrade_records_the_migration_chain(legacy_db):
    result = _run_init_db(legacy_db)
    assert "INIT_OK" in result.stdout, f"upgrade failed:\n{result.stderr}"

    versions = {row[0] for row in _rows(legacy_db, "SELECT version FROM schema_migrations")}
    assert versions >= set(range(1, 9)), f"migration chain incomplete: {sorted(versions)}"


def test_upgrade_is_idempotent(legacy_db):
    """Re-running must not fail, duplicate migrations, or duplicate backfills."""
    first = _run_init_db(legacy_db)
    assert "INIT_OK" in first.stdout, f"first upgrade failed:\n{first.stderr}"
    after_first = _rows(legacy_db, "SELECT version, name FROM schema_migrations ORDER BY version")

    second = _run_init_db(legacy_db)
    assert "INIT_OK" in second.stdout, f"second upgrade failed:\n{second.stderr}"
    after_second = _rows(legacy_db, "SELECT version, name FROM schema_migrations ORDER BY version")

    assert after_first == after_second, "migrations re-applied on second run"

    workspaces = _rows(
        legacy_db,
        "SELECT root_path FROM project_workspaces WHERE project_id = 1",
    )
    assert len(workspaces) == len(set(workspaces)), "v4 backfill duplicated workspace rows"

    tasks = _rows(legacy_db, "SELECT id FROM tasks")
    assert len(tasks) == 1, "task rows multiplied across upgrades"


def test_fresh_database_also_records_the_chain(tmp_path):
    """A new install must land on the same migration version as an upgraded one."""
    fresh = tmp_path / "fresh.db"
    result = _run_init_db(fresh)
    assert "INIT_OK" in result.stdout, f"fresh init failed:\n{result.stderr}"

    versions = {row[0] for row in _rows(fresh, "SELECT version FROM schema_migrations")}
    assert versions >= set(range(1, 9)), (
        "a fresh database skipped migration bookkeeping, so the next release's "
        f"migrations would run against it unpredictably: {sorted(versions)}"
    )
