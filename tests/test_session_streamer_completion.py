import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import async_session_maker, init_db
from kanban_runtime.handoff_protocol import update_status_file
from kanban_runtime.preferences import RoleAssignment
from kanban_runtime.session_streamer import (
    _check_completion,
    _finalize_completed_session,
    _terminal_completion_summary,
)
from models import (
    AgentSession,
    AgentSessionStatus,
    ApprovalStatus,
    Entity,
    EntityType,
    Project,
    Role,
    Stage,
    StagePolicy,
    Task,
    TaskStatus,
    task_assignments,
)


class _FakePrefs:
    def __init__(self, assignments):
        self._assignments = assignments

    def get_role_assignments(self):
        return self._assignments


def test_completion_summary_detects_file_list_handoff_at_shell_prompt():
    pane = """
# Todos
[x] Explore workspace file structure
[x] Compare files against AGENTS.md documented structure
[x] List files unrelated to existing structure

Here are the files **unrelated to the existing structure** as documented in AGENTS.md:

| File | Notes |
|---|---|
| `agent_reactor.py` | AGENTS.md: "deleted" |

kronos@host:~/worktree$
"""

    # Test the legacy terminal heuristic directly
    summary = _terminal_completion_summary(pane)
    assert summary is not None
    assert summary.startswith("Here are the files")
    assert "agent_reactor.py" in summary

    # Test the full two-tier check (falls back to heuristic with no workspace)
    summary2 = _check_completion(pane, workspace_path=None)
    assert summary2 is not None
    assert "agent_reactor.py" in summary2


@pytest.mark.asyncio
async def test_worker_completion_moves_task_to_review_and_assigns_review_roles(tmp_path, monkeypatch):
    await init_db()

    async def _publish_noop(*args, **kwargs):
        return None

    monkeypatch.setattr("kanban_runtime.session_streamer.event_bus.publish", _publish_noop)
    monkeypatch.setattr(
        "kanban_runtime.preferences.load_preferences",
        lambda: _FakePrefs({
            "test": RoleAssignment(agent="handoff-test-agent"),
            "diff_review": RoleAssignment(agent="handoff-review-agent"),
        }),
    )

    workspace = tmp_path / "worker"
    workspace.mkdir()
    update_status_file(workspace, {
        "state": "done",
        "handoff_ready": True,
        "assigned_role": "worker",
        "summary": "Implementation complete and ready for review.",
    })

    async with async_session_maker() as db:
        worker = Entity(name="handoff-worker-agent", entity_type=EntityType.AGENT, role=Role.WORKER, is_active=True)
        test_agent = Entity(name="handoff-test-agent", entity_type=EntityType.AGENT, role=Role.WORKER, is_active=True)
        review_agent = Entity(name="handoff-review-agent", entity_type=EntityType.AGENT, role=Role.WORKER, is_active=True)
        owner = Entity(name="handoff-owner", entity_type=EntityType.HUMAN, role=Role.OWNER, is_active=True)
        db.add_all([worker, test_agent, review_agent, owner])
        await db.flush()

        project = Project(
            name="handoff project worker",
            creator_id=owner.id,
            approval_status=ApprovalStatus.APPROVED,
            path=str(tmp_path),
        )
        db.add(project)
        await db.flush()
        backlog = Stage(project_id=project.id, name="Backlog", order=1)
        todo = Stage(project_id=project.id, name="To Do", order=2)
        progress = Stage(project_id=project.id, name="In Progress", order=3)
        review = Stage(project_id=project.id, name="Review", order=4)
        done = Stage(project_id=project.id, name="Done", order=5)
        db.add_all([backlog, todo, progress, review, done])
        await db.flush()
        db.add(StagePolicy(
            project_id=project.id,
            stage_id=review.id,
            stage_key="review",
            on_enter_roles_json='["test", "diff_review"]',
            required_outputs_json="[]",
        ))
        task = Task(
            title="Implement handoff",
            project_id=project.id,
            stage_id=progress.id,
            status=TaskStatus.IN_PROGRESS,
            created_by=owner.id,
        )
        db.add(task)
        await db.flush()
        await db.execute(task_assignments.insert().values(task_id=task.id, entity_id=worker.id))
        session = AgentSession(
            agent_id=worker.id,
            project_id=project.id,
            task_id=task.id,
            workspace_path=str(workspace),
            status=AgentSessionStatus.ACTIVE,
            command="worker",
        )
        db.add(session)
        await db.commit()
        session_id = session.id
        task_id = task.id
        review_stage_id = review.id
        test_agent_id = test_agent.id
        review_agent_id = review_agent.id

    async with async_session_maker() as db:
        session = (await db.execute(select(AgentSession).filter(AgentSession.id == session_id))).scalar_one()

    assert await _finalize_completed_session(session, pane="", summary="ready") is True

    async with async_session_maker() as db:
        task = (await db.execute(
            select(Task)
            .filter(Task.id == task_id)
            .options(selectinload(Task.assignees))
        )).scalar_one()
        session = (await db.execute(select(AgentSession).filter(AgentSession.id == session_id))).scalar_one()

    assert task.stage_id == review_stage_id
    assert task.status == TaskStatus.IN_REVIEW
    assert session.status == AgentSessionStatus.DONE
    assignee_ids = {entity.id for entity in task.assignees}
    assert test_agent_id in assignee_ids
    assert review_agent_id in assignee_ids


