"""Every task-move interface must enforce the same evidence and authority."""
from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from test_lifecycle_chain import chain, finish  # noqa: F401
from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import Entity, Project, StagePolicy, Task, TaskLog, OutboxEvent, Role
from agent_kanban_pm.routers.tasks import update_task
from agent_kanban_pm.routers.ui import ui_move_task
from agent_kanban_pm.mcp.server import KanbanMCPServer
from agent_kanban_pm.schemas import TaskUpdate
from agent_kanban_pm.runtime.assignment_launcher import AssignmentLauncher


async def move(interface, task_id, actor_id, stage_id=None, status=None, override_reason=None):
    data = {
        key: value
        for key, value in {
            'stage_id': stage_id,
            'status': status,
            'override_reason': override_reason,
        }.items()
        if value is not None
    }
    async with async_session_maker() as db:
        actor = await db.get(Entity, actor_id)
        if interface == 'mcp':
            server = SimpleNamespace(caller_entity=actor, _require_role=lambda *a: None)
            return await KanbanMCPServer._handle_move_task(server, dict(data, task_id=task_id))
        try:
            if interface == 'rest':
                return await update_task(task_id, TaskUpdate(**data), db=db, current_entity=actor)
            async def body():
                return data
            return await ui_move_task(task_id, SimpleNamespace(json=body), db=db, current_entity=actor)
        except HTTPException as exc:
            return {'error': exc.detail}


@pytest.mark.asyncio
@pytest.mark.parametrize('interface', ['rest', 'ui', 'mcp'])
async def test_worker_cannot_move_without_implementation_handoff(chain, interface):
    task_id, agent_id, agent_name, stages, launches = chain
    await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker')
    result = await move(interface, task_id, agent_id, stages[2])
    assert isinstance(result, dict) and result.get('error'), result
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('interface', ['rest', 'ui', 'mcp'])
async def test_manual_human_handoff_records_warning_and_stage_intent(chain, interface):
    task_id, agent_id, agent_name, stages, launches = chain
    await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker')
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        owner_id = (await db.get(Project, task.project_id)).creator_id
    blocked = await move(interface, task_id, owner_id, stages[2])
    assert isinstance(blocked, dict) and 'override reason' in blocked.get('error', '').lower()
    result = await move(
        interface,
        task_id,
        owner_id,
        stages[2],
        override_reason='Proceeding for a documented manual review.',
    )
    assert not isinstance(result, dict) or not result.get('error'), result
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        assert task.stage_id == stages[2]
        assert task.status.value == 'in_review'
        logs = list((await db.execute(select(TaskLog).where(TaskLog.task_id == task_id))).scalars())
        assert any(
            'Human transition override' in row.message
            and 'Proceeding for a documented manual review.' in row.message
            for row in logs
        )
        events = [json.loads(row.payload) for row in (await db.execute(select(OutboxEvent))).scalars()]
        intents = [event['data'] for event in events if event['event_type'] == 'task_assigned'
                   and event['data'].get('task_id') == task_id and event['data'].get('stage_id') == stages[2]]
        assert {item['role'] for item in intents} == {'test', 'diff_review', 'git_pr'}


@pytest.mark.asyncio
@pytest.mark.parametrize('interface', ['rest', 'mcp'])
async def test_status_only_completion_cannot_skip_review(chain, interface):
    task_id, agent_id, agent_name, stages, launches = chain
    await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker')
    result = await move(interface, task_id, agent_id, status='completed')
    assert isinstance(result, dict) and result.get('error'), result
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).status.value == 'in_progress'


@pytest.mark.asyncio
@pytest.mark.parametrize('interface', ['rest', 'ui', 'mcp'])
async def test_completion_emits_one_durable_event(chain, interface):
    task_id, agent_id, agent_name, stages, launches = chain
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        owner_id = (await db.get(Project, task.project_id)).creator_id
    for _ in range(2):
        result = await move(interface, task_id, owner_id, stages[3],
                            override_reason='Work verified manually by the project owner.')
        assert not isinstance(result, dict) or not result.get('error'), result
    async with async_session_maker() as db:
        events = [json.loads(row.payload) for row in (await db.execute(select(OutboxEvent))).scalars()]
        completed = [event for event in events if event['event_type'] == 'task_completed'
                     and event['data'].get('task_id') == task_id]
        assert len(completed) == 1
        assert completed[0]['entity_id'] == owner_id
        assert completed[0]['data']['status'] == 'completed'


