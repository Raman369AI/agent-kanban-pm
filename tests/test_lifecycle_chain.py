"""Exercise durable handoffs with actual Git and a deterministic subprocess CLI."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select, delete

from agent_kanban_pm.db import async_session_maker, init_db
from agent_kanban_pm.events import EventBus, EventType
from agent_kanban_pm.models import (
    AgentSession, AgentSessionStatus, ApprovalStatus, Entity, EntityType,
    LaunchRequest, OutboxEvent, DiffReview, DiffReviewStatus, Project, Role, Stage, StagePolicy, Task,
    TaskStatus, task_assignments,
)
from agent_kanban_pm.runtime.assignment_launcher import AssignmentLauncher
from agent_kanban_pm.runtime.adapter_loader import AdapterSpec, InvokeSpec, TaskCommandSpec
from agent_kanban_pm.runtime.preferences import Preferences, RoleAssignment, RoleConfig
from agent_kanban_pm.runtime.process_launcher import RunnerState
from agent_kanban_pm.runtime.scheduler import dispatch_launches, request_launch
from agent_kanban_pm.runtime.session_streamer import _stream_one_session
from agent_kanban_pm.runtime.workspaces import WorkspacePreparationError


FAKE_CLI = '''
import os, subprocess, sys
from pathlib import Path
from agent_kanban_pm.runtime.handoff_protocol import update_status_file
role = os.environ['KANBAN_AGENT_ROLE']
outputs = {'worker': ['code_changes', 'status_summary'], 'test': ['test_result'],
           'diff_review': ['diff_review_result'], 'git_pr': ['final_summary']}[role]
assert all(output in sys.argv[-1] for output in outputs), 'Missing output contract in prompt'
if role == 'worker':
    assert 'Commit completed implementation changes' in sys.argv[-1]
    Path('implementation.txt').write_text('review this implementation\\n')
    subprocess.run(['git', 'add', 'implementation.txt'], check=True)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '-m', 'Implementation'], check=True, capture_output=True)
else:
    assert Path('implementation.txt').read_text() == 'review this implementation\\n'
artifacts = ([{'kind': 'pull_request', 'provider': 'github',
               'url': 'https://github.com/example/repo/pull/42'}]
             if role == 'git_pr' else [])
update_status_file(Path.cwd(), {'state': 'done', 'handoff_ready': True,
                               'summary': role + ' completed', 'outputs': outputs,
                               'artifacts': artifacts})
'''


@pytest.fixture
async def chain(tmp_path, monkeypatch):
    await init_db()
    project_path = tmp_path / 'project'
    subprocess.run(['git', 'init', str(project_path)], check=True, capture_output=True)
    subprocess.run(['git', '-C', str(project_path), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.com', 'commit', '--allow-empty', '-m', 'Initial'],
                   check=True, capture_output=True)
    async with async_session_maker() as db:
        owner = Entity(name=f'chain-owner-{tmp_path.name}', entity_type=EntityType.HUMAN, role=Role.OWNER)
        agent = Entity(name=f'chain-cli-{tmp_path.name}', entity_type=EntityType.AGENT, role=Role.WORKER)
        db.add_all([owner, agent])
        await db.flush()
        project = Project(name=f'chain-{tmp_path.name}', path=str(project_path),
                          creator_id=owner.id, approval_status=ApprovalStatus.APPROVED)
        db.add(project)
        await db.flush()
        stages = [Stage(project_id=project.id, name=name, order=i)
                  for i, name in enumerate(['To Do', 'In Progress', 'Review', 'Done'])]
        db.add_all(stages)
        await db.flush()
        for stage, roles, outputs in [
            (stages[1], ['worker'], ['code_changes', 'status_summary']),
            (stages[2], ['test', 'diff_review', 'git_pr'], ['test_result', 'diff_review_result', 'final_summary']),
            (stages[3], [], []),
        ]:
            db.add(StagePolicy(project_id=project.id, stage_id=stage.id, stage_key=stage.key,
                               on_enter_roles_json=json.dumps(roles), required_outputs_json=json.dumps(outputs),
                               requires_orchestrator_move=False))
        task = Task(title='Full chain', project_id=project.id, stage_id=stages[0].id,
                    status=TaskStatus.PENDING, created_by=owner.id)
        db.add(task)
        await db.flush()
        await db.execute(task_assignments.insert().values(task_id=task.id, entity_id=agent.id))
        await db.commit()
        task_id, agent_id, agent_name = task.id, agent.id, agent.name
        stage_ids = [s.id for s in stages]
    assignment = RoleAssignment(agent=agent_name)
    prefs = Preferences(roles=RoleConfig(**{r: assignment for r in ['worker', 'test', 'diff_review', 'git_pr']}))
    adapter = AdapterSpec(name=agent_name, display_name="Test CLI", invoke=InvokeSpec(command=sys.executable),
                          task_command=TaskCommandSpec(args=['-c', FAKE_CLI, '{prompt}']))
    monkeypatch.setattr('agent_kanban_pm.runtime.assignment_launcher.load_preferences', lambda: prefs)
    monkeypatch.setattr('agent_kanban_pm.runtime.preferences.load_preferences', lambda: prefs)
    monkeypatch.setattr('agent_kanban_pm.runtime.assignment_launcher.load_all_adapters', lambda: [adapter])
    monkeypatch.setattr('agent_kanban_pm.runtime.assignment_launcher._tmux_available', lambda: True)
    monkeypatch.setattr('agent_kanban_pm.runtime.assignment_launcher._git_worktree_path', lambda *a: tmp_path / 'task-worktree')
    monkeypatch.setattr('agent_kanban_pm.runtime.assignment_launcher.session_state', lambda *a: RunnerState('missing'))
    monkeypatch.setattr('agent_kanban_pm.runtime.session_streamer._runner_state', lambda *a: RunnerState('exited', 0))
    monkeypatch.setattr('agent_kanban_pm.runtime.session_streamer._tmux_has_session', lambda *a: False)
    monkeypatch.setattr('agent_kanban_pm.runtime.session_streamer.kill_session', lambda *a: True)
    monkeypatch.setattr('agent_kanban_pm.runtime.session_streamer.capture_pane', lambda *a, **kw: '')
    monkeypatch.setattr(
        'agent_kanban_pm.runtime.integration_evidence.verify_pull_request_handoff',
        lambda workspace, artifacts, expected: {
            'kind': 'pull_request', 'provider': 'github',
            'url': artifacts[0]['url'], 'external_id': '42', 'state': 'merged',
            'merged_at': '2026-09-19T00:00:00Z', 'head_revision': expected,
            'verified_at': '2026-09-19T00:00:01Z',
        },
    )
    launches = []

    def fake_terminal(**kwargs):
        env = dict(kwargs['env'], PYTHONPATH=str(Path('src').resolve()))
        result = subprocess.run(kwargs['args'], cwd=kwargs['cwd'], env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        launches.append({"cwd": kwargs["cwd"], "env": {"KANBAN_AGENT_ROLE": kwargs["env"]["KANBAN_AGENT_ROLE"]}})
    monkeypatch.setattr('agent_kanban_pm.runtime.assignment_launcher.start_session', fake_terminal)
    try:
        yield task_id, agent_id, agent_name, stage_ids, launches
    finally:
        async with async_session_maker() as db:
            for event in (await db.execute(select(OutboxEvent))).scalars():
                if json.loads(event.payload).get('project_id') == project.id or json.loads(event.payload).get('data', {}).get('task_id') == task_id:
                    await db.delete(event)
            await db.execute(delete(Project).where(Project.id == project.id))
            await db.execute(delete(Entity).where(Entity.id.in_([owner.id, agent_id])))
            await db.commit()


async def finish(session_id, agent_name):
    async with async_session_maker() as db:
        session = await db.get(AgentSession, session_id)
    await _stream_one_session(session, agent_name)
    async with async_session_maker() as db:
        return await db.get(AgentSession, session_id)


@pytest.mark.asyncio
async def test_full_chain_same_cli_restart_and_serial_reviews(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    launcher = AssignmentLauncher()
    worker_id = await launcher.launch_for_assignment(task_id, agent_id, 'worker')
    worker = await finish(worker_id, agent_name)
    assert worker.status == AgentSessionStatus.DONE
    assert worker.work_revision
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[2]
        pending = list((await db.execute(select(OutboxEvent).where(OutboxEvent.delivered_at.is_(None)))).scalars())
        review_events = [e for e in pending if json.loads(e.payload).get('data', {}).get('task_id') == task_id
                         and json.loads(e.payload)['event_type'] == EventType.TASK_ASSIGNED.value]
        assert len(review_events) == 3
        moved = [json.loads(e.payload) for e in pending if json.loads(e.payload)["event_type"] == EventType.TASK_MOVED.value
                 and json.loads(e.payload).get("data", {}).get("task_id") == task_id
                 and json.loads(e.payload)["data"].get("to_stage_id") == stages[2]]
        assert len(moved) == 1
    # No dispatch happened before this point: a new bus recovers committed intent.
    bus = EventBus()
    bus.subscribe(EventType.TASK_ASSIGNED.value, request_launch)
    while await bus.dispatch_pending():
        pass
    await dispatch_launches(launcher)
    async with async_session_maker() as db:
        requests = list((await db.execute(select(LaunchRequest).where(LaunchRequest.task_id == task_id)
                                         .order_by(LaunchRequest.id))).scalars())
        assert [r.status for r in requests] == ['started', 'queued', 'queued']
        test_id = requests[0].session_id
    test = await finish(test_id, agent_name)
    assert test.source_session_id == worker_id
    assert test.work_revision == worker.work_revision
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[2]
        row = await db.get(LaunchRequest, requests[1].id)
        row.retry_at = None
        await db.commit()
    await dispatch_launches(launcher)
    async with async_session_maker() as db:
        from agent_kanban_pm.services.coordination import review_evidence
        evidence = await review_evidence(db, worker.project_id, task_id)
        db.add(DiffReview(project_id=worker.project_id, task_id=task_id,
                          work_revision=worker.work_revision, diff_content=evidence["diff"],
                          diff_sha256=evidence["diff_sha256"], file_paths=evidence["file_paths"],
                          status=DiffReviewStatus.APPROVED))
        await db.commit()
        review_id = (await db.get(LaunchRequest, requests[1].id)).session_id
    review = await finish(review_id, agent_name)
    assert review.status == AgentSessionStatus.DONE
    assert review.source_session_id == worker_id
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[2]
        row = await db.get(LaunchRequest, requests[2].id)
        row.retry_at = None
        await db.commit()
    await dispatch_launches(launcher)
    async with async_session_maker() as db:
        git_id = (await db.get(LaunchRequest, requests[2].id)).session_id
    git = await finish(git_id, agent_name)
    assert git.source_session_id == worker_id
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[3]
        assert (await db.get(Task, task_id)).status == TaskStatus.COMPLETED
        events = [json.loads(row.payload) for row in (await db.execute(select(OutboxEvent))).scalars()]
        completed = [event for event in events if event['event_type'] == EventType.TASK_COMPLETED.value
                     and event['data'].get('task_id') == task_id]
        assert len(completed) == 1
    assert [l['env']['KANBAN_AGENT_ROLE'] for l in launches] == ['worker', 'test', 'diff_review', 'git_pr']
    assert len({l['cwd'] for l in launches}) == 1
    assert await launcher.launch_for_assignment(task_id, agent_id, 'git_pr') == git.id
    assert len(launches) == 4


@pytest.mark.asyncio
async def test_launch_rejects_changed_implementation(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    launcher = AssignmentLauncher()
    worker_id = await launcher.launch_for_assignment(task_id, agent_id, 'worker')
    worker = await finish(worker_id, agent_name)
    Path(worker.workspace_path, 'implementation.txt').write_text('changed after handoff')
    with pytest.raises(WorkspacePreparationError, match='Commit implementation'):
        await launcher.launch_for_assignment(task_id, agent_id, 'test')
    subprocess.run(['git', '-C', worker.workspace_path, 'add', 'implementation.txt'], check=True)
    subprocess.run(['git', '-C', worker.workspace_path, '-c', 'user.name=Test', '-c',
                    'user.email=test@example.com', 'commit', '-m', 'Different revision'], check=True, capture_output=True)
    with pytest.raises(WorkspacePreparationError, match='changed after handoff'):
        await launcher.launch_for_assignment(task_id, agent_id, 'test')
    assert len(launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing_output', 'nonzero_exit', 'changed_revision'])
async def test_reviewer_failure_never_completes_task(chain, monkeypatch, failure):
    from agent_kanban_pm.runtime.handoff_protocol import update_status_file
    task_id, agent_id, agent_name, stages, launches = chain
    launcher = AssignmentLauncher()
    worker = await finish(await launcher.launch_for_assignment(task_id, agent_id, 'worker'), agent_name)
    test_id = await launcher.launch_for_assignment(task_id, agent_id, 'test')
    if failure == 'missing_output':
        update_status_file(worker.workspace_path, {'outputs': []})
    elif failure == 'nonzero_exit':
        monkeypatch.setattr('agent_kanban_pm.runtime.session_streamer._runner_state', lambda *a: RunnerState('exited', 7))
    else:
        Path(worker.workspace_path, 'implementation.txt').write_text('reviewer changed code')
    test = await finish(test_id, agent_name)
    if failure != 'missing_output':
        assert test.status == AgentSessionStatus.ERROR
    else:
        assert test.status == AgentSessionStatus.DONE
        await finish(await launcher.launch_for_assignment(task_id, agent_id, 'diff_review'), agent_name)
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[2]


@pytest.mark.asyncio
async def test_legacy_worker_without_role_is_reviewable(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    launcher = AssignmentLauncher()
    worker = await finish(await launcher.launch_for_assignment(task_id, agent_id, 'worker'), agent_name)
    async with async_session_maker() as db:
        row = await db.get(AgentSession, worker.id)
        row.assigned_role = None
        await db.commit()
    assert await launcher.launch_for_assignment(task_id, agent_id, 'test')


@pytest.mark.asyncio
async def test_finalizer_requires_persisted_handoff(chain):
    from agent_kanban_pm.runtime.session_streamer import _finalize_completed_session
    task_id, agent_id, agent_name, stages, launches = chain
    session_id = await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker')
    async with async_session_maker() as db:
        session = await db.get(AgentSession, session_id)
    # A caller cannot skip run-scoped handoff ingestion by passing arbitrary text.
    assert await _finalize_completed_session(session, '', 'unverified text') is False
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[1]


@pytest.mark.asyncio
async def test_human_policy_preserved(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    async with async_session_maker() as db:
        policy = await db.scalar(select(StagePolicy).where(StagePolicy.stage_id == stages[2]))
        policy.requires_orchestrator_move = True
        await db.commit()
    worker = await finish(await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker'), agent_name)
    assert worker.status == AgentSessionStatus.DONE
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[1]


@pytest.mark.asyncio
async def test_queued_review_keeps_source_identity(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    launcher = AssignmentLauncher()
    worker = await finish(await launcher.launch_for_assignment(task_id, agent_id, 'worker'), agent_name)
    async with async_session_maker() as db:
        event = EventBus().enqueue(db, EventType.TASK_ASSIGNED.value,
                                  {'task_id': task_id, 'entity_id': agent_id, 'role': 'test',
                                   'stage_id': stages[2], 'source_session_id': worker.id + 9999})
        await db.commit()
        payload = json.loads(event.payload)
        payload['event_id'] = event.id
    await request_launch(payload)
    await dispatch_launches(launcher)
    async with async_session_maker() as db:
        request = await db.scalar(select(LaunchRequest).where(LaunchRequest.event_id == event.id))
        assert request.status == 'blocked'
        assert request.session_id is None
    assert len(launches) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['stale', 'critical', 'digest_mismatch', 'duplicate_current'])
async def test_conditional_review_requires_current_noncritical_approval(chain, evidence):
    from agent_kanban_pm.models import DiffReview, DiffReviewStatus, ReviewMode
    task_id, agent_id, agent_name, stages, launches = chain
    launcher = AssignmentLauncher()
    worker = await finish(await launcher.launch_for_assignment(task_id, agent_id, 'worker'), agent_name)
    await finish(await launcher.launch_for_assignment(task_id, agent_id, 'test'), agent_name)
    async with async_session_maker() as db:
        policy = await db.scalar(select(StagePolicy).where(StagePolicy.stage_id == stages[2]))
        policy.review_mode = ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL
        from agent_kanban_pm.services.coordination import review_evidence
        snapshot = await review_evidence(db, worker.project_id, task_id)
        for _ in range(2 if evidence == 'duplicate_current' else 1):
            db.add(DiffReview(project_id=worker.project_id, task_id=task_id, diff_content=snapshot["diff"],
                              work_revision='outdated' if evidence == 'stale' else worker.work_revision,
                              status=DiffReviewStatus.APPROVED, is_critical=evidence == 'critical',
                              diff_sha256=("b" * 64 if evidence == "digest_mismatch" else snapshot["diff_sha256"]), file_paths=snapshot["file_paths"]))
        await db.commit()
    await finish(await launcher.launch_for_assignment(task_id, agent_id, 'diff_review'), agent_name)
    await finish(await launcher.launch_for_assignment(task_id, agent_id, 'git_pr'), agent_name)
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        assert task.stage_id == (stages[3] if evidence == 'duplicate_current' else stages[2])


@pytest.mark.asyncio
@pytest.mark.parametrize('interface', ['rest', 'mcp'])
async def test_review_request_records_implementation_revision(chain, interface):
    from types import SimpleNamespace
    from agent_kanban_pm.models import DiffReview
    from agent_kanban_pm.routers.agent_activity import create_diff_review
    from agent_kanban_pm.schemas import DiffReviewCreate
    from agent_kanban_pm.mcp.server import KanbanMCPServer
    task_id, agent_id, agent_name, stages, launches = chain
    worker = await finish(await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker'), agent_name)
    args = {'project_id': worker.project_id, 'task_id': task_id, 'diff_content': 'implementation patch'}
    async with async_session_maker() as db:
        agent = await db.get(Entity, agent_id)
        if interface == 'rest':
            review = await create_diff_review(worker.project_id, DiffReviewCreate(**args), db=db, current_entity=agent)
            review_id = review.id
        else:
            result = await KanbanMCPServer._handle_request_diff_review(SimpleNamespace(caller_entity=agent), args)
            review_id = result['review_id']
    async with async_session_maker() as db:
        assert (await db.get(DiffReview, review_id)).work_revision == worker.work_revision
