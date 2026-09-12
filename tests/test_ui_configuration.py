"""Regression coverage for settings edits and stable workflow identities."""
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_kanban_pm.app import app
from agent_kanban_pm.models import Entity, EntityType, Role
from agent_kanban_pm.routers.ui import _role_assignment_payload, ui_assign_cli_to_role
from agent_kanban_pm.runtime import adapter_loader, preferences
from agent_kanban_pm.runtime.adapter_loader import AdapterSpec, InvokeSpec, ModelSpec
from agent_kanban_pm.runtime.assignment_launcher import _build_agent_command
from agent_kanban_pm.runtime.preferences import Preferences, RoleAssignment, RoleConfig
import tests_helper


def request(body):
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode()}
    return Request({"type": "http", "method": "POST", "path": "/"}, receive)


@pytest.fixture
def role_settings(monkeypatch):
    assignment = RoleAssignment(
        agent="first", model="large", mode="interactive", autonomy="auto",
        prompt_flag="--prompt", chat_stdin=True, chat_timeout_seconds=75,
        owns=["src/**"], review_only=True,
    )
    prefs = Preferences(roles=RoleConfig(worker=assignment), custom_roles={"security": assignment.model_copy()})
    adapters = [AdapterSpec(name=name, display_name=name, invoke=InvokeSpec(command=name, model_flag="--model"),
                            models=[ModelSpec(id=model)]) for name, model in [("first", "large"), ("second", "small")]]
    monkeypatch.setattr(preferences, "load_preferences", lambda: prefs)
    monkeypatch.setattr(preferences, "save_preferences", lambda value: None)
    monkeypatch.setattr(adapter_loader, "load_all_adapters", lambda: adapters)
    monkeypatch.setattr(adapter_loader, "discover_popular_clis", lambda: [])
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)
    return prefs, adapters


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["worker", "security"])
async def test_role_save_preserves_unedited_options(role_settings, role):
    prefs, _ = role_settings
    before = prefs.get_role_assignments()[role].model_dump()
    owner = Entity(id=1, name="Owner", entity_type=EntityType.HUMAN, role=Role.OWNER)
    result = await ui_assign_cli_to_role(request({"role": role, "agent": "first", "model": "large"}), owner)
    saved = prefs.get_role_assignments()[role]
    for field in ("mode", "autonomy", "prompt_flag", "chat_stdin", "chat_timeout_seconds", "owns", "review_only"):
        assert getattr(saved, field) == before[field]
    assert saved.model_dump() == before
    assert "security" in result["role_names"]


@pytest.mark.asyncio
async def test_agent_model_validation_does_not_change_saved_role(role_settings):
    prefs, _ = role_settings
    before = prefs.roles.worker.model_dump()
    owner = Entity(id=1, name="Owner", entity_type=EntityType.HUMAN, role=Role.OWNER)
    with pytest.raises(HTTPException) as error:
        await ui_assign_cli_to_role(request({"role": "worker", "agent": "second", "model": "large"}), owner)
    assert error.value.status_code == 422
    assert prefs.roles.worker.model_dump() == before
    await ui_assign_cli_to_role(request({"role": "worker", "agent": "second", "model": "small", "autonomy": "supervised"}), owner)
    assert prefs.roles.worker.agent == "second"
    assert prefs.roles.worker.model == "small"
    assert prefs.roles.worker.autonomy == "supervised"
    assert prefs.roles.worker.owns == ["src/**"]


def test_task_command_uses_selected_model_without_enabling_bypass(role_settings):
    _, adapters = role_settings
    adapter = adapters[0]
    adapter.task_command.auto_args = ["--bypass"]
    command = _build_agent_command(adapter, "/tmp/workspace", "Do the task", model="selected")
    assert command[command.index("--model") + 1] == "selected"
    assert "--bypass" not in command
    assert "Do the task" in command


