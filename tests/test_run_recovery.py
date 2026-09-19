"""Crash-boundary tests using deterministic subprocesses, never real agents."""
import json
from datetime import UTC, datetime
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from agent_kanban_pm.runtime.run_guard import execution_state, execute_once, UNCERTAIN_EXIT


def test_claimed_run_is_never_replayed(tmp_path):
    receipt = str(tmp_path / 'run.json')
    output = tmp_path / 'side-effect.txt'
    args = [sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).open("a").write("once\\n")', str(output)]
    assert execution_state(receipt) == ('missing', None)
    assert execute_once(receipt, args) == 0
    assert execution_state(receipt) == ('exited', 0)
    assert execute_once(receipt, args) == 75
    assert output.read_text() == 'once\n'


def test_claim_without_live_process_is_explicit_failure(tmp_path):
    receipt = tmp_path / 'run.json'
    receipt.write_text(json.dumps({'status': 'claimed'}))
    assert execution_state(str(receipt)) == ('exited', UNCERTAIN_EXIT)
    assert execute_once(str(receipt), [sys.executable, '-c', 'raise RuntimeError("must not execute")']) == 75


def test_remote_observer_recognizes_fresh_guard_heartbeat(tmp_path, monkeypatch):
    receipt = tmp_path / "remote-running.json"
    receipt.write_text(json.dumps({
        "version": 2, "status": "running", "hostname": "another-host",
        "child_pid": 1234, "child_identity": "5678",
        "heartbeat_at": datetime.now(UTC).isoformat(),
    }))
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
    assert execution_state(str(receipt)) == ("running", None)


def test_remote_claim_is_never_replayed_or_reported_as_local_failure(tmp_path, monkeypatch):
    receipt = tmp_path / "remote.json"
    receipt.write_text(json.dumps({
        "version": 2, "status": "running", "hostname": "another-host",
        "child_pid": 1234, "child_identity": "5678",
    }))
    monkeypatch.setattr(socket, "gethostname", lambda: "this-host")
    assert execution_state(str(receipt)) == ("unknown", None)
    assert execute_once(str(receipt), [sys.executable, "-c", "raise RuntimeError()"]) == 75


def test_new_observer_recognizes_running_guard(tmp_path):
    receipt = str(tmp_path / 'run.json')
    release = tmp_path / 'release'
    code = 'import pathlib, sys, time\nwhile not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)'
    env = dict(os.environ, PYTHONPATH=str(Path('src').resolve()))
    process = subprocess.Popen([sys.executable, '-m', 'agent_kanban_pm.runtime.run_guard', receipt,
                                sys.executable, '-c', code, str(release)], env=env)
    try:
        deadline = time.monotonic() + 5
        while execution_state(receipt)[0] != 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        assert execution_state(receipt) == ('running', None)
        assert execute_once(receipt, [sys.executable, '-c', 'raise RuntimeError("duplicate")']) == 75
        release.touch()
        assert process.wait(timeout=5) == 0
        assert execution_state(receipt) == ('exited', 0)
    finally:
        release.touch()
        process.wait(timeout=5)


import pytest
from sqlalchemy import select
from test_lifecycle_chain import chain, finish  # noqa: F401
from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.events import EventBus, EventType
from agent_kanban_pm.models import AgentSession, LaunchRequest, Task, Project, Entity, ApprovalStatus, AgentSessionStatus
from agent_kanban_pm.runtime.assignment_launcher import AssignmentLauncher
from agent_kanban_pm.runtime.scheduler import dispatch_launches, request_launch
from agent_kanban_pm.runtime.scheduler import recover_legacy_assignments


class SimulatedCrash(BaseException):
    pass


async def queued_worker(task_id, agent_id):
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        event = EventBus().enqueue(db, EventType.TASK_ASSIGNED.value,
                                  {'task_id': task_id, 'entity_id': agent_id, 'role': 'worker', 'stage_id': task.stage_id},
                                  project_id=task.project_id)
        await db.commit()
        payload = json.loads(event.payload)
        payload['event_id'] = event.id
    await request_launch(payload)
    async with async_session_maker() as db:
        return await db.scalar(select(LaunchRequest.id).where(LaunchRequest.event_id == event.id))


