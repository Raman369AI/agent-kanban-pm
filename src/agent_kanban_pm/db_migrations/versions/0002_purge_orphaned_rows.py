"""Purge rows orphaned by deletes made before foreign keys were enforced.

Older releases ran without ``PRAGMA foreign_keys=ON``, so deleting a project,
task or entity left its dependants behind. They are invisible today, but
SQLite reuses ids (no AUTOINCREMENT), so a new row can later inherit a stale
dependant, and rebuilding an affected table in a later revision fails on the
first orphan.

This applies what the cascade would have done, using each foreign key's own
``ON DELETE`` rule: ``CASCADE`` rows are deleted, ``SET NULL`` columns are
cleared, anything else is left alone. It repeats until nothing changes because
deleting an orphan can orphan its own dependants. If anything is found the
database file is copied first.

Revision ID: 0002
Revises: 0001
"""
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alembic import op
from sqlalchemy import text

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")
_MAX_PASSES = 20


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _foreign_keys(conn) -> list[dict]:
    """Single-column foreign keys with an action we can apply."""
    tables = [
        row[0]
        for row in conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ))
    ]
    found = []
    for table in tables:
        rows = conn.execute(text(f"PRAGMA foreign_key_list({_quote(table)})")).fetchall()
        composite = {row[0] for row in rows if row[1] > 0}
        for row in rows:
            fk_id, _seq, parent, column, parent_column, _on_update, on_delete = row[:7]
            if fk_id in composite or on_delete.upper() not in {"CASCADE", "SET NULL"}:
                continue
            found.append({
                "table": table, "column": column, "parent": parent,
                "parent_column": parent_column or "id", "action": on_delete.upper(),
            })
    return found


def _orphan_predicate(fk: dict) -> str:
    return (
        f"{_quote(fk['column'])} IS NOT NULL AND {_quote(fk['column'])} NOT IN "
        f"(SELECT {_quote(fk['parent_column'])} FROM {_quote(fk['parent'])})"
    )


def _count_orphans(conn, fks: list[dict]) -> int:
    return sum(
        conn.execute(text(f"SELECT COUNT(*) FROM {_quote(fk['table'])} WHERE {_orphan_predicate(fk)}")).scalar()
        for fk in fks
    )


def _backup(conn) -> None:
    """Copy the database file next to itself; skipped for in-memory databases."""
    database = conn.engine.url.database
    if not database or database == ":memory:" or not Path(database).exists():
        return
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = Path(database).with_name(f"{Path(database).stem}.pre-orphan-purge-{stamp}.db")
    source = sqlite3.connect(database)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    logger.warning("Backed up database to %s before removing orphaned rows", target)


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "sqlite":
        return
    fks = _foreign_keys(conn)
    if not _count_orphans(conn, fks):
        return

    _backup(conn)
    removed: dict[str, int] = {}
    for _ in range(_MAX_PASSES):
        changed = 0
        for fk in fks:
            table, column = _quote(fk["table"]), _quote(fk["column"])
            if fk["action"] == "CASCADE":
                sql = f"DELETE FROM {table} WHERE {_orphan_predicate(fk)}"
            else:
                sql = f"UPDATE {table} SET {column} = NULL WHERE {_orphan_predicate(fk)}"
            count = conn.execute(text(sql)).rowcount
            if count:
                changed += count
                key = f"{fk['table']}.{fk['column']}"
                removed[key] = removed.get(key, 0) + count
        if not changed:
            break
    for key, count in sorted(removed.items()):
        logger.warning("Orphan purge: %d row(s) via %s", count, key)


def downgrade() -> None:
    # Deleted rows cannot be restored; the pre-purge backup is the way back.
    pass
