from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from typing import Optional
from datetime import UTC, datetime
from pathlib import Path
import asyncio
from pydantic import ValidationError
import logging
import re

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Project, Task, Entity, Stage, Comment, EntityType, TaskStatus, ApprovalStatus,
    TaskLog, ProjectWorkspace, Role, OrchestrationDecision, DecisionType,
    AgentSession, AgentSessionStatus,
)
from agent_kanban_pm.schemas import ProjectResponse, ChatPlanRequest, TaskCreate, TaskUpdate
from agent_kanban_pm.auth import get_current_entity, require_owner, require_manager, is_owner_or_manager, require_project_approval_for_mutation, require_task_access
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.runtime.task_transitions import coerce_task_status
from agent_kanban_pm.services.tasks import (
    TaskReferenceError,
    TaskTransitionError,
    create_task_record,
    update_task_record,
)
from agent_kanban_pm.runtime.default_stages import DEFAULT_STAGES
from agent_kanban_pm.runtime.stage_identity import normalize_stage_key, STAGE_STATUSES
from agent_kanban_pm.runtime.handoff_protocol import update_status_file
from agent_kanban_pm.runtime.instance import get_csrf_token
from agent_kanban_pm.runtime.paths import templates_dir

logger = logging.getLogger(__name__)

# Initialize templates
templates = Jinja2Templates(directory=str(templates_dir()))

# Every page embeds the per-instance CSRF token as a meta tag; the fetch
# wrapper in base.html forwards it as X-CSRF-Token on mutations. Derived from
# the auth token, so no per-session storage is needed.
templates.env.globals["current_year"] = lambda: datetime.now(UTC).year
templates.env.globals["kanban_csrf_token"] = get_csrf_token

# Human-readable labels for raw TaskStatus values ("in_progress" -> "In
# progress"). Keeps board badges readable without changing API contracts.
STATUS_LABELS = {
    "pending": "Pending",
    "in_progress": "In progress",
    "in_review": "In review",
    "completed": "Completed",
    "blocked": "Blocked",
}


def _status_label(value) -> str:
    key = getattr(value, "value", value)
    return STATUS_LABELS.get(str(key), str(key).replace("_", " "))


templates.env.filters["status_label"] = _status_label

router = APIRouter(include_in_schema=False)


def _role_to_entity_role(role_name: str) -> Role:
    return Role.MANAGER if role_name == "orchestrator" else Role.WORKER


async def _role_assignment_payload():
    import shutil
    from agent_kanban_pm.runtime.preferences import load_preferences
    from agent_kanban_pm.runtime.adapter_loader import load_all_adapters, discover_popular_clis

    prefs = load_preferences()
    assignments = prefs.get_role_assignments() if prefs else {}
    adapters = {a.name: a for a in load_all_adapters()}
    discovered = {c.command: c for c in discover_popular_clis()}

    roles = []
    for role_name, assignment in assignments.items():
        adapter = adapters.get(assignment.agent)
        command = adapter.invoke.command if adapter else (assignment.command or assignment.agent)
        adapter_models = [m.id for m in adapter.models] if adapter else []
        models = adapter_models or assignment.models
        roles.append({
            "role": role_name,
            "agent": assignment.agent,
            "display_name": adapter.display_name if adapter else (assignment.display_name or assignment.agent),
            "command": command,
            "mode": assignment.mode,
            "autonomy": assignment.autonomy,
            "model": assignment.model or (models[0] if models else "default"),
            "models": models,
            "source": "adapter" if adapter else "standalone",
            "installed": shutil.which(command) is not None,
        })

    candidates = []
    for adapter in adapters.values():
        candidates.append({
            "agent": adapter.name,
            "display_name": adapter.display_name,
            "command": adapter.invoke.command,
            "source": "adapter",
            "models": [m.id for m in adapter.models],
            "installed": shutil.which(adapter.invoke.command) is not None,
        })
    for cli in discovered.values():
        if cli.command not in {adapter.invoke.command for adapter in adapters.values()}:
            candidates.append({
                "agent": cli.command,
                "display_name": cli.display_name,
                "command": cli.command,
                "source": "standalone",
                "models": ["default"],
                "installed": cli.installed,
            })

    from agent_kanban_pm.runtime.preferences import AgentRole
    return {"roles": roles, "candidates": candidates,
            "role_names": list(dict.fromkeys([r.value for r in AgentRole] + list(assignments)))}


