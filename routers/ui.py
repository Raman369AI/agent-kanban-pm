from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime
from pathlib import Path
import logging

from database import get_db
from models import Project, Task, Entity, Stage, Comment, EntityType, TaskStatus, ApprovalStatus, TaskLog, ProjectWorkspace, Role
from schemas import ProjectResponse
from auth import get_current_entity, is_owner_or_manager, require_project_approval_for_mutation, require_task_access
from event_bus import event_bus, EventType

logger = logging.getLogger(__name__)

# Initialize templates
templates = Jinja2Templates(directory="templates")

router = APIRouter(include_in_schema=False)


def _is_noisy_project(project: Project) -> bool:
    text = f"{project.name or ''} {project.path or ''}".lower()
    markers = [
        "test", "phase 6", "visibility", "coordination",
        "approval queue", "diff review", "reject project",
        "folder picker smoke", "/tmp/",
    ]
    return any(marker in text for marker in markers)


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

    result = await db.execute(
        select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    await require_task_access(task, current_entity, db, require_write=True)

    old_stage_id = task.stage_id
    task.stage_id = new_stage_id
    task.status = new_status
    if new_status == "completed" and task.completed_at is None:
        task.completed_at = datetime.utcnow()
    task.version += 1
    task.updated_at = datetime.utcnow()

    await db.commit()

    logger.info(f"Task moved via UI: {task.title} by {current_entity.name}")

    await event_bus.publish(
        EventType.TASK_MOVED.value,
        {
            "task_id": task_id,
            "title": task.title,
            "from_stage_id": old_stage_id,
            "to_stage_id": new_stage_id,
            "status": new_status
        },
        project_id=task.project_id,
        entity_id=current_entity.id
    )

    return {"ok": True, "task_id": task_id, "stage_id": new_stage_id, "status": new_status}


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

    for field in ["title", "description", "priority", "required_skills", "status"]:
        if field in body:
            setattr(task, field, body[field])

    if body.get("status") == "completed" and task.completed_at is None:
        task.completed_at = datetime.utcnow()

    task.version += 1
    task.updated_at = datetime.utcnow()
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
    current_entity: Optional[Entity] = Depends(get_current_entity)
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

    project.updated_at = datetime.utcnow()
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
    current_entity: Optional[Entity] = Depends(get_current_entity)
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


@router.get("/ui/register", response_class=HTMLResponse)
async def ui_register_form(request: Request):
    """Show human registration form"""
    return templates.TemplateResponse("register.html", {"request": request})


@router.post("/ui/register", response_class=HTMLResponse)
async def ui_register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(""),
    skills: str = Form(""),
    db: AsyncSession = Depends(get_db)
):
    """Handle human registration form submission. Defaults to WORKER role."""
    from models import Role
    db_entity = Entity(
        name=name,
        entity_type=EntityType.HUMAN,
        email=email or None,
        skills=skills,
        role=Role.WORKER
    )
    db.add(db_entity)
    await db.commit()

    return templates.TemplateResponse("register.html", {
        "request": request,
        "success": f"Account created for {name}!"
    })


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
