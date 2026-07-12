import pytest
from sqlalchemy import select

from agent_kanban_pm.db import async_session_maker, init_db
from agent_kanban_pm.mcp.server import KanbanMCPServer
from agent_kanban_pm.models import ApprovalStatus, Entity, EntityType, Project, Role, Stage, Task, TaskStatus


@pytest.mark.asyncio
async def test_get_tasks_assigned_to_me_uses_authenticated_caller():
    await init_db()

    async with async_session_maker() as db:
        agent = Entity(
            name="MCP Assigned Filter Agent",
            entity_type=EntityType.AGENT,
            role=Role.WORKER,
            is_active=True,
        )
        other_agent = Entity(
            name="MCP Other Agent",
            entity_type=EntityType.AGENT,
            role=Role.WORKER,
            is_active=True,
        )
        project = Project(
            name="MCP Assigned Filter Project",
            approval_status=ApprovalStatus.APPROVED,
        )
        db.add_all([agent, other_agent, project])
        await db.flush()

        stage = Stage(name="To Do", order=1, project_id=project.id)
        db.add(stage)
        await db.flush()
        assigned_task = Task(
            title="Assigned task",
            project_id=project.id,
            stage_id=stage.id,
            status=TaskStatus.PENDING,
        )
        unassigned_task = Task(
            title="Other task",
            project_id=project.id,
            stage_id=stage.id,
            status=TaskStatus.PENDING,
        )
        assigned_task.assignees.append(agent)
        unassigned_task.assignees.append(other_agent)
        db.add_all([stage, assigned_task, unassigned_task])
        await db.commit()
        await db.refresh(agent)
        caller_id = agent.id
        project_id = project.id

    async with async_session_maker() as db:
        caller = await db.scalar(select(Entity).filter(Entity.id == caller_id))

    server = KanbanMCPServer.__new__(KanbanMCPServer)
    server.caller_entity = caller

    tasks = await server._handle_get_tasks({"project_id": project_id, "assigned_to_me": True})

    assert [task["title"] for task in tasks] == ["Assigned task"]
