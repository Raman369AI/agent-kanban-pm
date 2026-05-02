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


def get_local_owner(client):
    """Return the single bootstrapped local human owner for tests.

    The app is local-first and no longer supports creating extra humans via a
    registration endpoint. TestClient lifespan startup creates the owner; tests
    should use that same identity through X-Entity-ID.
    """
    response = client.get("/entities/me")
    if response.status_code != 200:
        raise AssertionError(f"Could not resolve local owner: {response.status_code} {response.text}")
    owner = response.json()
    if owner.get("entity_type") != "human":
        raise AssertionError(f"Expected local human owner, got: {owner}")
    return owner


def local_owner_headers(client) -> tuple[dict, dict[str, str]]:
    owner = get_local_owner(client)
    return owner, {"x-entity-id": str(owner["id"])}


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
    from sqlalchemy import delete, update, select
    from models import (
        Entity, Project, Task, AgentApproval, DiffReview, AgentActivity,
        AgentSession, AgentHeartbeat, AgentConnection, Comment,
        TaskLease, ActivitySummary, OrchestrationDecision, UserContribution,
        ProjectWorkspace, TaskLog, Stage,
    )
    from models import task_assignments

    entity_ids = list(_created_entity_ids)
    project_ids = list(_created_project_ids)

    async with async_session_maker() as db:
        # SQLite defaults to FKs OFF (matches production). We manage delete
        # order explicitly so that "foreign key off" + ordered deletes leaves
        # no dangling references.
        if entity_ids:
            # Dependent rows the test agents own.
            await db.execute(delete(AgentApproval).where(
                (AgentApproval.agent_id.in_(entity_ids)) |
                (AgentApproval.resolved_by_entity_id.in_(entity_ids))
            ))
            await db.execute(delete(DiffReview).where(
                (DiffReview.reviewer_id.in_(entity_ids)) |
                (DiffReview.requester_id.in_(entity_ids))
            ))
            await db.execute(delete(ActivitySummary).where(
                ActivitySummary.agent_id.in_(entity_ids)
            ))
            await db.execute(delete(OrchestrationDecision).where(
                OrchestrationDecision.manager_agent_id.in_(entity_ids)
            ))
            await db.execute(delete(UserContribution).where(
                UserContribution.entity_id.in_(entity_ids)
            ))
            await db.execute(delete(TaskLease).where(
                TaskLease.agent_id.in_(entity_ids)
            ))
            await db.execute(delete(AgentActivity).where(
                AgentActivity.agent_id.in_(entity_ids)
            ))
            await db.execute(delete(AgentSession).where(
                AgentSession.agent_id.in_(entity_ids)
            ))
            await db.execute(delete(AgentHeartbeat).where(
                AgentHeartbeat.agent_id.in_(entity_ids)
            ))
            await db.execute(delete(AgentConnection).where(
                AgentConnection.entity_id.in_(entity_ids)
            ))
            await db.execute(delete(Comment).where(
                Comment.author_id.in_(entity_ids)
            ))
            # task_assignments is an association Table, not a model class
            from models import task_assignments
            await db.execute(delete(task_assignments).where(
                task_assignments.c.entity_id.in_(entity_ids)
            ))
            # Severable refs from non-tracked rows: null instead of delete.
            await db.execute(update(Task).where(
                Task.created_by.in_(entity_ids)
            ).values(created_by=None))
            await db.execute(update(Project).where(
                Project.creator_id.in_(entity_ids)
            ).values(creator_id=None))

        if project_ids:
            # Anything bound directly to the project goes first.
            await db.execute(delete(AgentApproval).where(
                AgentApproval.project_id.in_(project_ids)
            ))
            await db.execute(delete(DiffReview).where(
                DiffReview.project_id.in_(project_ids)
            ))
            await db.execute(delete(ActivitySummary).where(
                ActivitySummary.project_id.in_(project_ids)
            ))
            await db.execute(delete(OrchestrationDecision).where(
                OrchestrationDecision.project_id.in_(project_ids)
            ))
            await db.execute(delete(UserContribution).where(
                UserContribution.entity_id.in_(project_ids)
            ))
            await db.execute(delete(ProjectWorkspace).where(
                ProjectWorkspace.project_id.in_(project_ids)
            ))
            await db.execute(delete(AgentSession).where(
                AgentSession.project_id.in_(project_ids)
            ))
            await db.execute(delete(AgentActivity).where(
                AgentActivity.project_id.in_(project_ids)
            ))
            task_ids = select(Task.id).where(Task.project_id.in_(project_ids))
            await db.execute(delete(TaskLease).where(
                TaskLease.task_id.in_(task_ids)
            ))
            # task_assignments is an association Table, not a model class
            await db.execute(delete(task_assignments).where(
                task_assignments.c.task_id.in_(task_ids)
            ))
            await db.execute(delete(Comment).where(
                Comment.task_id.in_(task_ids)
            ))
            await db.execute(delete(TaskLog).where(
                TaskLog.task_id.in_(task_ids)
            ))
            await db.execute(delete(Task).where(
                Task.project_id.in_(project_ids)
            ))
            await db.execute(delete(Stage).where(
                Stage.project_id.in_(project_ids)
            ))
            await db.execute(delete(Project).where(
                Project.id.in_(project_ids)
            ))

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
