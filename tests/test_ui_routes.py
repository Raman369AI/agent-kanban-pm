import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from agent_kanban_pm.app import app
import tests_helper


def test_ui_routes_and_board_render():
    """Smoke test key UI routes without starting/killing a real server."""
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
        assert '/static/js/board.js' in body
        assert "function openApprovalPopup" in client.get('/static/js/board.js').text
        assert client.get('/static/css/board.css').status_code == 200
        assert client.get('/static/js/role-settings.js').status_code == 200
        assert "&#128272; Approvals" not in body

        stage_names = re.findall(r'data-stage-name="([^"]+)"', body)
        add_task_stages = {
            name.lower().replace(" ", "").replace("-", "").replace("_", "")
            for name in re.findall(r'class="btn-add-task"[^>]*data-stage-name="([^"]+)"', body)
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


def test_board_phase1_interaction_fixes():
    """Phase 1 UI plan: creation entry points, pending guards, mobile nav,
    accurate labels, and readable status text."""
    with TestClient(app) as client:
        assert client.get("/").status_code == 200

        owner, headers = tests_helper.local_owner_headers(client)
        project_response = client.post(
            "/projects",
            json={"name": "Phase 1 UI", "description": "Interaction fixes"},
            headers=headers,
        )
        assert project_response.status_code == 201, project_response.text
        project = project_response.json()
        client.post(f"/projects/{project['id']}/approve", json={}, headers=headers)

        task_response = client.post(
            "/tasks",
            json={"title": "Phase 1 task", "project_id": project["id"]},
            headers=headers,
        )
        assert task_response.status_code == 201, task_response.text
        task = task_response.json()

        # Put the card in Backlog so the board renders its stage actions.
        detail = client.get(f"/projects/{project['id']}").json()
        stages = {s["name"]: s["id"] for s in detail["stages"]}
        staged = client.post(
            "/ui/tasks/create",
            json={
                "title": "Phase 1 backlog card",
                "project_id": project["id"],
                "stage_id": stages["Backlog"],
            },
            headers=headers,
        )
        assert staged.status_code == 200, staged.text
        task = staged.json()["task"]

        board = client.get(f"/ui/projects/{project['id']}/board")
        assert board.status_code == 200
        body = board.text
        board_js = client.get("/static/js/board.js").text
        base_html = client.get("/ui/projects").text
        main_js = client.get("/static/js/main.js").text
        style_css = client.get("/static/css/style.css").text

        # Two explicit creation entry points with the destination stage
        # visible before submission.
        assert 'id="new-task-btn"' in body
        assert "openNewTaskModal" in board_js
        assert "Plan work" in body
        assert 'id="task-form-stage"' in body
        assert "Planning proposes task cards" in body

        # One Enter handler only (no inline keydown on the plan input) and a
        # pending guard so repeated submit attempts cannot duplicate work.
        assert not re.search(r'id="chat-task-input"[^>]*onkeydown', body)
        assert "planRequestPending" in board_js
        assert "taskFormPending" in board_js
        assert 'id="chat-plan-error"' in body
        assert 'id="task-form-error"' in body

        # Global navigation reachable at phone widths via a header toggle.
        assert 'id="mobile-nav-toggle"' in base_html
        assert "closeMobileNav" in main_js
        assert "data-sidebar-open" in style_css
        assert ".mobile-nav-toggle" in style_css

        # Approval language is reserved for approval requests; the backlog
        # action describes its actual effect.
        assert "Move to To Do" in body
        assert "moveToTodo" in board_js
        assert "approveToTodo" not in board_js
        assert 'title="Move this card to the To Do stage"' in body

        # Destructive task actions live in an accessible overflow menu.
        assert 'aria-haspopup="menu"' in body
        assert "toggleTaskMenu" in board_js
        assert "Delete task" in body
        assert 'title="Delete"' not in body

        # Stage policy copy describes the runtime's automatic handoff.
        assert "STATUS.md" in body
        assert "never auto-assigns or auto-moves" not in body

        # Readable status labels instead of raw enum values.
        move = client.patch(
            f"/ui/tasks/{task['id']}/move",
            json={"stage_id": stages["In Progress"], "status": "in_progress"},
            headers=headers,
        )
        assert move.status_code == 200, move.text
        moved_board = client.get(f"/ui/projects/{project['id']}/board").text
        assert "In progress" in moved_board
        assert ">in_progress<" not in moved_board
        assert "function statusLabel" in board_js


def test_phase2_setup_guide_and_unified_navigation():
    """Phase 2 UI plan: setup checklist, unified project navigation,
    Agents & roles directory, and empty state recovery."""
    with TestClient(app) as client:
        owner, headers = tests_helper.local_owner_headers(client)

        # 1. New project with no folder or tasks initially
        proj_resp = client.post(
            "/projects",
            json={"name": "Phase 2 Guide", "description": "Setup & Nav tests"},
            headers=headers,
        )
        assert proj_resp.status_code == 201
        project = proj_resp.json()
        pid = project["id"]
        client.post(f"/projects/{pid}/approve", json={}, headers=headers)

        # Global navigation in base.html
        base_resp = client.get("/ui/projects")
        assert base_resp.status_code == 200
        assert "Agents &amp; roles" in base_resp.text or "Agents & roles" in base_resp.text

        # main.js defaults sidebar to expanded for new users
        main_js = client.get("/static/js/main.js").text
        assert "|| 'expanded'" in main_js

        # Consistent project navigation on every project page
        board_html = client.get(f"/ui/projects/{pid}/board").text
        activity_html = client.get(f"/ui/projects/{pid}/workbench").text
        changes_html = client.get(f"/ui/projects/{pid}/git").text
        settings_html = client.get(f"/ui/projects/{pid}/settings").text

        for page_html in (board_html, activity_html, changes_html, settings_html):
            assert "project-sub-nav" in page_html
            assert f"/ui/projects/{pid}/board" in page_html
            assert f"/ui/projects/{pid}/workbench" in page_html
            assert f"/ui/projects/{pid}/git" in page_html
            assert f"/ui/projects/{pid}/settings" in page_html
            assert "Board" in page_html
            assert "Activity" in page_html
            assert "Changes" in page_html
            assert "Settings" in page_html

        # Board page shows setup checklist with missing prerequisites
        assert "project-setup-checklist" in board_html
        assert "Choose folder" in board_html
        assert "Configure worker" in board_html
        assert "Create task" in board_html
        assert "Start work" in board_html
        assert "Missing prerequisite" in board_html or "Prerequisite" in board_html

        # Assignment modal in board.js: Configure agents action in empty state & unavailable explanation
        board_js = client.get("/static/js/board.js").text
        assert "Configure agents" in board_js
        assert "not found on PATH" in board_js

        # Agents & roles page (/ui/users)
        users_resp = client.get("/ui/users")
        assert users_resp.status_code == 200
        users_html = users_resp.text
        assert "Agents &amp; Roles" in users_html or "Agents & Roles" in users_html
        assert "Configured Roles" in users_html
        assert "Team Members" in users_html
        assert "highlight-antigravity" not in users_html
        assert "Configure roles" in users_html
        assert "role-editor-container" in users_html

        # Project cards in /ui/projects have unified actions
        projects_html = client.get("/ui/projects").text
        assert f"/ui/projects/{pid}/settings" in projects_html
        assert "Activity" in projects_html
        assert "Changes" in projects_html
