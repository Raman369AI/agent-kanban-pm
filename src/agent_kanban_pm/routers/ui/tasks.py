from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from typing import Optional
import json
from pydantic import ValidationError

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Project, Task, Entity, Stage, Comment, TaskStatus, TaskLog, OrchestrationDecision, DecisionType,
    AgentSession, AgentSessionStatus, LaunchRequest, OutboxEvent,
)
from agent_kanban_pm.schemas import ChatPlanRequest, TaskCreate, TaskUpdate
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager, require_project_approval_for_mutation, require_task_access
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.runtime.task_transitions import coerce_task_status, validate_task_transition
from agent_kanban_pm.services.tasks import (
    TaskReferenceError,
    TaskTransitionError,
    create_task_record,
    update_task_record,
)
from agent_kanban_pm.runtime.stage_identity import STAGE_STATUSES
from ._common import (
    _stage_name_matches,
    _notify_stage_policy_for_todo,
    _plan_items_from_chat,
    logger,
)
from . import roles

router = APIRouter(include_in_schema=False)


@router.get("/ui/tasks/{task_id}/completion-gates")
async def ui_task_completion_gates(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Explain the server-observed evidence required before a task reaches Done."""
    task = await db.scalar(select(Task).where(Task.id == task_id))
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if current_entity:
        await require_task_access(task, current_entity, db, require_write=False)
    from agent_kanban_pm.runtime.review_gate import completion_gate_status
    return await completion_gate_status(db, task)


@router.patch("/ui/tasks/{task_id}/move")
async def ui_move_task(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Move a task to a different stage (UI drag-and-drop)"""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()
    new_stage_id = body.get("stage_id")
    if new_stage_id is None:
        raise HTTPException(status_code=422, detail="stage_id is required")
    new_status = body.get("status")
    override_reason = (body.get("override_reason") or "").strip()
    move_summary = (
        (body.get("summary") or "").strip()
        or override_reason
        or "Manual move"
    )

    result = await db.execute(
        select(Task)
        .filter(Task.id == task_id)
        .options(selectinload(Task.assignees), selectinload(Task.stage))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        target_stage = await db.get(Stage, new_stage_id)
        inferred_status = STAGE_STATUSES.get(target_stage.key) if target_stage else None
        new_status = coerce_task_status(new_status or inferred_status) or task.status
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid task status: {exc}")

    await require_task_access(task, current_entity, db, require_write=True)
    old_stage_name = task.stage.name if task.stage else "Unknown"
    try:
        mutation = await update_task_record(
            db,
            task,
            current_entity,
            changes={},
            stage_id=new_stage_id,
            status=new_status,
            allow_human_policy_warning=True,
            override_reason=override_reason,
        )
    except TaskReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except TaskTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    transition_state = mutation.transition
    transition_warning = mutation.warning

    db.add(TaskLog(
        task_id=task.id,
        message=(
            f"Moved from {old_stage_name} to {task.stage.name if task.stage else new_stage_id} "
            f"by {current_entity.name}. Handoff summary: {move_summary}"
        ),
        log_type="handoff",
    ))
    db.add(Comment(
        task_id=task.id,
        author_id=current_entity.id,
        content=(
            f"Handoff summary after move to {task.stage.name if task.stage else new_stage_id}:\n"
            f"{move_summary}"
        ),
    ))
    auto_policy_hint = await _notify_stage_policy_for_todo(task, current_entity, db)

    if auto_policy_hint:
        event_bus.enqueue(db,
            EventType.STAGE_POLICY_CREATED.value,
            {
                "task_id": task_id,
                "stage_key": auto_policy_hint.get("stage_key"),
                "expected_roles": auto_policy_hint.get("expected_roles", []),
                "message": auto_policy_hint.get("message", ""),
            },
            project_id=task.project_id,
            entity_id=current_entity.id,
        )
    await db.commit()
    logger.info(f"Task moved via UI: {task.title} by {current_entity.name}")

    return {
        "ok": True,
        "task_id": task_id,
        "stage_id": new_stage_id,
        "status": task.status.value,
        "stage_policy_hint": auto_policy_hint,
        "transition_warning": transition_warning,
    }


@router.post("/ui/tasks/create")
async def ui_create_task(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create a task from the UI"""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()

    try:
        task_data = TaskCreate.model_validate(body)
        task_status = coerce_task_status(
            body.get("status", TaskStatus.PENDING)
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid task: {exc}")

    result = await db.execute(
        select(Project).filter(Project.id == task_data.project_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await require_project_approval_for_mutation(project, current_entity)

    if task_data.stage_id is None:
        raise HTTPException(status_code=422, detail="stage_id is required")
    try:
        task = await create_task_record(
            db,
            **task_data.model_dump(),
            status=task_status,
            created_by=current_entity.id,
        )
    except TaskReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await db.commit()
    await db.refresh(task, ["assignees"])

    return {
        "ok": True,
        "task": {
            "id": task.id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "priority": task.priority,
            "required_skills": task.required_skills,
            "assignees": []
        }
    }


def _render_acceptance(description: str, acceptance: list[str]) -> str:
    if not acceptance:
        return description
    checklist = "\n".join(f"- [ ] {a}" for a in acceptance if a and a.strip())
    if not checklist:
        return description
    body = (description or "").rstrip()
    return f"{body}\n\n**Acceptance:**\n{checklist}" if body else f"**Acceptance:**\n{checklist}"


def _render_dependencies(description: str, dep_task_ids: list[int]) -> str:
    if not dep_task_ids:
        return description
    refs = ", ".join(f"#{tid}" for tid in dep_task_ids)
    body = (description or "").rstrip()
    return f"{body}\n\nDepends on: {refs}" if body else f"Depends on: {refs}"


@router.post("/ui/tasks/chat-plan/preview")
async def ui_preview_chat_plan(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Prepare task proposals without creating tasks or writing workspace files."""
    if not current_entity:
        raise HTTPException(status_code=401, detail="Authentication required")
    try:
        chat_req = ChatPlanRequest(**(await request.json()))
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid chat plan request: {exc}")
    message = (chat_req.message or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")
    project = (await db.execute(select(Project).where(
        Project.id == chat_req.project_id
    ))).scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_project_approval_for_mutation(project, current_entity)
    return {"items": _plan_items_from_chat(message)}


@router.post("/ui/tasks/chat-plan")
async def ui_create_chat_plan(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Turn a chat request into durable backlog cards and a planning decision.

    Two modes:
      * Regex fallback (browser chat bar) — body = {project_id, message}.
      * LLM-decomposed plan (CLI chat designer) — body also includes
        items[] (pre-decomposed) and an optional transcript.
    """
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()
    try:
        chat_req = ChatPlanRequest(**body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid chat plan request: {exc}")

    project_id = chat_req.project_id
    message = (chat_req.message or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="message is required")

    result = await db.execute(
        select(Project)
        .filter(Project.id == project_id)
        .options(selectinload(Project.stages))
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await require_project_approval_for_mutation(project, current_entity)

    backlog_stage = next((stage for stage in project.stages if _stage_name_matches(stage, "backlog")), None)
    if not backlog_stage:
        backlog_stage = min(project.stages, key=lambda stage: stage.order, default=None)
    if not backlog_stage:
        raise HTTPException(status_code=422, detail="Project has no stages")

    if chat_req.items:
        if len(chat_req.items) > 20:
            raise HTTPException(status_code=422, detail="chat plan exceeds 20 items")
        plan_items = [
            {
                "title": item.title,
                "description": _render_acceptance(item.description, item.acceptance),
                "priority": item.priority,
                "role_hint": item.role_hint,
                "depends_on": list(item.depends_on or []),
            }
            for item in chat_req.items
        ]
        from_designer = True
    else:
        plan_items = _plan_items_from_chat(message)
        from_designer = False

    created_tasks: list[Task] = []
    for idx, item in enumerate(plan_items):
        task = Task(
            title=item["title"],
            description=item.get("description", ""),
            project_id=project.id,
            stage_id=backlog_stage.id,
            priority=item.get("priority", 5),
            status=TaskStatus.PENDING,
            created_by=current_entity.id,
            sequence_order=idx + 1,
        )
        db.add(task)
        created_tasks.append(task)
    await db.flush()

    if from_designer:
        for idx, (item, task) in enumerate(zip(plan_items, created_tasks)):
            dep_indices = item.get("depends_on") or []
            dep_task_ids = [
                created_tasks[d].id
                for d in dep_indices
                if isinstance(d, int) and 0 <= d < len(created_tasks) and d != idx
            ]
            if dep_task_ids:
                task.description = _render_dependencies(task.description, dep_task_ids)

    for task in created_tasks:
        db.add(TaskLog(
            task_id=task.id,
            message=f"Created from chat plan by {current_entity.name}"
                     + (" (designer)" if from_designer else ""),
            log_type="action",
        ))

    rationale = (
        f"Created {len(created_tasks)} backlog cards from chat request: "
        + ", ".join(f"#{task.id} {task.title}" for task in created_tasks)
        + f"\n\nOriginal request:\n{message}"
    )
    if chat_req.transcript:
        rationale = f"{rationale}\n\n--- transcript ---\n{chat_req.transcript[:8000]}"
    db.add(OrchestrationDecision(
        project_id=project.id,
        manager_agent_id=current_entity.id,
        decision_type=DecisionType.TASK_SPLIT,
        input_summary=message[:1000],
        rationale=rationale,
        affected_task_ids=",".join(str(task.id) for task in created_tasks),
    ))

    for task in created_tasks:
        event_bus.enqueue(db,
            EventType.TASK_CREATED.value,
            {
                "task_id": task.id,
                "title": task.title,
                "from_chat_plan": True,
                "from_designer": from_designer,
            },
            project_id=project.id,
            entity_id=current_entity.id,
        )
    event_bus.enqueue(db,
        EventType.CHAT_TASK_CREATED.value,
        {
            "project_id": project.id,
            "task_ids": [task.id for task in created_tasks],
            "status_path": None,
            "from_designer": from_designer,
        },
        project_id=project.id,
        entity_id=current_entity.id,
    )
    await db.commit()

    return {
        "project_id": project.id,
        "tasks": [
            {"id": t.id, "title": t.title, "priority": t.priority}
            for t in created_tasks
        ],
        "status_path": None,
        "from_designer": from_designer,
    }

@router.patch("/ui/tasks/{task_id}/edit")
async def ui_edit_task(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Edit a task from the UI"""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()
    try:
        task_update = TaskUpdate.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid task update: {exc}")
    update_data = task_update.model_dump(exclude_unset=True)
    expected_version = update_data.pop("version", None)
    override_reason = update_data.pop("override_reason", None)

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await require_task_access(task, current_entity, db, require_write=True)
    if expected_version is not None and expected_version != task.version:
        raise HTTPException(
            status_code=409,
            detail="This task changed while you were editing. Review the latest version before saving.",
        )

    new_status = task_update.status
    try:
        await update_task_record(
            db,
            task,
            current_entity,
            changes=update_data,
            status=new_status if "status" in update_data else None,
            allow_human_policy_warning=True,
            override_reason=override_reason,
        )
    except TaskReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except TaskTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.commit()

    return {"ok": True}


@router.delete("/ui/tasks/{task_id}")
async def ui_delete_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Delete a task from the UI"""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await require_task_access(task, current_entity, db, require_write=True)

    task_id_to_delete = task.id
    project_id = task.project_id
    await db.delete(task)
    event_bus.enqueue(db,
        EventType.TASK_DELETED.value,
        {"task_id": task_id_to_delete},
        project_id=project_id,
        entity_id=current_entity.id
    )
    await db.commit()

    return {"ok": True}


@router.post("/ui/tasks/{task_id}/assign")
async def ui_assign_task(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Assign/unassign entities to a task from the UI"""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()
    entity_id = body["entity_id"]
    action = body.get("action", "assign")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Enforce project approval
    project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
    project = project_result.scalar_one_or_none()
    if project:
        await require_project_approval_for_mutation(project, current_entity)

    # RBAC: non-managers can only self-assign/unassign
    if not is_owner_or_manager(current_entity) and entity_id != current_entity.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only assign/unassign yourself")

    entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
    entity = entity_result.scalar_one_or_none()
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")

    if action not in {"assign", "unassign"}:
        raise HTTPException(status_code=422, detail="action must be assign or unassign")
    from agent_kanban_pm.services.tasks import assign_task_record
    await assign_task_record(db, task, current_entity, entity, unassign=action == "unassign")
    await db.commit()

    return {"ok": True}


@router.post("/ui/tasks/{task_id}/assign-role")
async def ui_assign_task_role(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Assign a task directly to a configured role's CLI agent."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can assign roles")

    body = await request.json()
    role_name = body.get("role")
    override_reason = (body.get("override_reason") or "").strip()
    if not role_name:
        raise HTTPException(status_code=422, detail="role is required")

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
    project = project_result.scalar_one_or_none()
    if project:
        await require_project_approval_for_mutation(project, current_entity)

    entity = await roles._ensure_role_entity(role_name, db)
    if not entity.is_active:
        raise HTTPException(status_code=422, detail=f"CLI for role '{role_name}' is not installed")

    current_stage = await db.get(Stage, task.stage_id) if task.stage_id else None
    if current_stage and current_stage.key == "to_do":
        execution_stage = await db.scalar(
            select(Stage).where(
                Stage.project_id == task.project_id,
                Stage.workflow_key == "in_progress",
            ).limit(1)
        )
        if execution_stage:
            warning = await validate_task_transition(
                db,
                task,
                current_entity,
                new_stage_id=execution_stage.id,
                new_status=TaskStatus.IN_PROGRESS,
            )
            if warning and len(override_reason) < 3:
                raise HTTPException(
                    status_code=409,
                    detail=f"{warning}. A human override reason is required to start work",
                )

    from agent_kanban_pm.services.tasks import assign_task_record
    await assign_task_record(
        db,
        task,
        current_entity,
        entity,
        role=role_name,
        override_reason=override_reason,
    )
    await db.commit()
    return {"ok": True, "entity_id": entity.id, "agent": entity.name, "role": role_name}


@router.post("/ui/tasks/{task_id}/request-git-pr")
async def ui_request_git_pr(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Request one durable Git/PR handoff for the current implementation."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can request Git/PR work")

    task = await db.scalar(
        select(Task).where(Task.id == task_id).options(selectinload(Task.assignees))
    )
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await db.get(Project, task.project_id)
    if project:
        await require_project_approval_for_mutation(project, current_entity)
    stage = await db.get(Stage, task.stage_id) if task.stage_id else None
    if stage is None or stage.key != "review":
        raise HTTPException(status_code=409, detail="Git/PR work can only be requested from Review")

    from agent_kanban_pm.runtime.review_gate import implementation_for_task
    source = await implementation_for_task(db, task.id)
    if source is None or not source.work_revision:
        raise HTTPException(status_code=409, detail="A committed implementation handoff is required")

    entity = await roles._ensure_role_entity("git_pr", db)
    if not entity.is_active:
        raise HTTPException(status_code=422, detail="CLI for role 'git_pr' is not installed")

    current_session = await db.scalar(
        select(AgentSession).where(
            AgentSession.task_id == task.id,
            AgentSession.assigned_role == "git_pr",
            AgentSession.source_session_id == source.id,
            AgentSession.work_revision == source.work_revision,
        ).order_by(AgentSession.id.desc()).limit(1)
    )
    if current_session is not None and (
        current_session.ended_at is None or current_session.status == AgentSessionStatus.DONE
    ):
        return {
            "ok": True,
            "created": False,
            "state": "completed" if current_session.status == AgentSessionStatus.DONE else "active",
            "session_id": current_session.id,
            "source_session_id": source.id,
            "work_revision": source.work_revision,
        }

    open_statuses = ("queued", "blocked", "reserved", "starting", "started")

    async def current_request():
        return await db.scalar(
            select(LaunchRequest).where(
                LaunchRequest.task_id == task.id,
                LaunchRequest.role == "git_pr",
                LaunchRequest.stage_id == stage.id,
                LaunchRequest.source_session_id == source.id,
                LaunchRequest.archived_at.is_(None),
                LaunchRequest.status.in_(open_statuses),
            ).order_by(LaunchRequest.id.desc()).limit(1)
        )

    existing = await current_request()
    if existing is not None:
        return {
            "ok": True,
            "created": False,
            "state": existing.status,
            "request_id": existing.id,
            "session_id": existing.session_id,
            "source_session_id": source.id,
            "work_revision": source.work_revision,
        }

    # Entering Review may already have committed an automatic git_pr event,
    # while the scheduler has not converted it into a LaunchRequest yet.
    # Recognize that durable intent instead of creating a second request.
    pending_events = (await db.execute(
        select(OutboxEvent).where(OutboxEvent.delivered_at.is_(None))
        .order_by(OutboxEvent.id.desc()).limit(500)
    )).scalars()
    for pending in pending_events:
        try:
            payload = json.loads(pending.payload)
        except (TypeError, ValueError):
            continue
        data = payload.get("data") or {}
        if (
            payload.get("event_type") == EventType.TASK_ASSIGNED.value
            and data.get("task_id") == task.id
            and data.get("entity_id") == entity.id
            and data.get("role") == "git_pr"
            and data.get("stage_id") == stage.id
            and data.get("source_session_id") == source.id
        ):
            return {
                "ok": True,
                "created": False,
                "state": "policy_queued",
                "event_id": pending.id,
                "source_session_id": source.id,
                "work_revision": source.work_revision,
            }

    # Close the narrow handoff race where the outbox dispatcher committed the
    # LaunchRequest after the first lookup but before marking its event sent.
    existing = await current_request()
    if existing is not None:
        return {
            "ok": True,
            "created": False,
            "state": existing.status,
            "request_id": existing.id,
            "session_id": existing.session_id,
            "source_session_id": source.id,
            "work_revision": source.work_revision,
        }

    if entity not in task.assignees:
        task.assignees.append(entity)
    task.version += 1
    db.add(TaskLog(
        task_id=task.id,
        log_type="action",
        message=f"Requested Git/PR handoff from {entity.name} by {current_entity.name}",
    ))
    event = event_bus.enqueue(
        db,
        EventType.TASK_ASSIGNED.value,
        {
            "task_id": task.id,
            "entity_id": entity.id,
            "role": "git_pr",
            "stage_id": stage.id,
            "source_session_id": source.id,
        },
        project_id=task.project_id,
        entity_id=current_entity.id,
    )
    await db.flush()
    request = LaunchRequest(
        event_id=event.id,
        actor_id=current_entity.id,
        task_id=task.id,
        agent_id=entity.id,
        role="git_pr",
        source_session_id=source.id,
        stage_id=stage.id,
        status="queued",
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)
    return {
        "ok": True,
        "created": True,
        "state": request.status,
        "request_id": request.id,
        "source_session_id": source.id,
        "work_revision": source.work_revision,
    }