@pytest.mark.asyncio
@pytest.mark.parametrize('after_spawn', [False, True])
async def test_restart_recovers_same_run_across_spawn_boundary(chain, monkeypatch, after_spawn):
    from agent_kanban_pm.runtime import assignment_launcher as module
    task_id, agent_id, agent_name, stages, launches = chain
    request_id = await queued_worker(task_id, agent_id)
    original = module.start_session

    def interrupted(**kwargs):
        if after_spawn:
            original(**kwargs)
        raise SimulatedCrash()

    monkeypatch.setattr(module, 'start_session', interrupted)
    with pytest.raises(SimulatedCrash):
        await dispatch_launches(AssignmentLauncher())
    async with async_session_maker() as db:
        session = await db.scalar(select(AgentSession).where(AgentSession.launch_request_id == request_id))
        session_id = session.id
        assert (await db.get(Task, task_id)).stage_id == stages[1]
    monkeypatch.setattr(module, 'start_session', original)
    await dispatch_launches(AssignmentLauncher())
    await dispatch_launches(AssignmentLauncher())
    async with async_session_maker() as db:
        request = await db.get(LaunchRequest, request_id)
        assert request.status == 'started'
        assert request.session_id == session_id
        assert len(list((await db.execute(select(AgentSession).where(AgentSession.task_id == task_id))).scalars())) == 1
    assert len(launches) == 1
    assert (await finish(session_id, agent_name)).status == AgentSessionStatus.DONE


