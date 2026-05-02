from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import UTC, datetime
from pathlib import Path
import logging
import re

from database import get_db
from models import (
    Project, Task, Entity, Stage, Comment, EntityType, TaskStatus, ApprovalStatus,
    TaskLog, ProjectWorkspace, Role, OrchestrationDecision, DecisionType
)
from schemas import ProjectResponse, ChatPlanRequest
from auth import get_current_entity, require_owner, require_manager, is_owner_or_manager, require_project_approval_for_mutation, require_task_access
from event_bus import event_bus, EventType
from kanban_runtime.handoff_protocol import update_status_file
from kanban_runtime.paths import templates_dir

logger = logging.getLogger(__name__)

# Initialize templates
templates = Jinja2Templates(directory=str(templates_dir()))

router = APIRouter(include_in_schema=False)


def _is_noisy_project(project: Project) -> bool:
    return False


def _role_to_entity_role(role_name: str) -> Role:
    return Role.MANAGER if role_name == "orchestrator" else Role.WORKER


async def _role_assignment_payload():
    from kanban_runtime.preferences import load_preferences
    from kanban_runtime.adapter_loader import load_all_adapters, discover_popular_clis

    prefs = load_preferences()
    assignments = prefs.get_role_assignments() if prefs else {}
    adapters = {a.name: a for a in load_all_adapters()}
    discovered = {c.command: c for c in discover_popular_clis()}

    roles = []
    for role_name, assignment in assignments.items():
        adapter = adapters.get(assignment.agent)
        command = adapter.invoke.command if adapter else (assignment.command or assignment.agent)
        adapter_models = [m.id for m in adapter.models] if adapter else []
        models = assignment.models or adapter_models
        roles.append({
            "role": role_name,
            "agent": assignment.agent,
            "display_name": adapter.display_name if adapter else (assignment.display_name or assignment.agent),
            "command": command,
            "mode": assignment.mode,
            "model": assignment.model or (models[0] if models else "default"),
            "models": models,
            "source": "adapter" if adapter else "standalone",
            "installed": command in discovered and discovered[command].installed or bool(adapter and __import__("shutil").which(command)),
        })

    candidates = []
    for adapter in adapters.values():
        candidates.append({
            "agent": adapter.name,
            "display_name": adapter.display_name,
            "command": adapter.invoke.command,
            "source": "adapter",
            "models": [m.id for m in adapter.models],
            "installed": __import__("shutil").which(adapter.invoke.command) is not None,
        })
    for cli in discovered.values():
        if cli.command not in adapters:
            candidates.append({
                "agent": cli.command,
                "display_name": cli.display_name,
                "command": cli.command,
                "source": "standalone",
                "models": ["default"],
                "installed": cli.installed,
            })

    return {"roles": roles, "candidates": candidates}


