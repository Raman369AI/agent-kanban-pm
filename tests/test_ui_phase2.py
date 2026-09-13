"""Phase 2 setup state follows real agent runs, not manual board movement."""

import asyncio
import re
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agent_kanban_pm.app import app
from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import AgentSession, AgentSessionStatus, Entity, EntityType, Role
from agent_kanban_pm.routers import ui
import tests_helper


def _step_is_complete(html: str, step_id: str) -> bool:
    match = re.search(r'<div class="setup-step-item ([^"]+)" id="' + step_id + '"', html)
    assert match, f"Missing setup step {step_id}"
    return "step-complete" in match.group(1)


def test_setup_queues_then_assigns_and_requires_a_real_session(monkeypatch, tmp_path):
    agent_name = f"phase2-worker-{uuid.uuid4().hex[:8]}"

    async def configured_roles():
        return {
            "roles": [{
                "role": "worker", "agent": agent_name, "display_name": "Phase 2 worker",
                "command": "worker-cli", "model": "default", "mode": "headless",
                "autonomy": "supervised", "installed": True,
            }],
            "candidates": [],
            "role_names": ["worker"],
        }

    monkeypatch.setattr(ui, "_role_assignment_payload", configured_roles)
    with TestClient(app) as client:
        owner, headers = tests_helper.local_owner_headers(client)
        created = client.post("/projects", json={"name": "Phase 2 setup"}, headers=headers)
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]
        assert client.post(f"/projects/{project_id}/approve", json={}, headers=headers).status_code == 200
        updated = client.patch(
            f"/ui/projects/{project_id}/edit",
            json={"path": str(tmp_path)},
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        stages = {stage["name"]: stage["id"] for stage in client.get(f"/projects/{project_id}").json()["stages"]}
        created_task = client.post(
            "/ui/tasks/create",
            json={"project_id": project_id, "stage_id": stages["Backlog"], "title": "First real work"},
            headers=headers,
        )
        assert created_task.status_code == 200, created_task.text
        task_id = created_task.json()["task"]["id"]

        board_url = f"/ui/projects/{project_id}/board"
        backlog_html = client.get(board_url).text
        assert _step_is_complete(backlog_html, "step-choose-folder")
        assert _step_is_complete(backlog_html, "step-configure-worker")
        assert _step_is_complete(backlog_html, "step-create-task")
        assert not _step_is_complete(backlog_html, "step-start-work")
        assert f"moveToTodo({task_id},this)" in backlog_html

        queued = client.patch(
            f"/ui/tasks/{task_id}/move",
            json={"stage_id": stages["To Do"], "status": "pending"},
            headers=headers,
        )
        assert queued.status_code == 200, queued.text
        todo_html = client.get(board_url).text
        assert f"assignRoleToTask({task_id},'worker',this)" in todo_html
        assert not _step_is_complete(todo_html, "step-start-work")

        # A human assignee does not replace the configured worker.
        human_assignment = client.post(
            f"/ui/tasks/{task_id}/assign",
            json={"entity_id": owner["id"], "action": "assign"},
            headers=headers,
        )
        assert human_assignment.status_code == 200, human_assignment.text
        assert f"assignRoleToTask({task_id},'worker',this)" in client.get(board_url).text

        # Moving a stage/status manually cannot complete the execution step.
        moved = client.patch(
            f"/ui/tasks/{task_id}/move",
            json={"stage_id": stages["In Progress"], "status": "in_progress"},
            headers=headers,
        )
        assert moved.status_code == 200, moved.text
        assert not _step_is_complete(client.get(board_url).text, "step-start-work")

        async def create_failed_session():
            async with async_session_maker() as db:
                agent = Entity(
                    name=agent_name, entity_type=EntityType.AGENT,
                    role=Role.WORKER, is_active=True,
                )
                db.add(agent)
                await db.flush()
                session = AgentSession(
                    agent_id=agent.id, project_id=project_id, task_id=task_id,
                    workspace_path=str(tmp_path), status=AgentSessionStatus.ERROR,
                    ended_at=datetime.now(UTC),
                )
                db.add(session)
                await db.commit()
                return session.id

        session_id = asyncio.run(create_failed_session())
        failed_html = client.get(board_url).text
        assert not _step_is_complete(failed_html, "step-start-work")
        assert "Agent session failed" in failed_html

        async def mark_running():
            async with async_session_maker() as db:
                session = await db.get(AgentSession, session_id)
                session.status = AgentSessionStatus.ACTIVE
                session.ended_at = None
                await db.commit()

        asyncio.run(mark_running())
        running_html = client.get(board_url).text
        assert _step_is_complete(running_html, "step-start-work")
        assert "Agent session active on task" in running_html

        roles_html = client.get("/ui/users").text
        assert "Current work" in roles_html
        assert "First real work" in roles_html
        assert f"/ui/projects/{project_id}/workbench" in roles_html
        assert "Available" in roles_html
