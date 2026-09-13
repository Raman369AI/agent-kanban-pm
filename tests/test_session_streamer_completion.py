import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_kanban_pm.db import async_session_maker, init_db
from agent_kanban_pm.runtime.handoff_protocol import update_status_file
from agent_kanban_pm.runtime.preferences import RoleAssignment
from agent_kanban_pm.runtime.session_streamer import (
    _completion_for_session,
    _stream_one_session,
    _finalize_completed_session,
)
from agent_kanban_pm.models import (
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


@pytest.mark.asyncio
async def test_worker_completion_moves_task_to_review_and_assigns_review_roles(tmp_path, monkeypatch):
    await init_db()

    async def _publish_noop(*args, **kwargs):
        return None

    monkeypatch.setattr("agent_kanban_pm.runtime.session_streamer.event_bus.publish", _publish_noop)
    monkeypatch.setattr(
        "agent_kanban_pm.runtime.preferences.load_preferences",
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
    assert await _finalize_completed_session(session, pane="", summary="ready") is False

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

    monkeypatch.setattr("agent_kanban_pm.runtime.session_streamer.event_bus.publish", _publish_noop)
    monkeypatch.setattr(
        "agent_kanban_pm.runtime.preferences.load_preferences",
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
            assigned_role="diff_review",
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


@pytest.mark.asyncio
async def test_completion_import_requires_exact_run_and_survives_runner_exit(tmp_path, monkeypatch):
    await init_db()
    async def _publish_noop(*args, **kwargs):
        return None
    monkeypatch.setattr("agent_kanban_pm.runtime.session_streamer.event_bus.publish", _publish_noop)
    monkeypatch.setattr("agent_kanban_pm.runtime.session_streamer._tmux_has_session", lambda _: False)
    workspace = tmp_path / "scoped"
    workspace.mkdir()
    async with async_session_maker() as db:
        owner = Entity(name="scoped-handoff-owner", entity_type=EntityType.HUMAN, role=Role.OWNER, is_active=True)
        agent = Entity(name="scoped-handoff-agent", entity_type=EntityType.AGENT, role=Role.WORKER, is_active=True)
        db.add_all([owner, agent])
        await db.flush()
        project = Project(name="scoped handoff project", creator_id=owner.id, approval_status=ApprovalStatus.APPROVED, path=str(workspace))
        db.add(project)
        await db.flush()
        stage = Stage(project_id=project.id, name="In Progress", order=3)
        review = Stage(project_id=project.id, name="Review", order=4)
        db.add_all([stage, review])
        await db.flush()
        review_stage_id = review.id
        task = Task(title="Scoped task", project_id=project.id, stage_id=stage.id, status=TaskStatus.IN_PROGRESS, created_by=owner.id)
        db.add(task)
        await db.flush()
        session = AgentSession(agent_id=agent.id, project_id=project.id, task_id=task.id, workspace_path=str(workspace), assigned_role="worker", run_token="active-run", status=AgentSessionStatus.ACTIVE, command="worker")
        db.add(session)
        await db.commit()
        session_id, task_id, project_id = session.id, task.id, project.id

    from agent_kanban_pm.runtime.handoff_protocol import initialize_status_file
    initialize_status_file(workspace, task_id=task_id, project_id=project_id, session_id=session_id, run_token="old-run", current_agent="agent", assigned_role="worker")
    update_status_file(workspace, {"state": "done", "handoff_ready": True, "summary": "Old run"})
    async with async_session_maker() as db:
        session = await db.get(AgentSession, session_id)
    assert await _completion_for_session(session) is None

    initialize_status_file(workspace, task_id=task_id, project_id=project_id, session_id=session_id, run_token="active-run", current_agent="agent", assigned_role="worker")
    update_status_file(workspace, {"state": "done", "handoff_ready": True, "summary": "Current run"})
    assert await _completion_for_session(session) == "Current run"
    (workspace / "STATUS.md").unlink()
    assert await _completion_for_session(session) == "Current run"
    await _stream_one_session(session, "scoped-handoff-agent")
    async with async_session_maker() as db:
        saved_session = await db.get(AgentSession, session_id)
        saved_task = await db.get(Task, task_id)
        assert saved_session.ended_at is not None
        assert saved_task.stage_id == review_stage_id


@pytest.mark.asyncio
async def test_handoff_submission_rejects_wrong_run_and_is_idempotent(tmp_path):
    from agent_kanban_pm.routers.agent_activity import SessionHandoffSubmit, submit_agent_session_handoff

    await init_db()
    async with async_session_maker() as db:
        owner = Entity(name="api-handoff-owner", entity_type=EntityType.HUMAN, role=Role.OWNER, is_active=True)
        agent = Entity(name="api-handoff-agent", entity_type=EntityType.AGENT, role=Role.WORKER, is_active=True)
        db.add_all([owner, agent])
        await db.flush()
        project = Project(name="api handoff project", creator_id=owner.id, approval_status=ApprovalStatus.APPROVED, path=str(tmp_path))
        db.add(project)
        await db.flush()
        progress = Stage(project_id=project.id, name="In Progress", order=3)
        db.add(progress)
        await db.flush()
        task = Task(title="API handoff task", project_id=project.id, stage_id=progress.id, status=TaskStatus.IN_PROGRESS, created_by=owner.id)
        db.add(task)
        await db.flush()
        session = AgentSession(agent_id=agent.id, project_id=project.id, task_id=task.id, workspace_path=str(tmp_path), assigned_role="worker", run_token="current-run", status=AgentSessionStatus.ACTIVE, command="worker")
        db.add(session)
        await db.commit()
        session_id, project_id, task_id, agent_id = session.id, project.id, task.id, agent.id

    async with async_session_maker() as db:
        agent = await db.get(Entity, agent_id)
        wrong = SessionHandoffSubmit(project_id=project_id, task_id=task_id, run_token="stale-run", state="done", summary="Ready")
        with pytest.raises(HTTPException) as exc:
            await submit_agent_session_handoff(session_id, wrong, db=db, current_entity=agent)
        assert exc.value.status_code == 409
        good = SessionHandoffSubmit(project_id=project_id, task_id=task_id, run_token="current-run", state="done", summary="Ready")
        assert (await submit_agent_session_handoff(session_id, good, db=db, current_entity=agent))["accepted"]
        assert (await submit_agent_session_handoff(session_id, good, db=db, current_entity=agent))["accepted"]
        with pytest.raises(HTTPException) as exc:
            await submit_agent_session_handoff(
                session_id, good.model_copy(update={"summary": "Different"}),
                db=db, current_entity=agent,
            )
        assert exc.value.status_code == 409

    async with async_session_maker() as db:
        session = await db.get(AgentSession, session_id)
        task = await db.get(Task, task_id)
        assert session.handoff_summary == "Ready"
        assert session.handoff_received_at is not None
        assert task.status == TaskStatus.IN_PROGRESS