@pytest.mark.parametrize("name", ["codex", "opencode"])
def test_default_model_labels_are_not_sent_as_cli_models(role_settings, name):
    from agent_kanban_pm.runtime.role_supervisor import build_command_for_role
    adapter = AdapterSpec(name=name, display_name=name, invoke=InvokeSpec(command=name, model_flag="--model"),
                          models=[ModelSpec(id=name + '-default')])
    assignment = RoleAssignment(agent=name, model=name + '-default')
    assert '--model' not in _build_agent_command(adapter, '/tmp/workspace', 'task', model=assignment.model)
    assert '--model' not in build_command_for_role(adapter, assignment, 'worker', 'http://localhost')


@pytest.mark.parametrize("name", ["claude", "aider"])
def test_bundled_adapters_follow_cli_default_until_model_is_selected(name, monkeypatch):
    from agent_kanban_pm.runtime.role_supervisor import build_command_for_role

    adapter = adapter_loader.load_adapter(adapter_loader.BUNDLED_ADAPTERS_DIR / f"{name}.yaml")
    assert adapter is not None
    assert [model.id for model in adapter.models] == ["default"]
    monkeypatch.setattr("shutil.which", lambda command: "/usr/bin/" + command)

    default_assignment = RoleAssignment(agent=name, model=adapter.models[0].id)
    assert "--model" not in _build_agent_command(adapter, "/tmp/workspace", "task", model=default_assignment.model)
    assert "--model" not in build_command_for_role(adapter, default_assignment, "worker", "http://localhost")

    selected = "my-selected-model"
    task_command = _build_agent_command(adapter, "/tmp/workspace", "task", model=selected)
    role_command = build_command_for_role(
        adapter, RoleAssignment(agent=name, model=selected), "worker", "http://localhost"
    )
    assert task_command[task_command.index("--model") + 1] == selected
    assert role_command[role_command.index("--model") + 1] == selected


@pytest.mark.asyncio
async def test_role_payload_uses_refreshed_adapter_models(role_settings):
    prefs, _ = role_settings
    prefs.roles.worker.models = ["stale-model"]
    payload = await _role_assignment_payload()
    worker = next(role for role in payload["roles"] if role["role"] == "worker")
    assert worker["models"] == ["large"]
    assert worker["model"] == "large"


def test_renaming_stage_preserves_transition_semantics():
    with TestClient(app) as client:
        _, headers = tests_helper.local_owner_headers(client)
        project = client.post('/projects', json={"name": "Stage identity regression"}, headers=headers).json()
        project_id = project["id"]
        assert client.post(f'/projects/{project_id}/approve', json={}, headers=headers).status_code == 200
        stages = client.get(f'/projects/{project_id}').json()["stages"]
        done = next(stage for stage in stages if stage["name"] == "Done")
        response = client.patch(f'/stages/{done["id"]}', json={"name": "Shipped"}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["workflow_key"] == "done"
        created = client.post('/ui/tasks/create', json={"project_id": project_id, "stage_id": stages[0]['id'], "title": "Ship it"}, headers=headers)
        assert created.status_code == 200, created.text
        task = created.json()["task"]
        moved = client.patch(f'/ui/tasks/{task["id"]}/move', json={"stage_id": done["id"]}, headers=headers)
        assert moved.status_code == 200, moved.text
        assert client.get(f'/tasks/{task["id"]}').json()["status"] == "completed"
        custom = client.post(f'/projects/{project_id}/stages', json={"name": "Waiting externally", "order": 6}, headers=headers).json()
        moved = client.patch(f'/ui/tasks/{task["id"]}/move', json={"stage_id": custom["id"]}, headers=headers)
        assert moved.status_code == 200, moved.text
        assert client.get(f'/tasks/{task["id"]}').json()["status"] == "completed"
        board = client.get(f'/ui/projects/{project_id}/board').text
        assert 'data-stage-name="Shipped" data-stage-key="done" data-stage-status="completed"' in board
