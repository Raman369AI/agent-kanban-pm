"""Behavioral checks for ownership, transactions and concurrent mutations."""
from pathlib import Path
import subprocess
import uuid
import pytest
from sqlalchemy import select, text
from sqlalchemy.orm.exc import StaleDataError
from agent_kanban_pm.db import async_session_maker, init_db
from agent_kanban_pm.models import Entity, EntityType, Role, Project, ApprovalStatus, Stage, Task, TaskStatus, OutboxEvent
from agent_kanban_pm.events import EventBus
from agent_kanban_pm.runtime.assignment_launcher import AssignmentLauncher, prune_git_worktree


async def seed_task(stage_name='In Progress'):
    await init_db()
    async with async_session_maker() as db:
        owner = Entity(name=uuid.uuid4().hex, entity_type=EntityType.HUMAN, role=Role.OWNER)
        db.add(owner)
        await db.flush()
        project = Project(name='Reliability', creator_id=owner.id, approval_status=ApprovalStatus.APPROVED)
        db.add(project)
        await db.flush()
        stage = Stage(name=stage_name, project_id=project.id, order=1)
        db.add(stage)
        await db.flush()
        task = Task(title='Work', project_id=project.id, stage_id=stage.id, created_by=owner.id)
        db.add(task)
        await db.commit()
        return task.id, owner.id


@pytest.mark.asyncio
async def test_foreign_keys_on_every_new_connection():
    await init_db()
    for _ in range(3):
        async with async_session_maker() as db:
            assert await db.scalar(text('PRAGMA foreign_keys')) == 1


@pytest.mark.asyncio
async def test_competing_task_writes_have_one_winner():
    task_id, _ = await seed_task()
    async with async_session_maker() as first, async_session_maker() as second:
        a = await first.get(Task, task_id)
        b = await second.get(Task, task_id)
        a.title = 'First'
        a.version += 1
        b.title = 'Second'
        b.version += 1
        await first.commit()
        with pytest.raises(StaleDataError):
            await second.commit()
        await second.rollback()
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).title == 'First'


@pytest.mark.asyncio
async def test_outbox_rolls_back_with_mutation_and_survives_new_bus(monkeypatch):
    task_id, _ = await seed_task()
    event_type = uuid.uuid4().hex
    bus = EventBus()
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        task.title = 'Rolled back'
        bus.enqueue(db, event_type, {'task_id': task_id})
        await db.rollback()
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).title == 'Work'
        assert not any(event_type in row.payload for row in (await db.execute(select(OutboxEvent))).scalars())
        bus.enqueue(db, event_type, {'task_id': task_id})
        await db.commit()
    restarted = EventBus()
    seen = []
    async def collect(payload):
        seen.append(payload['data']['task_id'])
    async def ignore(*args):
        pass
    restarted.subscribe(event_type, collect)
    monkeypatch.setattr(restarted, '_persist_for_agents', ignore)
    while await restarted.dispatch_pending():
        pass
    await restarted.dispatch_pending()
    assert seen == [task_id]


@pytest.mark.asyncio
async def test_starting_reviewer_preserves_review_stage():
    task_id, owner_id = await seed_task('Review')
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        await db.refresh(task, ['stage'])
        task.status = TaskStatus.IN_REVIEW
        before = task.stage_id
        assert await AssignmentLauncher()._mark_task_started(db, task, await db.get(Entity, owner_id)) is None
        assert task.stage_id == before
        assert task.status == TaskStatus.IN_REVIEW


