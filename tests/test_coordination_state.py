import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit
from fastapi.testclient import TestClient

from models import ContributionType
from main import app
import routers.agent_activity as agent_activity_router


def test_coordination_state_and_terminal_feed():
    with TestClient(app) as client:
        owner, owner_headers = tests_helper.local_owner_headers(client)

        agent = client.post("/entities/register/agent", json={
            "name": "Coordination Agent",
            "entity_type": "agent",
            "role": "manager"
        }, headers=owner_headers).json()
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


def test_github_sync_uses_local_git_when_gh_is_unauthenticated(monkeypatch):
    suffix = uuid4().hex[:8]

    monkeypatch.setattr(agent_activity_router, "_git_config_value", lambda path, key: "Local User" if key == "user.name" else None)

    monkeypatch.setattr(agent_activity_router, "_discover_github_repos", lambda path: ["owner/repo"])
    monkeypatch.setattr(agent_activity_router, "_gh_available", lambda: True)
    monkeypatch.setattr(agent_activity_router, "_gh_authenticated", lambda path: False)
    monkeypatch.setattr(
        agent_activity_router,
        "_git_local_commits",
        lambda path, author, limit=50: [{
            "sha": "abc123",
            "title": "Local commit",
            "url": None,
            "state": "committed",
            "createdAt": "2026-04-29T12:00:00Z",
            "updatedAt": "2026-04-29T12:00:00Z",
        }],
    )

    with TestClient(app) as client:
        owner = client.get("/entities/me").json()
        owner_headers = {"x-entity-id": str(owner["id"])}

        project = client.post("/projects", json={
            "name": f"Git Sync Project {suffix}",
            "description": "Local git sync test",
            "path": "/tmp/git-sync-project"
        }, headers=owner_headers).json()

        response = client.post(
            f"/agents/projects/{project['id']}/contributions/sync/github",
            headers=owner_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["author"] == "Local User"
        assert data["local_author"] == "Local User"
        assert data["github_author"] is None
        assert data["seen"] == 1
        assert any("not authenticated" in error for error in data["errors"])

        contributions = client.get(f"/agents/projects/{project['id']}/contributions").json()
        assert any(
            c["contribution_type"] == ContributionType.COMMIT.value
            and c["external_id"] == "owner/repo@abc123"
            for c in contributions
        )


def test_github_sync_uses_git_author_for_commits_even_when_gh_is_authenticated(monkeypatch):
    suffix = uuid4().hex[:8]
    gh_commands = []

    def fake_run_project_command(command, cwd):
        gh_commands.append(command)
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:3] == ["gh", "issue", "list"]:
            return "[]"
        if command[:3] == ["gh", "search", "prs"]:
            return "[]"
        if command[:3] == ["gh", "search", "commits"]:
            return "[]"
        raise AssertionError(f"unexpected command: {command}")

    captured_commit_authors = []

    def fake_git_local_commits(path, author, limit=50):
        captured_commit_authors.append(author)
        return [{
            "sha": "def456",
            "title": "Commit with local identity",
            "url": None,
            "state": "committed",
            "createdAt": "2026-04-29T12:00:00Z",
            "updatedAt": "2026-04-29T12:00:00Z",
        }]

    monkeypatch.setattr(agent_activity_router, "_git_config_value", lambda path, key: "Local Git Name" if key == "user.name" else None)
    monkeypatch.setattr(agent_activity_router, "_discover_github_repos", lambda path: ["owner/repo"])
    monkeypatch.setattr(agent_activity_router, "_gh_available", lambda: True)
    monkeypatch.setattr(agent_activity_router, "_gh_authenticated", lambda path: True)
    monkeypatch.setattr(agent_activity_router, "_github_current_user", lambda path: "github-login")
    monkeypatch.setattr(agent_activity_router, "_run_project_command", fake_run_project_command)
    monkeypatch.setattr(agent_activity_router, "_git_local_commits", fake_git_local_commits)

    with TestClient(app) as client:
        owner = client.get("/entities/me").json()
        owner_headers = {"x-entity-id": str(owner["id"])}

        project = client.post("/projects", json={
            "name": f"Git Sync Auth Project {suffix}",
            "description": "Local git author with gh auth test",
            "path": "/tmp/git-sync-auth-project"
        }, headers=owner_headers).json()

        response = client.post(
            f"/agents/projects/{project['id']}/contributions/sync/github",
            headers=owner_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["author"] == "Local Git Name"
        assert data["local_author"] == "Local Git Name"
        assert data["github_author"] == "github-login"
        assert captured_commit_authors == ["Local Git Name"]
        assert any("--author" in command and "github-login" in command for command in gh_commands)
