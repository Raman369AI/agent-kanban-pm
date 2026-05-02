import sys
import asyncio
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from database import engine
from main import app
import tests_helper


def test_ui_routes_and_board_render():
    """Smoke test key UI routes without starting/killing a real server."""
    asyncio.run(engine.dispose())
    with TestClient(app) as client:
        routes = [
            ("/", 200),
            ("/ui/projects", 200),
            ("/ui/users", 200),
            ("/static/css/style.css", 200),
            ("/static/css/kanban_premium.css", 200),
            ("/static/js/main.js", 200),
            ("/docs", 200),
        ]
        for path, expected_status in routes:
            response = client.get(path)
            assert response.status_code == expected_status, f"{path}: {response.status_code}"

        board_probe = client.get("/ui/projects/1/board")
        assert board_probe.status_code in (200, 404)

        owner, headers = tests_helper.local_owner_headers(client)
        assert owner["role"] == "owner"

        project_response = client.post(
            "/projects",
            json={"name": "UI Test", "description": "Testing UI"},
            headers=headers,
        )
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()

        approve_response = client.post(
            f"/projects/{project['id']}/approve",
            json={},
            headers=headers,
        )
        assert approve_response.status_code == 200, approve_response.text

        task_response = client.post(
            "/tasks",
            json={"title": "UI Test Task", "project_id": project["id"]},
            headers=headers,
        )
        assert task_response.status_code == 201, task_response.text

        projects_response = client.get("/ui/projects")
        assert projects_response.status_code == 200
        projects_body = projects_response.text
        assert "UI Test" in projects_body
        assert f"/ui/projects/{project['id']}/board" in projects_body
        assert f"/ui/projects/{project['id']}/workbench" in projects_body
        assert f"/ui/projects/{project['id']}/git" in projects_body

        board_response = client.get(f"/ui/projects/{project['id']}/board")
        assert board_response.status_code == 200
        body = board_response.text
        assert "board-revamp-shell" in body
        assert "kanban-column-revamp" in body
        assert 'id="approval-popup-overlay"' in body
        assert "function openApprovalPopup" in body
        assert "&#128272; Approvals" not in body

        stage_names = re.findall(r'data-stage-name="([^"]+)"', body)
        add_task_stages = {
            name.lower().replace(" ", "").replace("-", "").replace("_", "")
            for _, name in re.findall(r"openAddTaskModal\((\d+),\s*'([^']+)'\)", body)
        }
        assert "backlog" in add_task_stages
        assert "todo" in add_task_stages
        assert add_task_stages <= {"backlog", "todo"}
        for stage_name in stage_names:
            normalized = stage_name.lower().replace(" ", "").replace("-", "").replace("_", "")
            if normalized in {"inprogress", "review", "done", "completed"}:
                assert normalized not in add_task_stages

        workbench_response = client.get(f"/ui/projects/{project['id']}/workbench")
        assert workbench_response.status_code == 200
        assert f"{project['name']} — Workbench" in workbench_response.text
        assert (
            "&#9888; Approvals" in workbench_response.text
            or "\u26a0 Approvals" in workbench_response.text
        )
        assert "&#128272; Approvals" not in workbench_response.text

        git_response = client.get(f"/ui/projects/{project['id']}/git")
        assert git_response.status_code == 200
        assert "Git Contributions" in git_response.text

        for marker in ("TemplateNotFound", "Jinja2", "Traceback"):
            assert marker.lower() not in body.lower()