def test_clean_manual_worktree_is_never_removed(tmp_path):
    def git(*args):
        subprocess.run(['git', *args], check=True, capture_output=True)
    project = tmp_path / 'repo'
    project.mkdir()
    git('init', str(project))
    git('-C', str(project), '-c', 'user.name=Test', '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'initial')
    manual = tmp_path / 'manual'
    git('-C', str(project), 'worktree', 'add', '--detach', str(manual))
    assert not prune_git_worktree(str(project), manual)
    assert (manual / '.git').exists()


@pytest.mark.asyncio
async def test_review_gate_waits_for_all_roles_on_same_revision(tmp_path):
    import json
    from datetime import UTC, datetime
    from agent_kanban_pm.models import AgentSession, AgentSessionStatus, StagePolicy
    from agent_kanban_pm.runtime.review_gate import completion_blocker
    task_id, owner_id = await seed_task('Review')
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        review = await db.get(Stage, task.stage_id)
        done = Stage(name='Done', order=2, project_id=task.project_id)
        db.add(done)
        await db.flush()
        db.add(StagePolicy(project_id=task.project_id, stage_id=review.id, stage_key='review',
                           on_enter_roles_json='["test", "diff_review"]', required_outputs_json='["test_result", "diff_review_result"]'))
        source = AgentSession(agent_id=owner_id, project_id=task.project_id, task_id=task.id,
                              workspace_path=str(tmp_path), status=AgentSessionStatus.DONE,
                              assigned_role="worker", ended_at=datetime.now(UTC),
                              handoff_received_at=datetime.now(UTC), work_revision="abc")
        db.add(source)
        await db.flush()
        first = AgentSession(agent_id=owner_id, project_id=task.project_id, task_id=task.id,
                             workspace_path=str(tmp_path), status=AgentSessionStatus.DONE,
                             ended_at=datetime.now(UTC), handoff_received_at=datetime.now(UTC),
                             assigned_role='test', source_session_id=source.id, work_revision='abc',
                             handoff_outputs_json=json.dumps(['test_result']))
        db.add(first)
        await db.flush()
        assert 'diff_review' in await completion_blocker(db, first, task, review, done)
        second = AgentSession(agent_id=owner_id, project_id=task.project_id, task_id=task.id,
                              workspace_path=str(tmp_path), status=AgentSessionStatus.DONE,
                              ended_at=datetime.now(UTC), handoff_received_at=datetime.now(UTC),
                              assigned_role='diff_review', source_session_id=source.id, work_revision='old',
                              handoff_outputs_json=json.dumps(['diff_review_result']))
        db.add(second)
        await db.flush()
        assert await completion_blocker(db, first, task, review, done)
        second.work_revision = 'abc'
        from agent_kanban_pm.models import DiffReview, DiffReviewStatus
        db.add(DiffReview(project_id=task.project_id, task_id=task.id, diff_content="patch",
                          work_revision="abc", diff_sha256="a" * 64,
                          status=DiffReviewStatus.APPROVED))
        await db.flush()
        assert await completion_blocker(db, first, task, review, done) is None
        second.handoff_outputs_json = '[]'
        stale = AgentSession(agent_id=owner_id, project_id=task.project_id, task_id=task.id,
                             workspace_path=str(tmp_path), status=AgentSessionStatus.DONE,
                             assigned_role="diff_review", source_session_id=source.id,
                             ended_at=datetime.now(UTC), handoff_received_at=datetime.now(UTC),
                             work_revision="old", handoff_outputs_json='["diff_review_result"]')
        db.add(stale)
        await db.flush()
        assert 'Missing required outputs' in await completion_blocker(db, first, task, review, done)
        second.handoff_outputs_json = '["diff_review_result"]'
        source.work_revision = 'new-implementation'
        await db.flush()
        assert 'current implementation' in await completion_blocker(db, first, task, review, done)
        await db.rollback()


@pytest.mark.asyncio
async def test_durable_scheduler_retries_capacity_and_cancels_removed_assignment():
    from agent_kanban_pm.models import LaunchRequest, task_assignments
    from agent_kanban_pm.runtime.scheduler import request_launch, dispatch_launches
    task_id, owner_id = await seed_task('To Do')
    async with async_session_maker() as db:
        await db.execute(task_assignments.insert().values(task_id=task_id, entity_id=owner_id))
        event = EventBus().enqueue(db, 'task_assigned', {'task_id': task_id, 'entity_id': owner_id})
        await db.commit()
        event_id = event.id
    payload = {'event_id': event_id, 'data': {'task_id': task_id, 'entity_id': owner_id}}
    await request_launch(payload)
    await request_launch(payload)
    async with async_session_maker() as db:
        target_request_id = await db.scalar(
            select(LaunchRequest.id).where(LaunchRequest.event_id == event_id)
        )
    class Launcher:
        result = None
        calls = 0
        async def launch_for_assignment(self, *args, **kwargs):
            # dispatch_launches reconciles the global queue. Other tests can
            # legitimately leave unrelated requests pending, so only count
            # and resolve the request this test created.
            if kwargs.get('launch_request_id') == target_request_id:
                self.calls += 1
                return self.result
            return None
    launcher = Launcher()
    await dispatch_launches(launcher)
    async with async_session_maker() as db:
        rows = list((await db.execute(select(LaunchRequest).where(LaunchRequest.event_id == event_id))).scalars())
        assert len(rows) == 1
        assert rows[0].status == 'queued'
        rows[0].retry_at = None
        await db.commit()
    launcher.result = 123
    await dispatch_launches(launcher)
    async with async_session_maker() as db:
        row = await db.scalar(select(LaunchRequest).where(LaunchRequest.event_id == event_id))
        assert row.status == 'started'
        row.status = 'queued'
        row.retry_at = None
        await db.execute(task_assignments.delete().where(task_assignments.c.task_id == task_id))
        await db.commit()
    await dispatch_launches(launcher)
    assert launcher.calls == 2


@pytest.mark.asyncio
async def test_project_event_reaches_its_board_and_dashboard_once():
    from agent_kanban_pm.ws import ConnectionManager
    manager = ConnectionManager()
    class Socket:
        def __init__(self):
            self.messages = []
        async def accept(self):
            pass
        async def send_json(self, value):
            self.messages.append(value)
    first, other, dashboard = Socket(), Socket(), Socket()
    await manager.connect(first, 1)
    await manager.connect(other, 2)
    await manager.connect(dashboard)
    await manager.broadcast_to_project({'project_id': 1}, 1)
    assert len(first.messages) == len(dashboard.messages) == 1
    assert other.messages == []


@pytest.mark.asyncio
async def test_approval_cannot_block_another_agents_session(tmp_path):
    from agent_kanban_pm.models import AgentSession
    from agent_kanban_pm.services.coordination import validate_approval_scope
    task_id, owner_id = await seed_task()
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        agent = Entity(name=uuid.uuid4().hex, entity_type=EntityType.AGENT, role=Role.WORKER)
        db.add(agent)
        await db.flush()
        session = AgentSession(agent_id=agent.id, project_id=task.project_id, task_id=task.id, workspace_path=str(tmp_path))
        db.add(session)
        await db.flush()
        with pytest.raises(PermissionError):
            await validate_approval_scope(db, task.project_id, task.id, session.id, owner_id)
        assert await validate_approval_scope(db, task.project_id, None, session.id, agent.id) == task.id
        await db.rollback()


def test_viewer_or_requester_cannot_approve_diff_review():
    from types import SimpleNamespace
    from agent_kanban_pm.services.coordination import authorize_review_update
    review = SimpleNamespace(reviewer_id=2, requester_id=1)
    for actor in [SimpleNamespace(id=2, role=Role.VIEWER, is_active=True),
                  SimpleNamespace(id=1, role=Role.WORKER, is_active=True)]:
        with pytest.raises(PermissionError):
            authorize_review_update(review, actor)
    authorize_review_update(review, SimpleNamespace(id=2, role=Role.WORKER, is_active=True))

@pytest.mark.asyncio
async def test_outbox_retries_only_failed_delivery_channels(monkeypatch):
    from agent_kanban_pm.models import OutboxDelivery
    await init_db()
    event_type = uuid.uuid4().hex
    bus = EventBus()
    calls = {"stable": 0, "flaky": 0}
    async def stable(payload):
        calls["stable"] += 1
    async def flaky(payload):
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("temporary failure")
    async def skip_persistence(*args):
        return None
    monkeypatch.setattr(bus, "_persist_for_agents", skip_persistence)
    bus.subscribe(event_type, stable)
    bus.subscribe(event_type, flaky)
    async with async_session_maker() as db:
        event = bus.enqueue(db, event_type, {"value": 1})
        await db.commit()
        event_id = event.id
    await bus.dispatch_pending()
    async with async_session_maker() as db:
        row = await db.get(OutboxEvent, event_id)
        assert row.delivered_at is None
        row.retry_at = None
        await db.commit()
    await bus.dispatch_pending()
    assert calls == {"stable": 1, "flaky": 2}
    async with async_session_maker() as db:
        assert (await db.get(OutboxEvent, event_id)).delivered_at is not None
        receipts = list((await db.execute(select(OutboxDelivery).where(OutboxDelivery.event_id == event_id))).scalars())
        assert len(receipts) == 3


@pytest.mark.asyncio
async def test_latest_review_decision_supersedes_older_approval():
    from agent_kanban_pm.models import DiffReview, DiffReviewStatus
    from agent_kanban_pm.runtime.stage_policy import gather_transition_context
    task_id, _ = await seed_task()
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        db.add(DiffReview(project_id=task.project_id, task_id=task.id, work_revision="abc",
                          diff_content="patch", diff_sha256="a" * 64, status=DiffReviewStatus.APPROVED))
        await db.flush()
        db.add(DiffReview(project_id=task.project_id, task_id=task.id, work_revision="abc",
                          diff_content="patch", diff_sha256="a" * 64, status=DiffReviewStatus.CHANGES_REQUESTED))
        await db.flush()
        context = await gather_transition_context(db, task.id, task.project_id, work_revision="abc")
        assert context["has_diff_review"] is False


def test_runtime_status_file_is_not_implementation_evidence(tmp_path):
    from agent_kanban_pm.runtime.workspaces import git_revision
    from agent_kanban_pm.services.git_diff import read_task_git_diff
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    (repo / "STATUS.md").write_text("initial status\n")
    (repo / "implementation.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.com", "commit", "-m", "initial"],
                   check=True, capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    (repo / "STATUS.md").write_text("runtime update\n")
    assert git_revision(str(repo)) == head
    branch = subprocess.run(["git", "-C", str(repo), "branch", "--show-current"],
                            check=True, capture_output=True, text=True).stdout.strip()
    snapshot = read_task_git_diff(str(repo), str(repo), branch)
    assert snapshot is not None
    assert "STATUS.md" not in snapshot["diff"]