async def _ensure_role_entity(role_name: str, db: AsyncSession) -> Entity:
    from kanban_runtime.preferences import load_preferences
    from kanban_runtime.adapter_loader import load_all_adapters
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
    normalized = re.sub(r"[^a-z0-9]+", " ", stage.name.lower()).strip()
    return normalized in {re.sub(r"[^a-z0-9]+", " ", n.lower()).strip() for n in names}


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
        from kanban_runtime.stage_policy import get_stage_policy_for_stage, policy_roles, normalize_stage_key
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
        from kanban_runtime.stage_policy import policy_outputs
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
        if p.approval_status != ApprovalStatus.REJECTED and not _is_noisy_project(p)
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

    return templates.TemplateResponse("dashboard.html", {
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
            if p.approval_status != ApprovalStatus.REJECTED and not _is_noisy_project(p)
        ]

    agents_result = await db.execute(
        select(Entity).filter(Entity.entity_type == EntityType.AGENT, Entity.is_active == True)
    )
    agents = agents_result.scalars().all()

    return templates.TemplateResponse("projects.html", {
        "request": request,
        "projects": projects,
        "agents": agents,
        "current_entity": current_entity
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

    return templates.TemplateResponse("kanban_board.html", {
        "request": request,
        "project": project,
        "current_entity": current_entity
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
    return templates.TemplateResponse("project_workbench.html", {
        "request": request, "project": project, "current_entity": current_entity
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
    return templates.TemplateResponse("project_git.html", {
        "request": request, "project": project, "current_entity": current_entity
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
    new_status = body.get("status", "pending")
    move_summary = (body.get("summary") or "").strip() or "Manual move"

    result = await db.execute(
        select(Task)
        .filter(Task.id == task_id)
        .options(selectinload(Task.assignees), selectinload(Task.stage))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await require_task_access(task, current_entity, db, require_write=True)

    # Sequence order enforcement
    if new_status == "in_progress" and task.status != "in_progress":
        from routers.tasks import _check_predecessor
        err = await _check_predecessor(task, db)
        if err:
            raise HTTPException(status_code=409, detail=err)

    from kanban_runtime.stage_policy import get_stage_policy_for_stage, validate_transition, gather_transition_context
    transition_warning = None
    if task.stage_id and new_stage_id and task.stage_id != new_stage_id:
        from_policy = await get_stage_policy_for_stage(db, task.project_id, task.stage_id)
        to_policy = await get_stage_policy_for_stage(db, task.project_id, new_stage_id)
        move_initiator = "human" if current_entity.entity_type == EntityType.HUMAN else current_entity.name
        ctx = await gather_transition_context(db, task_id, task.project_id)
        transition_warning = validate_transition(
            from_policy=from_policy,
            to_policy=to_policy,
            move_initiator=move_initiator,
            has_diff_review=ctx["has_diff_review"],
            has_required_outputs=True,
            is_critical=ctx["is_critical"],
        )
        if transition_warning and current_entity.entity_type != EntityType.HUMAN:
            raise HTTPException(status_code=409, detail=transition_warning)

    old_stage_id = task.stage_id
    old_stage_name = task.stage.name if task.stage else "Unknown"
    task.stage_id = new_stage_id
    task.status = new_status
    new_stage_result = await db.execute(select(Stage).filter(Stage.id == new_stage_id))
    task.stage = new_stage_result.scalar_one_or_none()
    if new_status == "completed" and task.completed_at is None:
        task.completed_at = datetime.now(UTC)
    task.version += 1
    task.updated_at = datetime.now(UTC)

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
            "from_stage_id": old_stage_id,
            "to_stage_id": new_stage_id,
            "status": new_status,
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
        "status": new_status,
        "stage_policy_hint": auto_policy_hint,
        "transition_warning": transition_warning,
    }


@router.get("/ui/api/settings")
async def ui_get_settings():
    """Get current app settings. Manager-owned PM: reads from preferences.yaml."""
    from kanban_runtime.preferences import load_preferences
    prefs = load_preferences()
    if prefs:
        return {
            "manager": prefs.manager.agent,
            "mode": prefs.manager.mode,
            "workers": [w.agent for w in prefs.workers]
        }
    return {"manager": None, "mode": None, "workers": []}


@router.get("/ui/api/entities")
async def ui_list_entities(all: bool = False, db: AsyncSession = Depends(get_db)):
    """List entities for UI dropdowns.

    Default UI behavior is role-scoped. `all=true` is for debugging legacy DB
    rows and should not drive normal task assignment.
    """
    query = select(Entity).filter(Entity.is_active == True)
    if not all:
        from kanban_runtime.preferences import load_preferences
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
    from kanban_runtime.preferences import (
        Preferences, ManagerConfig, RoleConfig, RoleAssignment,
        AutonomyConfig, AgentRole, load_preferences, save_preferences,
    )
    from kanban_runtime.adapter_loader import load_all_adapters

    body = await request.json()
    role_name = body.get("role")
    agent = body.get("agent")
    command = body.get("command")
    if role_name not in [r.value for r in AgentRole]:
        raise HTTPException(status_code=422, detail="Invalid role")
    if not agent:
        raise HTTPException(status_code=422, detail="agent is required")

    adapters = {a.name: a for a in load_all_adapters()}
    adapter = adapters.get(agent)
    if not adapter:
        command = command or agent
        if not shutil.which(command):
            raise HTTPException(status_code=422, detail=f"CLI '{command}' was not found on PATH")

    models = body.get("models") or []
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    adapter_models = [m.id for m in adapter.models] if adapter else []
    model = body.get("model") or (models[0] if models else (adapter_models[0] if adapter_models else None))
    if adapter and model and adapter_models and model not in adapter_models:
        raise HTTPException(status_code=422, detail=f"Model must be one of: {', '.join(adapter_models)}")

    prefs = load_preferences() or Preferences(
        manager=ManagerConfig(agent=agent, model=model or "default", mode="headless"),
        roles=RoleConfig(),
        autonomy=AutonomyConfig(),
    )
    if prefs.roles is None:
        prefs.roles = RoleConfig()

    assignment = RoleAssignment(
        agent=agent,
        mode=body.get("mode") or "headless",
        model=model,
        models=models or adapter_models,
        command=None if adapter else command,
        display_name=body.get("display_name") or (adapter.display_name if adapter else agent),
        protocol=body.get("protocol") or (adapter.protocol if adapter else "stdio"),
        capabilities=adapter.capabilities if adapter else [role_name],
    )
    setattr(prefs.roles, role_name, assignment)
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

    try:
        subprocess.Popen(
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

    result = await db.execute(select(Project).filter(Project.id == body["project_id"]))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    await require_project_approval_for_mutation(project, current_entity)

    task = Task(
        title=body["title"],
        description=body.get("description", ""),
        project_id=body["project_id"],
        stage_id=body["stage_id"],
        priority=body.get("priority", 0),
        required_skills=body.get("required_skills", ""),
        status=body.get("status", "pending"),
        created_by=current_entity.id
    )
    db.add(task)
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

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await require_task_access(task, current_entity, db, require_write=True)

    # Stage policy transition validation for status changes (P0-3)
    new_status = body.get("status")
    if new_status and new_status != task.status:
        terminal_statuses = ("completed", "review", "done")
        if new_status in terminal_statuses:
            try:
                from kanban_runtime.stage_policy import get_stage_policy_for_stage, validate_transition, gather_transition_context
                from models import DiffReview, DiffReviewStatus
                from_policy = await get_stage_policy_for_stage(db, task.project_id, task.stage_id)
                to_policy = await get_stage_policy_for_stage(db, task.project_id, task.stage_id) if task.stage_id else None
                if from_policy:
                    move_initiator = "human" if current_entity.entity_type == EntityType.HUMAN else current_entity.name
                    ctx = await gather_transition_context(db, task_id, task.project_id)
                    transition_warning = validate_transition(
                        from_policy=from_policy,
                        to_policy=to_policy,
                        move_initiator=move_initiator,
                        has_diff_review=ctx["has_diff_review"],
                        has_required_outputs=True,
                        is_critical=ctx["is_critical"],
                    )
                    if transition_warning and current_entity.entity_type != EntityType.HUMAN:
                        raise HTTPException(status_code=409, detail=transition_warning)
            except ImportError:
                pass

    # Sequence order enforcement
    if new_status == "in_progress" and task.status != "in_progress":
        from routers.tasks import _check_predecessor
        err = await _check_predecessor(task, db)
        if err:
            raise HTTPException(status_code=409, detail=err)

    for field in ["title", "description", "priority", "required_skills", "status"]:
        if field in body:
            setattr(task, field, body[field])

    if body.get("status") == "completed" and task.completed_at is None:
        task.completed_at = datetime.now(UTC)

    task.version += 1
    task.updated_at = datetime.now(UTC)
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

    for field in ["name", "description", "path", "approval_status"]:
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

    default_stages = [
        ("Backlog", "Tasks to be done", 1),
        ("To Do", "Ready to start", 2),
        ("In Progress", "Currently being worked on", 3),
        ("Review", "Awaiting review", 4),
        ("Done", "Completed tasks", 5),
    ]
    for stage_name, stage_desc, order in default_stages:
        stage = Stage(
            name=stage_name,
            description=stage_desc,
            order=order,
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
    """List all registered users (entities)"""
    result = await db.execute(select(Entity).order_by(Entity.created_at.desc()))
    users = result.scalars().all()

    return templates.TemplateResponse("users.html", {
        "request": request,
        "users": users
    })