@pytest.mark.asyncio
async def test_review_completion_moves_task_to_done_and_assigns_git_pr(tmp_path, monkeypatch):
    await init_db()

    async def _publish_noop(*args, **kwargs):
        return None

    monkeypatch.setattr("kanban_runtime.session_streamer.event_bus.publish", _publish_noop)
    monkeypatch.setattr(
        "kanban_runtime.preferences.load_preferences",
        lambda: _FakePrefs({"git_pr": RoleAssignment(agent="handoff-git-agent")}),
    )

    workspace = tmp_path / "review"
    workspace.mkdir()
    update_status_file(workspace, {
        "state": "done",
        "handoff_ready": True,
        "assigned_role": "diff_review",
        "summary": "Review complete; ready for git handoff.",
    })

    async with async_session_maker() as db:
        reviewer = Entity(name="handoff-diff-review-agent", entity_type=EntityType.AGENT, role=Role.WORKER, is_active=True)
        git_agent = Entity(name="handoff-git-agent", entity_type=EntityType.AGENT, role=Role.WORKER, is_active=True)
        owner = Entity(name="handoff-review-owner", entity_type=EntityType.HUMAN, role=Role.OWNER, is_active=True)
        db.add_all([reviewer, git_agent, owner])
        await db.flush()
        project = Project(
            name="handoff project review",
            creator_id=owner.id,
            approval_status=ApprovalStatus.APPROVED,
            path=str(tmp_path),
        )
        db.add(project)
        await db.flush()
        review = Stage(project_id=project.id, name="Review", order=4)
        done = Stage(project_id=project.id, name="Done", order=5)
        db.add_all([review, done])
        await db.flush()
        db.add(StagePolicy(
            project_id=project.id,
            stage_id=done.id,
            stage_key="done",
            on_enter_roles_json='["git_pr"]',
            required_outputs_json="[]",
        ))
        task = Task(
            title="Review handoff",
            project_id=project.id,
            stage_id=review.id,
            status=TaskStatus.IN_REVIEW,
            created_by=owner.id,
        )
        db.add(task)
        await db.flush()
        await db.execute(task_assignments.insert().values(task_id=task.id, entity_id=reviewer.id))
        session = AgentSession(
            agent_id=reviewer.id,
            project_id=project.id,
            task_id=task.id,
            workspace_path=str(workspace),
            status=AgentSessionStatus.ACTIVE,
            command="review",
        )
        db.add(session)
        await db.commit()
        session_id = session.id
        task_id = task.id
        done_stage_id = done.id
        git_agent_id = git_agent.id

    async with async_session_maker() as db:
        session = (await db.execute(select(AgentSession).filter(AgentSession.id == session_id))).scalar_one()

    assert await _finalize_completed_session(session, pane="", summary="approved") is True

    async with async_session_maker() as db:
        task = (await db.execute(
            select(Task)
            .filter(Task.id == task_id)
            .options(selectinload(Task.assignees))
        )).scalar_one()

    assert task.stage_id == done_stage_id
    assert task.status == TaskStatus.COMPLETED
    assert task.completed_at is not None
    assert git_agent_id in {entity.id for entity in task.assignees}