async def _ensure_role_entity(role_name: str, db: AsyncSession) -> Entity:
    from agent_kanban_pm.runtime.preferences import load_preferences
    from agent_kanban_pm.runtime.adapter_loader import load_all_adapters
    import shutil

    prefs = load_preferences()
    if not prefs:
        raise HTTPException(status_code=404, detail="No role assignments configured")
    assignment = prefs.get_role_assignments().get(role_name)
    if not assignment:
        raise HTTPException(status_code=404, detail=f"Role '{role_name}' is not assigned")

    adapters = {a.name: a for a in load_all_adapters()}
    adapter = adapters.get(assignment.agent)
    command = adapter.invoke.command if adapter else (assignment.command or assignment.agent)

    result = await db.execute(
        select(Entity).filter(Entity.name == assignment.agent, Entity.entity_type == EntityType.AGENT)
    )
    entity = result.scalar_one_or_none()
    skills = ", ".join(adapter.capabilities) if adapter else ", ".join(assignment.capabilities or [role_name])
    active = shutil.which(command) is not None
    if entity:
        entity.skills = skills
        entity.role = _role_to_entity_role(role_name)
        entity.is_active = active
        return entity

    entity = Entity(
        name=assignment.agent,
        entity_type=EntityType.AGENT,
        skills=skills,
        role=_role_to_entity_role(role_name),
        is_active=active,
    )
    db.add(entity)
    await db.flush()
    return entity


def _stage_name_matches(stage: Optional[Stage], *names: str) -> bool:
    if not stage or not stage.name:
        return False
    return stage.key in {normalize_stage_key(n) for n in names}


async def _notify_stage_policy_for_todo(
    task: Task,
    current_entity: Entity,
    db: AsyncSession,
) -> Optional[dict]:
    """Check stage policy for a card moved to To Do and return a policy hint.
    
    Does NOT auto-assign. Returns the policy expectations so the UI can
    display them. The orchestrator or human makes the actual assignment.
    """
    if not _stage_name_matches(task.stage, "to do", "todo"):
        return None

    try:
        from agent_kanban_pm.runtime.stage_policy import get_stage_policy_for_stage, policy_roles
    except ImportError:
        return None

    stage = task.stage
    if not stage:
        return None

    policy = await get_stage_policy_for_stage(db, task.project_id, stage.id)
    if not policy:
        return None

    roles = policy_roles(policy)
    if not roles:
        return None

    return {
        "stage_key": policy.stage_key,
        "expected_roles": roles,
        "required_outputs": policy_outputs_if_available(policy),
        "message": f"Stage '{policy.stage_key}' expects roles: {', '.join(roles)}. Orchestrator or human should assign.",
    }


def policy_outputs_if_available(policy) -> list[str]:
    try:
        from agent_kanban_pm.runtime.stage_policy import policy_outputs
        return policy_outputs(policy)
    except Exception as exc:
        logger.warning("Could not load policy_outputs: %s", exc)
        return []


