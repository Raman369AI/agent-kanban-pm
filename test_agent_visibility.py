import os
import sys

sys.path.append(os.getcwd())

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from fastapi.testclient import TestClient

from main import app


def test_agent_workspace_visibility():
    with TestClient(app) as client:
        human = client.post("/entities/register/human", json={
            "name": "Visibility Owner",
            "entity_type": "human"
        }).json()
        human_headers = {"x-entity-id": str(human["id"])}

        project = client.post("/projects", json={
            "name": "Visibility Project",
            "description": "Workspace-aware activity test",
            "path": "/tmp/agent-kanban-visibility"
        }, headers=human_headers).json()

        agent = client.post("/entities/register/agent", json={
            "name": "Visibility Agent",
            "entity_type": "agent"
        }).json()
        agent_headers = {"x-entity-id": str(agent["id"])}

        session_response = client.post(f"/agents/{agent['id']}/sessions", json={
            "project_id": project["id"],
            "command": "codex",
            "model": "gpt",
            "mode": "auto"
        }, headers=agent_headers)
        assert session_response.status_code == 201
        session = session_response.json()
        assert session["workspace_path"] == "/tmp/agent-kanban-visibility"
        assert session["project_id"] == project["id"]

        activity_response = client.post(f"/agents/{agent['id']}/activity", json={
            "project_id": project["id"],
            "session_id": session["id"],
            "activity_type": "file_change",
            "source": "codex_event",
            "message": "Edited router",
            "workspace_path": "/tmp/agent-kanban-visibility",
            "file_path": "routers/tasks.py",
            "command": "apply_patch"
        }, headers=agent_headers)
        assert activity_response.status_code == 201

        feed = client.get(f"/agents/activity?project_id={project['id']}").json()
        assert any(
            entry["session_id"] == session["id"]
            and entry["file_path"] == "routers/tasks.py"
            and entry["source"] == "codex_event"
            for entry in feed
        )

        sessions = client.get(f"/agents/sessions?project_id={project['id']}&active_only=true").json()
        assert any(entry["id"] == session["id"] for entry in sessions)
