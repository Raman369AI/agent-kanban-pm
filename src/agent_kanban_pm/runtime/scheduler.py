"""One server consumes durable launch requests; MCP only writes intent."""
import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
import uuid
from sqlalchemy import select, or_, update
from sqlalchemy.exc import IntegrityError
from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import LaunchRequest, Task, Entity, AgentSession, task_assignments

logger = logging.getLogger(__name__)


async def request_launch(payload):
    data = payload['data']
    async with async_session_maker() as db:
        if await db.scalar(select(LaunchRequest.id).where(LaunchRequest.event_id == payload['event_id'])):
            return
        task = await db.get(Task, data['task_id'])
        agent = await db.get(Entity, data['entity_id'])
        if not task or not agent:
            return
        db.add(LaunchRequest(event_id=payload['event_id'], task_id=task.id, agent_id=agent.id,
                             actor_id=payload.get('entity_id'),
                             role=data.get('role'), stage_id=data.get('stage_id', task.stage_id),
                             source_session_id=data.get('source_session_id'),
                             override_reason=data.get('override_reason')))
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()


async def _claim_launches():
    now = datetime.now(UTC)
    stale = now - timedelta(seconds=60)
    token = uuid.uuid4().hex
    async with async_session_maker() as db:
        ids = list((await db.execute(select(LaunchRequest.id).where(
            LaunchRequest.status.in_(['queued', 'blocked', 'reserved', 'starting']),
            or_(LaunchRequest.retry_at.is_(None), LaunchRequest.retry_at <= now),
            or_(LaunchRequest.claimed_at.is_(None), LaunchRequest.claimed_at < stale),
        ).order_by(LaunchRequest.id).limit(20))).scalars())
        claimed = []
        for request_id in ids:
            result = await db.execute(update(LaunchRequest).where(
                LaunchRequest.id == request_id,
                LaunchRequest.status.in_(['queued', 'blocked', 'reserved', 'starting']),
                or_(LaunchRequest.claimed_at.is_(None), LaunchRequest.claimed_at < stale),
            ).values(claimed_at=now, claim_token=token))
            if result.rowcount == 1:
                claimed.append(request_id)
        await db.commit()
    return claimed, token


async def _renew_launch_claim(request_ids: list[int], token: str):
    while True:
        await asyncio.sleep(20)
        async with async_session_maker() as db:
            await db.execute(update(LaunchRequest).where(
                LaunchRequest.id.in_(request_ids),
                LaunchRequest.claim_token == token,
            ).values(claimed_at=datetime.now(UTC)))
            await db.commit()


async def dispatch_launches(launcher):
    request_ids, token = await _claim_launches()
    for request_id in request_ids:
        async with async_session_maker() as db:
            request = await db.get(LaunchRequest, request_id)
            if (request is None or request.claim_token != token
                    or request.status not in {'queued', 'blocked', 'reserved', 'starting'}):
                continue
        async with async_session_maker() as db:
            task = await db.get(Task, request.task_id)
            assigned = await db.scalar(select(task_assignments.c.entity_id).where(
                task_assignments.c.task_id == request.task_id,
                task_assignments.c.entity_id == request.agent_id))
            existing = await db.scalar(select(AgentSession).where(AgentSession.launch_request_id == request.id))
            if not existing and (not task or not assigned or task.stage_id != request.stage_id):
                row = await db.get(LaunchRequest, request.id)
                if row:
                    row.status = 'cancelled'
                    row.last_error = 'Assignment removed or stage changed'
                    row.claimed_at = None
                    row.claim_token = None
                    await db.commit()
                continue
        error = None
        renewal = asyncio.create_task(_renew_launch_claim(request_ids, token))
        try:
            session_id = await launcher.recover_launch(existing.id) if existing else await launcher.launch_for_assignment(
                request.task_id, request.agent_id, request.role,
                launch_request_id=request.id)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                async with async_session_maker() as db:
                    row = await db.get(LaunchRequest, request.id)
                    if row and row.claim_token == token:
                        row.claimed_at = None
                        row.claim_token = None
                        await db.commit()
                raise
            session_id = None
            error = str(exc)
            logger.warning('Launch request %s blocked: %s', request.id, error)
        finally:
            renewal.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
        async with async_session_maker() as db:
            row = await db.get(LaunchRequest, request.id)
            if not row or row.claim_token != token:
                continue
            row.attempts += 1
            # Recovery failures must not detach an already-reserved session.
            # Queue controls use this link to cancel or retry the reservation
            # without leaving an open STARTING session behind.
            if session_id is not None:
                row.session_id = session_id
            row.status = 'started' if session_id else ('blocked' if error else 'queued')
            if session_id:
                session = await db.get(AgentSession, session_id)
                if session and session.ended_at:
                    row.status = 'completed' if session.status.value == 'done' else 'failed'
                    if row.status == 'failed':
                        error = f'Session #{session_id} ended with an error; inspect its recorded outcome'
            row.last_error = error or (None if session_id else 'Waiting for capacity or runnable assignment')
            row.claimed_at = None
            row.claim_token = None
            row.retry_at = datetime.now(UTC) + timedelta(seconds=30 if error else 2)
            await db.commit()


