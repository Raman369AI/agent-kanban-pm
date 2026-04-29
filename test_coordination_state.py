import os
import sys

sys.path.append(os.getcwd())

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from fastapi.testclient import TestClient

from main import app


def test_coordination_state_and_terminal_feed():
    with TestClient(app) as client:
        owner = client.post("/entities/register/human", json={
            "name": "Coordination Owner",
            "entity_type": "human"
        }).json()
        owner_headers = {"x-entity-id": str(owner["id"])}

        agent = client.post("/entities/register/agent", json={
            "name": "Coordination Agent",
            "entity_type": "agent",
            "role": "manager"
        }).json()
        agent_headers = {"x-entity-id": str(agent["id"])}

        project = client.post("/projects", json={
            "name": "Coordination Project",
            "description": "Durable coordination state test",
            "path": "/tmp/coordination-project"
        }, headers=owner_headers).json()
        client.post(f"/projects/{project['id']}/approve", headers=owner_headers)

        workspaces = client.get(f"/agents/projects/{project['id']}/workspaces").json()
        assert any(w["root_path"] == "/tmp/coordination-project" and w["is_primary"] for w in workspaces)

        task = client.post("/tasks", json={
            "title": "Lease me",
            "project_id": project["id"]
        }, headers=owner_headers).json()

        session = client.post(f"/agents/{agent['id']}/sessions", json={
            "project_id": project["id"],
            "task_id": task["id"],
            "command": "codex --worktree /tmp/coordination-project",
            "mode": "auto"
        }, headers=agent_headers).json()

        lease_response = client.post(f"/agents/tasks/{task['id']}/lease", json={
            "task_id": task["id"],
            "agent_id": agent["id"],
            "session_id": session["id"],
            "ttl_seconds": 600
        }, headers=agent_headers)
        assert lease_response.status_code == 201
        lease = lease_response.json()
        assert lease["status"] == "active"

        decision_response = client.post(f"/agents/projects/{project['id']}/decisions", json={
            "project_id": project["id"],
            "manager_agent_id": agent["id"],
            "decision_type": "task_assign",
            "rationale": "Agent has the matching code capability.",
            "affected_task_ids": str([task["id"]]),
            "affected_agent_ids": str([agent["id"]])
        }, headers=agent_headers)
        assert decision_response.status_code == 201

        client.post(f"/agents/{agent['id']}/activity", json={
            "project_id": project["id"],
            "session_id": session["id"],
            "task_id": task["id"],
            "activity_type": "command",
            "source": "stdout",
            "message": "pytest passed",
            "command": "pytest -q"
        }, headers=agent_headers)

        terminal = client.get(f"/agents/sessions/{session['id']}/terminal").json()
        assert terminal["session"]["id"] == session["id"]
        assert any(a["command"] == "pytest -q" for a in terminal["activities"])

        summary_response = client.post(f"/agents/projects/{project['id']}/summaries", json={
            "project_id": project["id"],
            "task_id": task["id"],
            "agent_id": agent["id"],
            "summary": "Implemented and verified the task."
        }, headers=agent_headers)
        assert summary_response.status_code == 201

        contribution_response = client.post(f"/agents/projects/{project['id']}/contributions", json={
            "project_id": project["id"],
            "entity_id": owner["id"],
            "contribution_type": "pull_request",
            "provider": "github",
            "external_id": "42",
            "title": "Add coordination workbench",
            "url": "https://github.com/example/repo/pull/42",
            "status": "open"
        }, headers=owner_headers)
        assert contribution_response.status_code == 201

        contributions = client.get(f"/agents/projects/{project['id']}/contributions").json()
        assert any(c["external_id"] == "42" and c["contribution_type"] == "pull_request" for c in contributions)

        release_response = client.patch(f"/agents/leases/{lease['id']}/release", headers=agent_headers)
        assert release_response.status_code == 200
        assert release_response.json()["status"] == "released"
