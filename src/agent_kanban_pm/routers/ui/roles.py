from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from agent_kanban_pm.models import (
    Entity, EntityType, Role,
)


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
