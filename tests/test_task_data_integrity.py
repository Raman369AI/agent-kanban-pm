"""Regression tests for task integrity and board editing behavior."""

from datetime import datetime

from fastapi.testclient import TestClient

from agent_kanban_pm.app import app
import tests_helper


def _create_approved_project(client: TestClient, headers: dict, name: str) -> dict:
    response = client.post("/projects", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    project = response.json()
    response = client.post(
        f"/projects/{project['id']}/approve",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    detail = client.get(f"/projects/{project['id']}")
    assert detail.status_code == 200, detail.text
    return detail.json()


def _create_task(
    client: TestClient,
    headers: dict,
    project: dict,
    title: str = "Integrity task",
) -> dict:
    response = client.post(
        "/tasks",
        json={
            "title": title,
            "project_id": project["id"],
            "stage_id": project["stages"][0]["id"],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_task_references_cannot_cross_project_boundaries():
    with TestClient(app) as client:
        _, headers = tests_helper.local_owner_headers(client)
        first = _create_approved_project(client, headers, "Integrity project A")
        second = _create_approved_project(client, headers, "Integrity project B")
        foreign_stage = second["stages"][0]["id"]

        response = client.post(
            "/tasks",
            json={
                "title": "Wrong stage",
                "project_id": first["id"],
                "stage_id": foreign_stage,
            },
            headers=headers,
        )
        assert response.status_code == 422
        assert "does not exist in project" in response.json()["detail"]

        foreign_parent = _create_task(
            client,
            headers,
            second,
            title="Foreign parent",
        )
        response = client.post(
            "/tasks",
            json={
                "title": "Wrong parent",
                "project_id": first["id"],
                "parent_task_id": foreign_parent["id"],
            },
            headers=headers,
        )
        assert response.status_code == 422
        assert "Parent task" in response.json()["detail"]

        task = _create_task(client, headers, first)
        response = client.patch(
            f"/tasks/{task['id']}",
            json={"stage_id": foreign_stage},
            headers=headers,
        )
        assert response.status_code == 409

        response = client.post(
            "/ui/tasks/create",
            json={
                "title": "Wrong UI stage",
                "project_id": first["id"],
                "stage_id": foreign_stage,
            },
            headers=headers,
        )
        assert response.status_code == 422

        response = client.patch(
            f"/ui/tasks/{task['id']}/move",
            json={"stage_id": foreign_stage},
            headers=headers,
        )
        assert response.status_code == 422


def test_reopening_task_clears_completion_timestamp():
    with TestClient(app) as client:
        _, headers = tests_helper.local_owner_headers(client)
        project = _create_approved_project(client, headers, "Reopen project")
        task = _create_task(client, headers, project)

        completed = client.patch(
            f"/tasks/{task['id']}",
            json={"status": "completed"},
            headers=headers,
        )
        assert completed.status_code == 200, completed.text
        assert completed.json()["completed_at"] is not None

        reopened = client.patch(
            f"/tasks/{task['id']}",
            json={"status": "pending"},
            headers=headers,
        )
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["completed_at"] is None


def test_comment_creation_requires_an_attributed_author():
    with TestClient(app) as client:
        _, headers = tests_helper.local_owner_headers(client)
        project = _create_approved_project(client, headers, "Comments project")
        task = _create_task(client, headers, project)

        anonymous = client.post(
            "/comments",
            json={"task_id": task["id"], "content": "anonymous"},
        )
        assert anonymous.status_code == 401
        assert client.get(f"/tasks/{task['id']}/comments").json() == []

        attributed = client.post(
            "/comments",
            json={"task_id": task["id"], "content": "safe <b>text</b>"},
            headers=headers,
        )
        assert attributed.status_code == 201, attributed.text
        assert attributed.json()["author_id"] == int(headers["x-entity-id"])
        assert attributed.json()["author"]["name"]


def test_board_editing_uses_canonical_values_and_escapes_comments():
    with TestClient(app) as client:
        _, headers = tests_helper.local_owner_headers(client)
        project = _create_approved_project(client, headers, "Board edit project")
        _create_task(client, headers, project)

        response = client.get(f"/ui/projects/{project['id']}/board")
        assert response.status_code == 200
        body = response.text
        assert "<body>" in body
        assert str(datetime.now().year) in body
        assert "data.description || ''" in body
        assert "data.status || 'pending'" in body
        assert "escapeHtml(c.content)" in body
        assert "statusBadge.textContent.trim()" not in body


def test_ui_edit_rejects_invalid_status_instead_of_raising_server_error():
    with TestClient(app) as client:
        _, headers = tests_helper.local_owner_headers(client)
        project = _create_approved_project(
            client,
            headers,
            "Invalid edit project",
        )
        task = _create_task(client, headers, project)

        response = client.patch(
            f"/ui/tasks/{task['id']}/edit",
            json={"status": "not-a-status"},
            headers=headers,
        )
        assert response.status_code == 422