@pytest.mark.asyncio
async def test_revoked_project_cannot_launch_queued_work(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    request_id = await queued_worker(task_id, agent_id)
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        project = await db.get(Project, task.project_id)
        project.approval_status = ApprovalStatus.REJECTED
        await db.commit()
    await dispatch_launches(AssignmentLauncher())
    assert not launches
    async with async_session_maker() as db:
        request = await db.get(LaunchRequest, request_id)
        assert request.status == 'blocked'
        assert request.session_id is None


@pytest.mark.asyncio
async def test_restart_reconciles_assignment_event_without_launch_request(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        project = await db.get(Project, task.project_id)
        event = EventBus().enqueue(
            db, EventType.TASK_ASSIGNED.value,
            {"task_id": task.id, "entity_id": agent_id, "role": "worker", "stage_id": task.stage_id},
            project_id=task.project_id, entity_id=task.created_by,
        )
        await db.flush()
        event.delivered_at = datetime.now(UTC)
        await db.commit()
        event_id = event.id
        workspace = project.path

    await recover_legacy_assignments(workspace)
    await recover_legacy_assignments(workspace)

    async with async_session_maker() as db:
        requests = list((await db.execute(select(LaunchRequest).where(
            LaunchRequest.event_id == event_id,
        ))).scalars())
        assert len(requests) == 1
        assert requests[0].task_id == task_id
        assert requests[0].agent_id == agent_id
        assert requests[0].status == "queued"


@pytest.mark.asyncio
async def test_cancel_during_launch_preparation_prevents_execution(chain, monkeypatch):
    import asyncio
    from agent_kanban_pm.runtime import assignment_launcher as module
    from agent_kanban_pm.routers.agent_activity import LaunchQueueAction, cancel_launch_request

    task_id, agent_id, agent_name, stages, launches = chain
    request_id = await queued_worker(task_id, agent_id)
    loop = asyncio.get_running_loop()
    original = module._build_agent_command

    async def cancel():
        async with async_session_maker() as db:
            task = await db.get(Task, task_id)
            owner = await db.get(Entity, task.created_by)
            await cancel_launch_request(
                request_id, LaunchQueueAction(reason="cancel during preparation"),
                db=db, current_entity=owner,
            )

    def cancel_before_reservation(*args, **kwargs):
        asyncio.run_coroutine_threadsafe(cancel(), loop).result(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_build_agent_command", cancel_before_reservation)
    await dispatch_launches(AssignmentLauncher())
    assert not launches
    async with async_session_maker() as db:
        request = await db.get(LaunchRequest, request_id)
        assert request.status == "cancelled"
        assert request.session_id is None
        assert request.claim_token is None
        assert await db.scalar(select(AgentSession.id).where(
            AgentSession.launch_request_id == request_id,
        )) is None


def test_guard_crash_does_not_make_surviving_child_replayable(tmp_path):
    receipt = str(tmp_path / 'run.json')
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    code = ('import pathlib, sys, time\npathlib.Path(sys.argv[1]).touch()\n'
            'while not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)')
    env = dict(os.environ, PYTHONPATH=str(Path('src').resolve()))
    guard = subprocess.Popen([sys.executable, '-m', 'agent_kanban_pm.runtime.run_guard', receipt,
                              sys.executable, '-c', code, str(ready), str(release)], env=env)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists()
        guard.kill()
        guard.wait(timeout=5)
        assert execution_state(receipt) == ('running', None)
        assert execute_once(receipt, [sys.executable, '-c', 'raise RuntimeError("duplicate")']) == 75
    finally:
        release.touch()
        if guard.poll() is None:
            guard.kill()
        guard.wait(timeout=5)
    deadline = time.monotonic() + 5
    while execution_state(receipt)[0] == 'running' and time.monotonic() < deadline:
        time.sleep(.01)
    assert execution_state(receipt) == ('exited', UNCERTAIN_EXIT)


@pytest.mark.asyncio
async def test_revocation_after_reservation_still_prevents_execution(chain, monkeypatch):
    from agent_kanban_pm.runtime import assignment_launcher as module
    task_id, agent_id, agent_name, stages, launches = chain
    request_id = await queued_worker(task_id, agent_id)
    original = module.start_session
    def interrupted(**kwargs):
        raise SimulatedCrash()
    monkeypatch.setattr(module, 'start_session', interrupted)
    with pytest.raises(SimulatedCrash):
        await dispatch_launches(AssignmentLauncher())
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        project = await db.get(Project, task.project_id)
        project.approval_status = ApprovalStatus.REJECTED
        await db.commit()
    monkeypatch.setattr(module, 'start_session', original)
    await dispatch_launches(AssignmentLauncher())
    assert not launches
    async with async_session_maker() as db:
        request = await db.get(LaunchRequest, request_id)
        assert request.status == 'blocked'
        assert 'approved' in request.last_error
        assert request.session_id is not None


@pytest.mark.asyncio
async def test_cancelled_failed_reservation_cannot_revive(chain, monkeypatch):
    from agent_kanban_pm.runtime import assignment_launcher as module
    from agent_kanban_pm.routers.agent_activity import (
        LaunchQueueAction, cancel_launch_request, retry_launch_request,
    )

    task_id, agent_id, agent_name, stages, launches = chain
    request_id = await queued_worker(task_id, agent_id)
    original_start = module.start_session

    def crash_before_spawn(**kwargs):
        raise SimulatedCrash()

    monkeypatch.setattr(module, "start_session", crash_before_spawn)
    with pytest.raises(SimulatedCrash):
        await dispatch_launches(AssignmentLauncher())

    async with async_session_maker() as db:
        request = await db.get(LaunchRequest, request_id)
        original_session_id = request.session_id
        task = await db.get(Task, task_id)
        project = await db.get(Project, task.project_id)
        owner = await db.get(Entity, task.created_by)
        project.approval_status = ApprovalStatus.REJECTED
        await db.commit()

    monkeypatch.setattr(module, "start_session", original_start)
    await dispatch_launches(AssignmentLauncher())

    async with async_session_maker() as db:
        owner = await db.get(Entity, owner.id)
        cancelled = await cancel_launch_request(
            request_id, LaunchQueueAction(reason="cancel failed reservation"),
            db=db, current_entity=owner,
        )
        assert cancelled["status"] == "cancelled"
        original_session = await db.get(AgentSession, original_session_id)
        assert original_session.status == AgentSessionStatus.ERROR
        assert original_session.ended_at is not None
        task = await db.get(Task, task_id)
        project = await db.get(Project, task.project_id)
        project.approval_status = ApprovalStatus.APPROVED
        await db.commit()
        retried = await retry_launch_request(
            request_id, LaunchQueueAction(reason="new approved attempt"),
            db=db, current_entity=owner,
        )
        retry_id = retried["id"]

    await dispatch_launches(AssignmentLauncher())
    assert len(launches) == 1
    async with async_session_maker() as db:
        original = await db.get(LaunchRequest, request_id)
        retried = await db.get(LaunchRequest, retry_id)
        assert original.status == "cancelled"
        assert original.session_id == original_session_id
        assert retried.status == "started"
        assert retried.session_id != original_session_id


@pytest.mark.asyncio
async def test_interrupted_claim_fails_visibly_without_replay(chain, monkeypatch):
    from agent_kanban_pm.runtime import assignment_launcher as module
    from agent_kanban_pm.runtime.task_runner import launch_spec
    task_id, agent_id, agent_name, stages, launches = chain
    request_id = await queued_worker(task_id, agent_id)
    def interrupted(**kwargs):
        raise SimulatedCrash()
    monkeypatch.setattr(module, 'start_session', interrupted)
    with pytest.raises(SimulatedCrash):
        await dispatch_launches(AssignmentLauncher())
    async with async_session_maker() as db:
        session = await db.scalar(select(AgentSession).where(AgentSession.launch_request_id == request_id))
        path = Path(launch_spec(session)['receipt'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'status': 'claimed'}))
    await dispatch_launches(AssignmentLauncher())
    assert not launches
    saved = await finish(session.id, agent_name)
    assert saved.status == AgentSessionStatus.ERROR
    assert saved.exit_code == UNCERTAIN_EXIT
    async with async_session_maker() as db:
        assert (await db.get(LaunchRequest, request_id)).status == 'failed'


@pytest.mark.asyncio
async def test_streamer_waits_for_surviving_child_after_transport_loss(chain, monkeypatch):
    from agent_kanban_pm.runtime.process_launcher import RunnerState
    from agent_kanban_pm.runtime.session_streamer import _stream_one_session
    task_id, agent_id, agent_name, stages, launches = chain
    session_id = await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker')
    monkeypatch.setattr('agent_kanban_pm.runtime.session_streamer._runner_state', lambda *a: RunnerState('exited', 137))
    monkeypatch.setattr('agent_kanban_pm.runtime.task_runner.receipt_state', lambda *a: RunnerState('running'))
    async with async_session_maker() as db:
        session = await db.get(AgentSession, session_id)
    await _stream_one_session(session, agent_name)
    async with async_session_maker() as db:
        assert (await db.get(AgentSession, session_id)).ended_at is None
        assert (await db.get(Task, task_id)).stage_id == stages[1]

@pytest.mark.asyncio
async def test_launch_queue_manager_controls(chain):
    from datetime import UTC, datetime, timedelta
    from agent_kanban_pm.routers.agent_activity import (
        LaunchQueueAction, cancel_launch_request, cleanup_launch_requests,
        list_launch_requests, retry_launch_request,
    )
    task_id, agent_id, agent_name, stages, launches = chain
    request_id = await queued_worker(task_id, agent_id)
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        owner = await db.get(Entity, task.created_by)
        cancelled = await cancel_launch_request(
            request_id, LaunchQueueAction(reason="operator cancelled"), db=db, current_entity=owner)
        assert cancelled["status"] == "cancelled"
        retried = await retry_launch_request(
            request_id, LaunchQueueAction(reason="operator retry"), db=db, current_entity=owner)
        assert retried["status"] == "queued"
        assert retried["id"] != request_id
        visible = await list_launch_requests(
            project_id=task.project_id, status_filter=None, include_archived=False,
            limit=100, db=db, current_entity=owner)
        assert {row["id"] for row in visible} >= {request_id, retried["id"]}
        old = await db.get(LaunchRequest, request_id)
        old.created_at = datetime.now(UTC) - timedelta(days=40)
        await db.commit()
        result = await cleanup_launch_requests(
            older_than_days=30, db=db, current_entity=owner)
        assert result["archived"] == 1
        hidden = await list_launch_requests(
            project_id=task.project_id, status_filter=None, include_archived=False,
            limit=100, db=db, current_entity=owner)
        assert request_id not in {row["id"] for row in hidden}
