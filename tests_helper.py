"""
Test helper that auto-cleans entities and projects created during a test run.

Tests in this repo register fresh Entity and Project rows directly through the
REST API and never delete them. Over time the database accumulates "Heartbeat
Agent #5", "Visibility Owner", "Coordination Project", and similar test debris
which then bloats the Team and Projects views.

How it works:
- An SQLAlchemy `after_insert` listener records every Entity / Project row id
  that gets created after this module is imported.
- On process exit (`atexit`) those tracked rows are deleted with foreign-key
  cascade so heartbeats / sessions / activities / leases / approvals belonging
  to the test agents go with them.
- The same logic is exposed as a pytest fixture (`autouse`, `session`-scoped)
  so `pytest` runs are also covered.

Usage in a test file run as a plain script:

    import tests_helper  # noqa: F401  — auto-cleans throwaway rows on exit

Pytest discovers `conftest.py` automatically; no per-file import is required
for pytest invocations.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
from typing import Set

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Opt-out via env var if a test ever wants to keep its rows.
_DISABLED = os.environ.get("KANBAN_KEEP_TEST_ENTITIES") == "1"

_created_entity_ids: Set[int] = set()
_created_project_ids: Set[int] = set()
_listeners_installed = False


def _install_listeners() -> None:
    global _listeners_installed
    if _listeners_installed or _DISABLED:
        return
    # Imported lazily so this module stays cheap to import.
    from models import Entity, Project

    @event.listens_for(Entity, "after_insert")
    def _track_entity(mapper, connection, target):  # type: ignore[no-untyped-def]
        if target.id is not None:
            _created_entity_ids.add(target.id)

    @event.listens_for(Project, "after_insert")
    def _track_project(mapper, connection, target):  # type: ignore[no-untyped-def]
        if target.id is not None:
            _created_project_ids.add(target.id)

    _listeners_installed = True


async def _delete_tracked_async() -> None:
    if not _created_entity_ids and not _created_project_ids:
        return
    from database import async_session_maker
    from sqlalchemy import delete, text
    from models import Entity, Project

    entity_ids = list(_created_entity_ids)
    project_ids = list(_created_project_ids)

    async with async_session_maker() as db:
        # SQLite defaults to FKs OFF (matches production). We manage delete
        # order explicitly so that "foreign key off" + ordered deletes leaves
        # no dangling references.
        if entity_ids:
            ids_csv = ",".join(str(i) for i in entity_ids)
            # Dependent rows the test agents own.
            for stmt in (
                f"DELETE FROM agent_approvals WHERE agent_id IN ({ids_csv}) OR resolved_by_entity_id IN ({ids_csv})",
                f"DELETE FROM diff_reviews WHERE reviewer_id IN ({ids_csv}) OR requester_id IN ({ids_csv})",
                f"DELETE FROM activity_summaries WHERE agent_id IN ({ids_csv})",
                f"DELETE FROM orchestration_decisions WHERE manager_agent_id IN ({ids_csv})",
                f"DELETE FROM user_contributions WHERE entity_id IN ({ids_csv})",
                f"DELETE FROM task_leases WHERE agent_id IN ({ids_csv})",
                f"DELETE FROM agent_activities WHERE agent_id IN ({ids_csv})",
                f"DELETE FROM agent_sessions WHERE agent_id IN ({ids_csv})",
                f"DELETE FROM agent_heartbeats WHERE agent_id IN ({ids_csv})",
                f"DELETE FROM agent_connections WHERE entity_id IN ({ids_csv})",
                f"DELETE FROM comments WHERE author_id IN ({ids_csv})",
                f"DELETE FROM task_assignments WHERE entity_id IN ({ids_csv})",
                # Severable refs from non-tracked rows: null instead of delete.
                f"UPDATE tasks SET created_by=NULL WHERE created_by IN ({ids_csv})",
                f"UPDATE projects SET creator_id=NULL WHERE creator_id IN ({ids_csv})",
            ):
                await db.execute(text(stmt))

        if project_ids:
            pids_csv = ",".join(str(i) for i in project_ids)
            for stmt in (
                # Anything bound directly to the project goes first.
                f"DELETE FROM agent_approvals WHERE project_id IN ({pids_csv})",
                f"DELETE FROM diff_reviews WHERE project_id IN ({pids_csv})",
                f"DELETE FROM activity_summaries WHERE project_id IN ({pids_csv})",
                f"DELETE FROM orchestration_decisions WHERE project_id IN ({pids_csv})",
                f"DELETE FROM user_contributions WHERE project_id IN ({pids_csv})",
                f"DELETE FROM project_workspaces WHERE project_id IN ({pids_csv})",
                f"DELETE FROM agent_sessions WHERE project_id IN ({pids_csv})",
                f"DELETE FROM agent_activities WHERE project_id IN ({pids_csv})",
                # Tasks → comments / logs / leases / activities cascade via project FK.
                f"DELETE FROM task_leases WHERE task_id IN (SELECT id FROM tasks WHERE project_id IN ({pids_csv}))",
                f"DELETE FROM task_assignments WHERE task_id IN (SELECT id FROM tasks WHERE project_id IN ({pids_csv}))",
                f"DELETE FROM comments WHERE task_id IN (SELECT id FROM tasks WHERE project_id IN ({pids_csv}))",
                f"DELETE FROM task_logs WHERE task_id IN (SELECT id FROM tasks WHERE project_id IN ({pids_csv}))",
                f"DELETE FROM tasks WHERE project_id IN ({pids_csv})",
                f"DELETE FROM stages WHERE project_id IN ({pids_csv})",
                f"DELETE FROM projects WHERE id IN ({pids_csv})",
            ):
                await db.execute(text(stmt))

        if entity_ids:
            await db.execute(delete(Entity).where(Entity.id.in_(entity_ids)))

        await db.commit()


def cleanup_now() -> None:
    """Run the cleanup synchronously. Safe to call from atexit or fixtures."""
    if _DISABLED or (not _created_entity_ids and not _created_project_ids):
        return
    try:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already inside an event loop (e.g. pytest-asyncio session).
                # Spawn a one-shot task and wait for it.
                future = asyncio.run_coroutine_threadsafe(_delete_tracked_async(), loop)
                future.result(timeout=10)
                return
        except RuntimeError:
            pass
        asyncio.run(_delete_tracked_async())
    except Exception as exc:  # pragma: no cover — best-effort cleanup
        logger.warning("tests_helper cleanup failed: %s", exc)
    finally:
        _created_entity_ids.clear()
        _created_project_ids.clear()


# Install everything at import time so `import tests_helper` is the only
# call sites need.
_install_listeners()
atexit.register(cleanup_now)


# -----------------------------------------------------------------------------
# pytest fixture (only used when pytest is the runner)
# -----------------------------------------------------------------------------
try:  # pragma: no cover — optional dependency
    import pytest

    @pytest.fixture(autouse=True, scope="session")
    def _kanban_throwaway_cleanup():
        """Autouse session fixture: delete every entity/project created during the run."""
        _install_listeners()
        yield
        cleanup_now()
except ImportError:
    pass