def _plan_items_from_chat(text: str) -> list[dict]:
    raw_lines = [
        re.sub(r"^\s*[-*0-9.)\[\] ]+", "", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]
    if len(raw_lines) > 1:
        items = raw_lines[:8]
    else:
        base = raw_lines[0] if raw_lines else "Requested work"
        items = [
            f"Clarify scope for {base}",
            f"Implement {base}",
            f"Add tests or verification for {base}",
            f"Review and document {base}",
        ]
    return [
        {
            "title": item[:255],
            "description": f"Created from chat request:\n\n{text.strip()}",
            "priority": max(0, 10 - index),
        }
        for index, item in enumerate(items)
    ]


def _write_chat_plan_status(project: Project, request_text: str, created_tasks: list[Task]) -> Optional[str]:
    if not project.path:
        return None
    root = Path(project.path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return None
    try:
        path = update_status_file(root, {
            "state": "planned",
            "handoff_ready": True,
            "project_id": project.id,
            "task_id": None,
            "current_agent": "human",
            "assigned_role": "orchestrator",
            "summary": f"Chat request decomposed into {len(created_tasks)} backlog card(s).",
            "outputs": [f"task:{task.id} {task.title}" for task in created_tasks],
            "signals_to_next": (
                f"Original request:\n{request_text.strip()}\n\n"
                "Created backlog cards:\n"
                + "\n".join(f"- #{task.id}: {task.title}" for task in created_tasks)
            ),
            "blockers": "none",
        })
        return str(path)
    except OSError as exc:
        logger.warning("Could not write chat plan STATUS.md for %s: %s", root, exc)
        return None

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """Dashboard page"""
    projects_result = await db.execute(select(Project))
    visible_projects = [
        p for p in projects_result.scalars().all()
        if p.approval_status != ApprovalStatus.REJECTED
    ]
    total_tasks = await db.execute(select(func.count(Task.id)))
    completed_tasks = await db.execute(select(func.count(Task.id)).where(Task.status == TaskStatus.COMPLETED))
    role_payload = await _role_assignment_payload()

    stats = {
        "total_projects": len(visible_projects),
        "total_tasks": total_tasks.scalar(),
        "completed_tasks": completed_tasks.scalar(),
        "total_entities": len(role_payload["roles"])
    }

    recent_projects = sorted(visible_projects, key=lambda p: p.created_at, reverse=True)[:5]

    for project in recent_projects:
        task_count_result = await db.execute(
            select(func.count(Task.id)).where(Task.project_id == project.id)
        )
        project.task_count = task_count_result.scalar()

    result = await db.execute(
        select(Task).order_by(Task.created_at.desc()).limit(6)
    )
    recent_tasks = result.scalars().all()

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "stats": stats,
        "recent_projects": recent_projects,
        "recent_tasks": recent_tasks
    })