async def scheduler_loop(launcher):
    while True:
        try:
            await dispatch_launches(launcher)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Scheduler reconciliation failed')
        await asyncio.sleep(1)


async def recover_legacy_assignments(workspace_path):
    """Reconcile runnable assignments with durable launch intent.

    An outbox row alone is not proof that the scheduler subscriber handled the
    assignment. Reuse a matching event when it has no LaunchRequest so a
    shutdown between commit and delivery cannot lose work or create a second
    intent when the original event is later drained.
    """
    import json
    from agent_kanban_pm.models import OutboxEvent, Project, Stage, TaskStatus, ApprovalStatus
    from agent_kanban_pm.events import event_bus, EventType
    async with async_session_maker() as db:
        launches = list((await db.execute(select(LaunchRequest))).scalars())
        known = {(row.task_id, row.agent_id, row.stage_id) for row in launches}
        used_event_ids = {row.event_id for row in launches}
        assignment_events = {}
        for event_id, payload in (await db.execute(
            select(OutboxEvent.id, OutboxEvent.payload).order_by(OutboxEvent.id.desc())
        )).all():
            event = json.loads(payload)
            data = event.get('data', {})
            if event.get('event_type') != EventType.TASK_ASSIGNED.value or event_id in used_event_ids:
                continue
            key = (data.get('task_id'), data.get('entity_id'), data.get('stage_id'))
            assignment_events.setdefault(key, (event_id, event, data))
        rows = (await db.execute(select(Task, task_assignments.c.entity_id).join(
            task_assignments, Task.id == task_assignments.c.task_id
        ).join(Project, Project.id == Task.project_id).join(Stage, Stage.id == Task.stage_id).where(
            Project.path == workspace_path,
            Project.approval_status == ApprovalStatus.APPROVED,
            Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]),
            Stage.workflow_key.in_(['to_do', 'in_progress']),
        ))).all()
        for task, agent_id in rows:
            key = (task.id, agent_id, task.stage_id)
            if key in known:
                continue
            active = await db.scalar(select(AgentSession.id).where(
                AgentSession.task_id == task.id, AgentSession.agent_id == agent_id,
                AgentSession.ended_at.is_(None),
            ).limit(1))
            if active:
                continue
            existing_event = assignment_events.get(key)
            if existing_event is None:
                # Older events may predate stage_id in their payload. They are
                # still reusable when task and assignee identify one intent.
                existing_event = next((value for event_key, value in assignment_events.items()
                                       if event_key[:2] == key[:2]), None)
            if existing_event:
                event_id, event, data = existing_event
                db.add(LaunchRequest(
                    event_id=event_id,
                    actor_id=event.get('entity_id'),
                    task_id=task.id,
                    agent_id=agent_id,
                    role=data.get('role'),
                    stage_id=task.stage_id,
                    source_session_id=data.get('source_session_id'),
                    override_reason=data.get('override_reason'),
                ))
            else:
                event_bus.enqueue(db, EventType.TASK_ASSIGNED.value,
                                  {'task_id': task.id, 'entity_id': agent_id, 'stage_id': task.stage_id},
                                  project_id=task.project_id)
            known.add(key)
        await db.commit()