@pytest.mark.asyncio
async def test_completion_gates_use_configured_policy_and_show_blocker(chain):
    from agent_kanban_pm.models import Stage, TaskStatus
    from agent_kanban_pm.runtime.review_gate import completion_gate_status

    task_id, agent_id, agent_name, stages, launches = chain
    async with async_session_maker() as db:
        policy = await db.scalar(select(StagePolicy).where(StagePolicy.stage_id == stages[2]))
        policy.on_enter_roles_json = json.dumps(['test'])
        policy.required_outputs_json = json.dumps(['test_result'])
        (await db.get(Stage, stages[2])).name = 'Quality checks'
        (await db.get(Stage, stages[3])).name = 'Shipped'
        task = await db.get(Task, task_id)
        task.stage_id = stages[2]
        task.status = TaskStatus.IN_REVIEW
        await db.commit()

        completion = await completion_gate_status(db, task)
        required = {gate['key'] for gate in completion['gates'] if gate['required']}
        assert required == {'implementation', 'test'}
        assert completion['blocker'] == 'Review has no implementation handoff identity'
        assert completion['can_complete_automatically'] is False


@pytest.mark.asyncio
async def test_name_is_not_orchestrator_authority(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    async with async_session_maker() as db:
        actor = await db.get(Entity, agent_id)
        actor.name = 'orchestrator'
        actor.role = Role.WORKER
        target = await db.scalar(select(StagePolicy).where(StagePolicy.stage_id == stages[2]))
        target.requires_orchestrator_move = True
        await db.commit()
    result = await move('rest', task_id, agent_id, stages[2])
    assert isinstance(result, dict) and 'orchestrator' in result.get('error', '').lower(), result


@pytest.mark.asyncio
@pytest.mark.parametrize('interface', ['rest', 'ui', 'mcp'])
async def test_human_cannot_override_unfinished_predecessor(chain, interface):
    task_id, agent_id, agent_name, stages, launches = chain
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        owner_id = (await db.get(Project, task.project_id)).creator_id
        task.sequence_order = 2
        db.add(Task(project_id=task.project_id, stage_id=stages[0], title='Must finish first',
                    sequence_order=1, created_by=owner_id))
        await db.commit()
    result = await move(interface, task_id, owner_id, stages[1])
    assert isinstance(result, dict) and 'predecessor' in result.get('error', ''), result


@pytest.mark.asyncio
async def test_launch_obeys_execution_stage_policy(chain):
    task_id, agent_id, agent_name, stages, launches = chain
    async with async_session_maker() as db:
        policy = await db.scalar(select(StagePolicy).where(StagePolicy.stage_id == stages[1]))
        policy.requires_orchestrator_move = True
        await db.commit()
    from agent_kanban_pm.services.tasks import TaskTransitionError
    with pytest.raises(TaskTransitionError, match='orchestrator'):
        await AssignmentLauncher().launch_for_assignment(task_id, agent_id, 'worker')
    assert not launches


@pytest.mark.asyncio
@pytest.mark.parametrize("reordered", [False, True])
async def test_destination_conditional_review_is_enforced(chain, reordered):
    from agent_kanban_pm.models import ReviewMode, Stage
    task_id, agent_id, agent_name, stages, launches = chain
    launcher = AssignmentLauncher()
    await finish(await launcher.launch_for_assignment(task_id, agent_id, 'worker'), agent_name)
    await finish(await launcher.launch_for_assignment(task_id, agent_id, 'test'), agent_name)
    async with async_session_maker() as db:
        policy = await db.scalar(select(StagePolicy).where(StagePolicy.stage_id == stages[3]))
        policy.review_mode = ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL
        if reordered:
            (await db.get(Stage, stages[3])).order = -1
        await db.commit()
    await finish(await launcher.launch_for_assignment(task_id, agent_id, 'diff_review'), agent_name)
    async with async_session_maker() as db:
        assert (await db.get(Task, task_id)).stage_id == stages[2]