@router.get("/ui/projects", response_class=HTMLResponse)
async def ui_projects(
    request: Request,
    all: bool = False,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Projects list page"""
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.stages), selectinload(Project.tasks))
        .order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()
    if not all:
        projects = [
            p for p in projects
            if p.approval_status != ApprovalStatus.REJECTED
        ]

    agents_result = await db.execute(
        select(Entity).filter(Entity.entity_type == EntityType.AGENT, Entity.is_active == True)
    )
    agents = agents_result.scalars().all()

    return templates.TemplateResponse(request, "projects.html", {
        "request": request,
        "projects": projects,
        "agents": agents,
        "current_entity": current_entity,
    })


@router.get("/ui/projects/{project_id}/board", response_class=HTMLResponse)
async def project_kanban_board(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Kanban board for a project"""
    result = await db.execute(
        select(Project)
        .filter(Project.id == project_id)
        .options(
            selectinload(Project.stages).selectinload(Stage.tasks).selectinload(Task.assignees),
            selectinload(Project.stages).selectinload(Stage.tasks).selectinload(Task.subtasks),
            selectinload(Project.stages).selectinload(Stage.tasks).selectinload(Task.comments),
            selectinload(Project.creator)
        )
    )
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # A worker is ready only when the configured worker CLI is available.
    # Task stage alone cannot prove execution: use durable agent sessions.
    role_data = await _role_assignment_payload()
    worker_role = next((role for role in role_data["roles"] if role["role"] == "worker"), None)
    has_folder = bool(project.path and project.path.strip())
    has_worker = bool(worker_role and worker_role["installed"])

    all_tasks = [task for stage in project.stages for task in stage.tasks]
    session_result = await db.execute(
        select(AgentSession)
        .where(AgentSession.project_id == project_id, AgentSession.task_id.is_not(None))
        .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
    )
    sessions = session_result.scalars().all()
    has_started = any(
        session.status in (
            AgentSessionStatus.ACTIVE,
            AgentSessionStatus.IDLE,
            AgentSessionStatus.BLOCKED,
            AgentSessionStatus.DONE,
        )
        for session in sessions
    )
    execution_message = None
    if sessions:
        latest = sessions[0]
        if latest.status == AgentSessionStatus.ERROR:
            execution_message = f"Agent session failed for task #{latest.task_id}. Check Activity."
        elif latest.status == AgentSessionStatus.BLOCKED:
            execution_message = f"Agent session blocked on task #{latest.task_id}. Check Activity."
        elif latest.status == AgentSessionStatus.DONE:
            execution_message = f"Agent session finished for task #{latest.task_id}."
        elif latest.status == AgentSessionStatus.STARTING:
            execution_message = f"Agent starting task #{latest.task_id}."
        else:
            execution_message = f"Agent session active on task #{latest.task_id}."

    first_backlog_task_id = next(
        (task.id for stage in project.stages if stage.key == "backlog" for task in stage.tasks),
        None,
    )
    def has_worker_assignee(task: Task) -> bool:
        return bool(worker_role and any(
            assignee.entity_type == EntityType.AGENT and assignee.name == worker_role["agent"]
            for assignee in task.assignees
        ))

    first_todo_unassigned_task_id = next(
        (task.id for stage in project.stages if stage.key == "to_do"
         for task in stage.tasks if not has_worker_assignee(task)),
        None,
    )
    first_todo_assigned_task_id = next(
        (task.id for stage in project.stages if stage.key == "to_do"
         for task in stage.tasks if has_worker_assignee(task)),
        None,
    )
    completed_count = sum((has_folder, has_worker, bool(all_tasks), has_started))
    setup_checklist = {
        "has_folder": has_folder,
        "has_worker": has_worker,
        "worker_configured": worker_role is not None,
        "worker_agent": worker_role["agent"] if worker_role else "None",
        "worker_name": worker_role["display_name"] if worker_role else "Not configured",
        "has_tasks": bool(all_tasks),
        "task_count": len(all_tasks),
        "has_started": has_started,
        "execution_message": execution_message,
        "first_backlog_task_id": first_backlog_task_id,
        "todo_stage_id": next((stage.id for stage in project.stages if stage.key == "to_do"), None),
        "first_todo_unassigned_task_id": first_todo_unassigned_task_id,
        "first_todo_assigned_task_id": first_todo_assigned_task_id,
        "completed_count": completed_count,
        "all_complete": completed_count == 4,
    }

    return templates.TemplateResponse(request, "kanban_board.html", {
        "request": request,
        "project": project,
        "current_entity": current_entity,
        "stage_statuses": STAGE_STATUSES,
        "setup_checklist": setup_checklist,
        "active_page": "board",
    })


@router.get("/ui/projects/{project_id}/workbench", response_class=HTMLResponse)
async def project_workbench(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    result = await db.execute(
        select(Project).filter(Project.id == project_id).options(selectinload(Project.creator))
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse(request, "project_workbench.html", {
        "request": request, "project": project, "current_entity": current_entity,
        "active_page": "activity"
    })


@router.get("/ui/projects/{project_id}/git", response_class=HTMLResponse)
async def project_git(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    result = await db.execute(
        select(Project).filter(Project.id == project_id).options(selectinload(Project.creator))
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse(request, "project_git.html", {
        "request": request, "project": project, "current_entity": current_entity,
        "active_page": "changes"
    })


@router.get("/ui/projects/{project_id}/settings", response_class=HTMLResponse)
async def project_settings(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    result = await db.execute(
        select(Project)
        .filter(Project.id == project_id)
        .options(
            selectinload(Project.stages),
            selectinload(Project.creator)
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    role_data = await _role_assignment_payload()
    return templates.TemplateResponse(request, "project_settings.html", {
        "request": request,
        "project": project,
        "current_entity": current_entity,
        "role_data": role_data,
        "active_page": "settings",
    })


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
    move_summary = (body.get("summary") or "").strip() or "Manual move"

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

    await db.commit()

    logger.info(f"Task moved via UI: {task.title} by {current_entity.name}")

    await event_bus.publish(
        EventType.TASK_MOVED.value,
        {
            "task_id": task_id,
            "title": task.title,
            "from_stage_id": transition_state.old_stage_id,
            "to_stage_id": new_stage_id,
            "status": task.status.value,
            "summary": move_summary,
        },
        project_id=task.project_id,
        entity_id=current_entity.id
    )

    if _stage_name_matches(task.stage, "to do", "todo"):
        for assignee in task.assignees:
            await event_bus.publish(
                EventType.TASK_ASSIGNED.value,
                {
                    "task_id": task.id,
                    "entity_id": assignee.id,
                    "trigger": "manual_todo_move",
                },
                project_id=task.project_id,
                entity_id=current_entity.id,
            )

    if auto_policy_hint:
        await event_bus.publish(
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

    return {
        "ok": True,
        "task_id": task_id,
        "stage_id": new_stage_id,
        "status": task.status.value,
        "stage_policy_hint": auto_policy_hint,
        "transition_warning": transition_warning,
    }


@router.get("/ui/api/settings")
async def ui_get_settings():
    """Get current app settings. Manager-owned PM: reads from preferences.yaml."""
    from agent_kanban_pm.runtime.preferences import (
        load_preferences,
        get_manager_agent_name,
        get_manager_mode,
    )
    prefs = load_preferences()
    if not prefs:
        return {"manager": None, "mode": None, "workers": []}
    # A config may use only the new `roles:` shape, in which case the legacy
    # `manager`/`workers` blocks are absent. Derive from role assignments
    # instead of dereferencing prefs.manager unconditionally.
    manager = prefs.manager.agent if prefs.manager else get_manager_agent_name()
    mode = prefs.manager.mode if prefs.manager else get_manager_mode()
    workers = [w.agent for w in prefs.workers]
    if not workers:
        workers = sorted({
            a.agent
            for role, a in prefs.get_role_assignments().items()
            if role != "orchestrator"
        })
    return {"manager": manager, "mode": mode, "workers": workers}


@router.get("/ui/api/entities")
async def ui_list_entities(all: bool = False, db: AsyncSession = Depends(get_db)):
    """List entities for UI dropdowns.

    Default UI behavior is role-scoped. `all=true` is for debugging legacy DB
    rows and should not drive normal task assignment.
    """
    query = select(Entity).filter(Entity.is_active == True)
    if not all:
        from agent_kanban_pm.runtime.preferences import load_preferences
        prefs = load_preferences()
        role_names = set()
        if prefs:
            role_names = {a.agent for a in prefs.get_role_assignments().values()}
        if role_names:
            query = query.filter(Entity.name.in_(role_names))
    result = await db.execute(query.order_by(Entity.name))
    entities = result.scalars().all()
    return [{"id": e.id, "name": e.name, "entity_type": e.entity_type, "skills": e.skills} for e in entities]


@router.get("/ui/api/roles")
async def ui_get_roles():
    """Role assignments and available CLI candidates for the board UI."""
    return await _role_assignment_payload()


@router.post("/ui/api/roles/assign")
async def ui_assign_cli_to_role(
    request: Request,
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Assign an adapter or standalone CLI command to a role."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can configure role CLIs")

    import shutil
    from agent_kanban_pm.runtime.preferences import (
        Preferences, ManagerConfig, RoleConfig, RoleAssignment,
        AutonomyConfig, validate_role_name, load_preferences, save_preferences,
    )
    from agent_kanban_pm.runtime.adapter_loader import load_all_adapters

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Expected a settings object")
    role_name = body.get("role")
    agent = body.get("agent")
    command = body.get("command")
    try:
        validate_role_name(role_name)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid role") from exc
    if not isinstance(agent, str) or not agent.strip():
        raise HTTPException(status_code=422, detail="agent is required")
    for field in ("command", "model", "mode", "display_name", "protocol", "autonomy"):
        if body.get(field) is not None and not isinstance(body[field], str):
            raise HTTPException(status_code=422, detail=f"{field} must be a string")

    adapters = {a.name: a for a in load_all_adapters()}
    adapter = adapters.get(agent)
    prefs = load_preferences()
    previous = prefs.get_role_assignments().get(role_name) if prefs else None
    same_agent = previous is not None and previous.agent == agent
    if not adapter:
        command = command or (previous.command if same_agent else None) or agent
        if not shutil.which(command):
            raise HTTPException(status_code=422, detail=f"CLI '{command}' was not found on PATH")

    models = body.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    if not isinstance(models, list) or any(not isinstance(model, str) for model in models):
        raise HTTPException(status_code=422, detail="models must be a list of strings")
    adapter_models = [m.id for m in adapter.models] if adapter else []
    model = body.get("model") or (models[0] if models else (adapter_models[0] if adapter_models else None))
    if same_agent and "model" not in body:
        model = previous.model
    if adapter and model and adapter_models and model not in adapter_models and not (same_agent and model == previous.model):
        raise HTTPException(status_code=422, detail=f"Model must be one of: {', '.join(adapter_models)}")

    prefs = prefs or Preferences(
        manager=ManagerConfig(agent=agent, model=model or "default", mode="headless"),
        roles=RoleConfig(),
        autonomy=AutonomyConfig(),
    )
    if prefs.roles is None:
        prefs.roles = RoleConfig()

    autonomy = body.get("autonomy")
    if autonomy is not None and autonomy not in ("supervised", "auto"):
        raise HTTPException(status_code=422, detail="autonomy must be 'supervised' or 'auto'")
    if autonomy is None:
        # Re-assigning a role without an explicit autonomy keeps the previous
        # setting; new roles default to supervised.
        autonomy = previous.autonomy if previous else "supervised"

    # Patch the assignment: options not owned by this editor must survive.
    values = previous.model_dump() if previous else {}
    values.update(dict(
        agent=agent,
        mode=body.get("mode") or (previous.mode if previous else "headless"),
        model=model,
        models=models if "models" in body else (previous.models if same_agent else adapter_models),
        command=None if adapter else command,
        display_name=body.get("display_name") or (previous.display_name if same_agent else (adapter.display_name if adapter else agent)),
        protocol=body.get("protocol") or (previous.protocol if same_agent else (adapter.protocol if adapter else "stdio")),
        capabilities=previous.capabilities if same_agent else (adapter.capabilities if adapter else [role_name]),
        autonomy=autonomy,
    ))
    assignment = RoleAssignment.model_validate(values)
    prefs.set_role_assignment(role_name, assignment)
    if role_name == "orchestrator":
        prefs.manager = ManagerConfig(agent=agent, model=model or "default", mode=assignment.mode)
    save_preferences(prefs)
    return await _role_assignment_payload()


@router.get("/ui/api/folders")
async def ui_browse_folders(path: Optional[str] = None):
    """Browse local folders for project workspace selection."""
    requested = Path(path).expanduser() if path else Path.home()
    try:
        current = requested.resolve()
    except Exception:
        current = Path.home().resolve()

    if not current.exists() or not current.is_dir():
        current = Path.home().resolve()

    folders = []
    try:
        for child in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            if child.is_dir() and not child.name.startswith("."):
                folders.append({
                    "name": child.name,
                    "path": str(child),
                })
    except PermissionError:
        folders = []

    parent = current.parent if current.parent != current else None
    return {
        "path": str(current),
        "parent": str(parent) if parent else None,
        "home": str(Path.home().resolve()),
        "folders": folders,
    }


@router.post("/ui/api/open-workspace")
async def ui_open_workspace(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_owner),
):
    """Open a project's workspace folder using the OS native file manager.

    Safety: the requested path must match a known `Project.path` or a
    `ProjectWorkspace.root_path`. This stops the endpoint from acting as a
    generic "open arbitrary path" RCE.
    """
    import os
    import shutil
    import subprocess
    import sys

    body = await request.json()
    raw_path = (body.get("path") or "").strip()
    if not raw_path:
        raise HTTPException(status_code=422, detail="path is required")

    requested = Path(raw_path).expanduser()
    try:
        requested = requested.resolve()
    except Exception:
        raise HTTPException(status_code=422, detail="invalid path")

    # Whitelist against known project workspaces.
    proj_paths = (await db.execute(select(Project.path))).scalars().all()
    workspace_paths = (await db.execute(select(ProjectWorkspace.root_path))).scalars().all()
    known: set[str] = set()
    for raw in list(proj_paths) + list(workspace_paths):
        if not raw:
            continue
        try:
            known.add(str(Path(raw).expanduser().resolve()))
        except Exception:
            continue
    if str(requested) not in known:
        raise HTTPException(
            status_code=403,
            detail="Path is not registered as a project workspace",
        )

    if not requested.exists() or not requested.is_dir():
        raise HTTPException(status_code=404, detail="Workspace folder does not exist")

    if sys.platform == "darwin":
        opener = ["open", str(requested)]
    elif os.name == "nt":
        opener = ["explorer", str(requested)]
    else:
        if not shutil.which("xdg-open"):
            raise HTTPException(
                status_code=501,
                detail="xdg-open is not available on this host",
            )
        opener = ["xdg-open", str(requested)]

    await db.commit()
    try:
        await asyncio.to_thread(
            subprocess.Popen,
            opener,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to open folder: {exc}")

    return {"ok": True, "path": str(requested), "opener": opener[0]}


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

    log = TaskLog(task_id=task.id, message=f"Task created by {current_entity.name}", log_type="action")
    db.add(log)
    await db.commit()

    await event_bus.publish(
        EventType.TASK_CREATED.value,
        {"task_id": task.id, "title": task.title},
        project_id=task.project_id,
        entity_id=current_entity.id
    )

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


@router.post("/ui/tasks/chat-plan")
async def ui_create_chat_plan(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Turn a chat request into backlog cards and write the plan to STATUS.md.

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

    status_path = _write_chat_plan_status(project, message, created_tasks)
    rationale = (
        "Created backlog cards from chat request and wrote the plan "
        f"to {status_path or 'STATUS.md was unavailable'}."
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

    await db.commit()

    for task in created_tasks:
        await event_bus.publish(
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
    await event_bus.publish(
        EventType.CHAT_TASK_CREATED.value,
        {
            "project_id": project.id,
            "task_ids": [task.id for task in created_tasks],
            "status_path": status_path,
            "from_designer": from_designer,
        },
        project_id=project.id,
        entity_id=current_entity.id,
    )

    return {
        "project_id": project.id,
        "tasks": [
            {"id": t.id, "title": t.title, "priority": t.priority}
            for t in created_tasks
        ],
        "status_path": status_path,
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

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await require_task_access(task, current_entity, db, require_write=True)

    new_status = task_update.status
    try:
        await update_task_record(
            db,
            task,
            current_entity,
            changes=update_data,
            status=new_status if "status" in update_data else None,
            allow_human_policy_warning=True,
        )
    except TaskReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except TaskTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    await db.commit()

    await event_bus.publish(
        EventType.TASK_UPDATED.value,
        {
            "task_id": task.id,
            "title": task.title,
            "status": task.status,
            "stage_id": task.stage_id
        },
        project_id=task.project_id,
        entity_id=current_entity.id
    )

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
    await db.commit()

    await event_bus.publish(
        EventType.TASK_DELETED.value,
        {"task_id": task_id_to_delete},
        project_id=project_id,
        entity_id=current_entity.id
    )

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

    if action == "assign" and entity not in task.assignees:
        task.assignees.append(entity)
        task.version += 1
    elif action == "unassign" and entity in task.assignees:
        task.assignees.remove(entity)
        task.version += 1

    await db.commit()

    if action == "assign":
        await event_bus.publish(
            EventType.TASK_ASSIGNED.value,
            {"task_id": task.id, "entity_id": entity_id},
            project_id=task.project_id,
            entity_id=current_entity.id
        )
    else:
        await event_bus.publish(
            EventType.TASK_UNASSIGNED.value,
            {"task_id": task.id, "entity_id": entity_id},
            project_id=task.project_id,
            entity_id=current_entity.id
        )

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

    entity = await _ensure_role_entity(role_name, db)
    if not entity.is_active:
        raise HTTPException(status_code=422, detail=f"CLI for role '{role_name}' is not installed")

    if entity not in task.assignees:
        task.assignees.append(entity)
        task.version += 1

    await db.commit()

    await event_bus.publish(
        EventType.TASK_ASSIGNED.value,
        {"task_id": task.id, "entity_id": entity.id, "role": role_name},
        project_id=task.project_id,
        entity_id=current_entity.id
    )
    return {"ok": True, "entity_id": entity.id, "agent": entity.name, "role": role_name}


@router.patch("/ui/projects/{project_id}/edit")
async def ui_edit_project(
    project_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_manager),
):
    """Edit a project from the UI"""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()

    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Approval status changes require MANAGER+
    if "approval_status" in body and not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers/owners can change approval status")

    # Non-managers can only edit projects they created
    if not is_owner_or_manager(current_entity) and project.creator_id != current_entity.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit projects you created")

    for field in ["name", "description", "path", "approval_status", "is_demo"]:
        if field in body:
            setattr(project, field, body[field])

    project.updated_at = datetime.now(UTC)
    await db.commit()

    await event_bus.publish(
        EventType.PROJECT_UPDATED.value,
        {"project_id": project.id, "name": project.name},
        project_id=project.id,
        entity_id=current_entity.id
    )

    return {"ok": True}


@router.delete("/ui/projects/{project_id}")
async def ui_delete_project(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Entity = Depends(require_owner),
):
    """Delete a project from the UI. Only MANAGER+ can delete projects."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers/owners can delete projects")

    result = await db.execute(select(Project).filter(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_id_to_delete = project.id
    await db.delete(project)
    await db.commit()

    await event_bus.publish(
        EventType.PROJECT_DELETED.value,
        {"project_id": project_id_to_delete},
        project_id=project_id_to_delete,
        entity_id=current_entity.id
    )

    return {"ok": True}


@router.post("/ui/projects/create", response_model=ProjectResponse)
async def ui_create_project(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create a new project from the UI. Defaults to PENDING; MANAGER+ auto-approves."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    body = await request.json()
    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    path = body.get("path", "").strip() or None
    if not name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Project name is required")

    # MANAGER+ projects are auto-approved; others start as PENDING
    approval_status = ApprovalStatus.APPROVED if is_owner_or_manager(current_entity) else ApprovalStatus.PENDING

    project = Project(
        name=name,
        description=description,
        path=path,
        creator_id=current_entity.id,
        approval_status=approval_status
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    if project.path:
        db.add(ProjectWorkspace(
            project_id=project.id,
            root_path=project.path,
            label="Primary workspace",
            is_primary=True,
        ))

    for stage_data in DEFAULT_STAGES:
        stage = Stage(
            name=stage_data["name"],
            description=stage_data["description"],
            order=stage_data["order"],
            project_id=project.id
        )
        db.add(stage)
    await db.commit()

    await event_bus.publish(
        EventType.PROJECT_CREATED.value,
        {"project_id": project.id, "name": project.name},
        project_id=project.id,
        entity_id=current_entity.id
    )

    return project


@router.get("/ui/users", response_class=HTMLResponse)
async def ui_users(request: Request, db: AsyncSession = Depends(get_db)):
    """Show configured roles, CLI availability, and current agent work."""
    result = await db.execute(select(Entity).order_by(Entity.created_at.desc()))
    users = result.scalars().all()
    role_payload = await _role_assignment_payload()
    from agent_kanban_pm.runtime.preferences import get_manager_agent_name
    manager_agent = get_manager_agent_name()

    active_result = await db.execute(
        select(AgentSession)
        .where(
            AgentSession.ended_at.is_(None),
            AgentSession.status.in_([
                AgentSessionStatus.STARTING,
                AgentSessionStatus.ACTIVE,
                AgentSessionStatus.IDLE,
                AgentSessionStatus.BLOCKED,
            ]),
        )
        .options(
            selectinload(AgentSession.agent),
            selectinload(AgentSession.task),
            selectinload(AgentSession.project),
        )
        .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
    )
    current_work = {}
    for session in active_result.scalars():
        if session.agent.name not in current_work:
            current_work[session.agent.name] = {
                "task_id": session.task_id,
                "task_title": session.task.title if session.task else None,
                "project_id": session.project_id,
                "project_name": session.project.name if session.project else None,
                "status": session.status.value,
            }

    return templates.TemplateResponse(request, "users.html", {
        "request": request,
        "users": users,
        "role_payload": role_payload,
        "manager_agent": manager_agent,
        "current_work": current_work,
    })
