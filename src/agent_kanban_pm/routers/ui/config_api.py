from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import Optional
from pathlib import Path
import asyncio

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Project, Entity, ProjectWorkspace,
)
from agent_kanban_pm.auth import get_current_entity, require_owner, is_owner_or_manager
from . import roles

router = APIRouter(include_in_schema=False)


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
    return await roles._role_assignment_payload()


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
    return await roles._role_assignment_payload()


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
