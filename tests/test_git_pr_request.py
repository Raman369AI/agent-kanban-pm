import asyncio
import json
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import tests_helper
from agent_kanban_pm.app import app
from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    AgentSession,
    AgentSessionStatus,
    LaunchRequest,
    StagePolicy,
    Task,
    TaskLog,
    task_assignments,
)
from agent_kanban_pm.routers import ui
from agent_kanban_pm.runtime import adapter_loader, preferences
from agent_kanban_pm.runtime.preferences import Preferences, RoleAssignment, RoleConfig


def _configure_git_pr(monkeypatch, agent_name):
    prefs = Preferences(roles=RoleConfig(
        git_pr=RoleAssignment(agent=agent_name, command="git"),
    ))
    monkeypatch.setattr(preferences, "load_preferences", lambda: prefs)
    monkeypatch.setattr(adapter_loader, "load_all_adapters", lambda: [])
    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")

    async def role_payload():
        return {
            "roles": [{
                "role": "git_pr",
                "agent": agent_name,
                "display_name": "Git PR Agent",
                "command": "git",
                "mode": "headless",
                "autonomy": "supervised",
                "model": "default",
                "models": ["default"],
                "source": "standalone",
                "installed": True,
            }],
            "candidates": [],
            "role_names": ["git_pr"],
        }

    monkeypatch.setattr(ui, "_role_assignment_payload", role_payload)


async def _seed_implementation(task_id, agent_id, *, manual_policy=True):
    async with async_session_maker() as db:
        task = await db.get(Task, task_id)
        source = AgentSession(
            agent_id=agent_id,
            project_id=task.project_id,
            task_id=task.id,
            workspace_path="/tmp/git-pr-request-source",
            assigned_role="worker",
            status=AgentSessionStatus.DONE,
            work_revision="a" * 40,
            handoff_received_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            exit_code=0,
        )
        db.add(source)
        if manual_policy:
            policy = await db.scalar(select(StagePolicy).where(
                StagePolicy.stage_id == task.stage_id,
            ))
            policy.on_enter_roles_json = json.dumps(["test", "diff_review"])
        await db.commit()
        await db.refresh(source)
        return source.id


async def _request_facts(task_id):
    async with async_session_maker() as db:
        requests = list((await db.execute(
            select(LaunchRequest).where(
                LaunchRequest.task_id == task_id,
                LaunchRequest.role == "git_pr",
            ).order_by(LaunchRequest.id)
        )).scalars())
        log_count = await db.scalar(select(func.count(TaskLog.id)).where(
            TaskLog.task_id == task_id,
            TaskLog.message.like("Requested Git/PR handoff%"),
        ))
        assignment_count = await db.scalar(select(func.count()).select_from(
            task_assignments
        ).where(task_assignments.c.task_id == task_id))
        return requests, log_count, assignment_count


def test_git_pr_request_is_review_only_current_revision_and_idempotent(monkeypatch):
    agent_name = f"git-pr-agent-{uuid.uuid4().hex[:8]}"
    _configure_git_pr(monkeypatch, agent_name)

    with TestClient(app) as client:
        owner, headers = tests_helper.local_owner_headers(client)
        agent_response = client.post(
            "/entities/register/agent",
            json={"name": agent_name, "entity_type": "agent"},
            headers=headers,
        )
        assert agent_response.status_code == 201, agent_response.text
        agent_id = agent_response.json()["id"]

        project_response = client.post(
            "/projects",
            json={"name": "Manual Git PR", "description": "Request coverage"},
            headers=headers,
        )
        assert project_response.status_code == 201, project_response.text
        project_id = project_response.json()["id"]
        assert client.post(
            f"/projects/{project_id}/approve", json={}, headers=headers
        ).status_code == 200
        stages = {
            stage["name"]: stage["id"]
            for stage in client.get(f"/projects/{project_id}").json()["stages"]
        }

        backlog = client.post(
            "/ui/tasks/create",
            json={
                "project_id": project_id,
                "stage_id": stages["Backlog"],
                "title": "Wrong stage",
            },
            headers=headers,
        ).json()["task"]
        wrong_stage = client.post(
            f"/ui/tasks/{backlog['id']}/request-git-pr", json={}, headers=headers
        )
        assert wrong_stage.status_code == 409
        assert "only be requested from Review" in wrong_stage.json()["detail"]

        review = client.post(
            "/ui/tasks/create",
            json={
                "project_id": project_id,
                "stage_id": stages["Review"],
                "title": "Ship current revision",
            },
            headers=headers,
        ).json()["task"]
        missing_source = client.post(
            f"/ui/tasks/{review['id']}/request-git-pr", json={}, headers=headers
        )
        assert missing_source.status_code == 409
        assert "committed implementation handoff" in missing_source.json()["detail"]

        source_id = asyncio.run(_seed_implementation(review["id"], agent_id))
        board = client.get(f"/ui/projects/{project_id}/board")
        assert board.status_code == 200
        assert 'aria-label="Request Git/PR handoff for task Ship current revision"' in board.text

        worker_request = client.post(
            f"/ui/tasks/{review['id']}/request-git-pr",
            json={},
            headers={"X-Entity-ID": str(agent_id)},
        )
        assert worker_request.status_code == 403

        first = client.post(
            f"/ui/tasks/{review['id']}/request-git-pr", json={}, headers=headers
        )
        assert first.status_code == 200, first.text
        assert first.json()["created"] is True
        assert first.json()["source_session_id"] == source_id
        assert first.json()["work_revision"] == "a" * 40

        second = client.post(
            f"/ui/tasks/{review['id']}/request-git-pr", json={}, headers=headers
        )
        assert second.status_code == 200, second.text
        assert second.json()["created"] is False
        assert second.json()["request_id"] == first.json()["request_id"]

        requests, log_count, assignment_count = asyncio.run(_request_facts(review["id"]))
        assert len(requests) == 1
        assert requests[0].source_session_id == source_id
        assert log_count == 1
        assert assignment_count == 1

        board_js = client.get("/static/js/board.js").text
        assert "window.requestGitPr" in board_js
        assert "/request-git-pr" in board_js
