"""Stage-entry assignments shared by manual moves and automatic handoffs."""
from typing import Optional
from sqlalchemy import select
from agent_kanban_pm.models import Task, Stage, task_assignments
from agent_kanban_pm.events import event_bus, EventType


async def assign_stage_entry_roles(db, task: Task, stage: Stage, *, skip_agent_id: Optional[int] = None) -> list[dict]:
    from agent_kanban_pm.runtime.preferences import load_preferences
    from agent_kanban_pm.runtime.stage_policy import get_stage_policy_for_stage, policy_roles
    from agent_kanban_pm.models import Entity, EntityType

    policy = await get_stage_policy_for_stage(db, task.project_id, stage.id)
    roles = policy_roles(policy) if policy else []
    if not roles:
        return []

    prefs = load_preferences()
    assignments = prefs.get_role_assignments() if prefs else {}
    assigned: list[dict] = []
    for role_name in roles:
        assignment = assignments.get(role_name)
        if not assignment or not assignment.agent:
            continue
        entity = (await db.execute(
            select(Entity).filter(
                Entity.name == assignment.agent,
                Entity.entity_type == EntityType.AGENT,
                Entity.is_active == True,
            )
        )).scalar_one_or_none()
        if not entity or entity.id == skip_agent_id:
            continue

        exists = await db.execute(
            select(task_assignments.c.task_id).where(
                task_assignments.c.task_id == task.id,
                task_assignments.c.entity_id == entity.id,
            )
        )
        if exists.first():
            assigned.append({"role": role_name, "entity_id": entity.id, "agent": entity.name, "already_assigned": True})
            continue
        await db.execute(task_assignments.insert().values(task_id=task.id, entity_id=entity.id))
        assigned.append({"role": role_name, "entity_id": entity.id, "agent": entity.name, "already_assigned": False})
    return assigned


async def enqueue_stage_entry(db, task, stage, *, actor_id=None, source_session_id=None):
    if stage.key == "to_do":
        ids = (await db.execute(select(task_assignments.c.entity_id).where(
            task_assignments.c.task_id == task.id))).scalars()
        assigned = [{"entity_id": agent_id, "role": None} for agent_id in ids]
    elif stage.key in {"review", "done"}:
        assigned = await assign_stage_entry_roles(db, task, stage)
        if source_session_id is None:
            from agent_kanban_pm.runtime.review_gate import implementation_for_task
            source = await implementation_for_task(db, task.id)
            source_session_id = source.id if source else None
    else:
        assigned = []
    for assignment in assigned:
        event_bus.enqueue(db, EventType.TASK_ASSIGNED.value,
                          {"task_id": task.id, "entity_id": assignment["entity_id"],
                           "role": assignment["role"], "stage_id": stage.id,
                           "source_session_id": source_session_id},
                          project_id=task.project_id, entity_id=actor_id)
    return assigned
