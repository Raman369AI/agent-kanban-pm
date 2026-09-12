"""Browser-level regression tests for the primary board workflows."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Browser, Page, expect, sync_playwright  # noqa: E402


pytestmark = pytest.mark.e2e
_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def browser() -> Browser:
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture
def page(browser: Browser) -> Page:
    context = browser.new_context()
    instance = context.new_page()
    yield instance
    context.close()


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory):
    runtime_dir = tmp_path_factory.mktemp("playwright-server")
    db_path = runtime_dir / "kanban.db"
    home_path = runtime_dir / "home"
    home_path.mkdir()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "HOME": str(home_path),
            "KANBAN_TESTING": "1",
            "KANBAN_REGISTER_ALL_ADAPTERS": "0",
            "PYTHONPATH": str(_ROOT / "src")
            + os.pathsep
            + env.get("PYTHONPATH", ""),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "agent_kanban_pm.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Kanban test server exited with code {process.returncode}")
        try:
            if httpx.get(f"{base_url}/health", timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        process.terminate()
        raise RuntimeError("Kanban test server did not become healthy")

    yield base_url

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@pytest.fixture
def api(live_server: str):
    with httpx.Client(base_url=live_server, timeout=10, trust_env=False) as client:
        yield client


def _prepare_board(api: httpx.Client) -> dict:
    owner = api.get("/entities/me")
    owner.raise_for_status()
    owner_id = owner.json()["id"]
    headers = {"X-Entity-ID": str(owner_id)}
    suffix = uuid.uuid4().hex[:8]

    project_response = api.post(
        "/projects",
        json={"name": f"Browser Project {suffix}", "description": "Playwright coverage"},
        headers=headers,
    )
    project_response.raise_for_status()
    project_id = project_response.json()["id"]
    api.post(f"/projects/{project_id}/approve", json={}, headers=headers).raise_for_status()

    detail = api.get(f"/projects/{project_id}")
    detail.raise_for_status()
    stages = {stage["name"]: stage["id"] for stage in detail.json()["stages"]}
    task_response = api.post(
        "/ui/tasks/create",
        json={
            "project_id": project_id,
            "stage_id": stages["Backlog"],
            "title": f"Browser Task {suffix}",
            "description": "Canonical description from the API",
            "priority": 4,
            "status": "pending",
            "required_skills": "testing,accessibility",
        },
        headers=headers,
    )
    task_response.raise_for_status()
    task = task_response.json()["task"]
    return {
        "owner_id": owner_id,
        "headers": headers,
        "project_id": project_id,
        "stages": stages,
        "task": task,
    }


def test_drag_and_keyboard_card_movement(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")

    card = page.locator(f"#task-card-{task_id}")
    todo_zone = page.locator(
        '.kanban-column-revamp[data-stage-name="To Do"] .kanban-drop-zone-revamp'
    )
    card.drag_to(todo_zone)
    expect(todo_zone.locator(f"#task-card-{task_id}")).to_be_visible()
    expect(page.locator("#toast")).to_contain_text("Task moved to To Do")

    card.focus()
    card.press("ArrowRight")
    progress_zone = page.locator(
        '.kanban-column-revamp[data-stage-name="In Progress"] .kanban-drop-zone-revamp'
    )
    expect(progress_zone.locator(f"#task-card-{task_id}")).to_be_visible()
    expect(card).to_be_focused()


def test_layout_preferences_and_refresh_preserve_task_context(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    task_id = board['task']['id']
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    page.locator('#view-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-view', 'list')
    assert page.locator('.kanban-container-revamp').evaluate("el => getComputedStyle(el).flexDirection") == 'column'
    page.locator('#density-select').select_option('compact')
    face = page.locator(f'#task-card-{task_id} .task-compact-face')
    compact = face.evaluate('el => parseFloat(getComputedStyle(el).paddingTop)')
    page.locator('#density-select').select_option('spacious')
    assert face.evaluate('el => parseFloat(getComputedStyle(el).paddingTop)') > compact
    card = page.locator(f'#task-card-{task_id}')
    card.focus()
    card.press('Enter')
    card.locator('[data-tab="activity"]').click()
    card.focus()
    page.evaluate('() => refreshBoardFromServer()')
    expect(card).to_be_focused()
    expect(card.locator('[data-tab="activity"]')).to_have_class('expansion-tab active')
    page.reload()
    expect(page.locator('html')).to_have_attribute('data-view', 'list')
    expect(page.locator('#density-select')).to_have_value('spacious')


def test_renamed_stage_keyboard_move_uses_stable_status(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    progress_id = board['stages']['In Progress']
    api.patch(f'/stages/{progress_id}', json={'name': 'Building'}, headers=board['headers']).raise_for_status()
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    card = page.locator(f"#task-card-{board['task']['id']}")
    card.focus()
    card.press('ArrowRight')
    expect(card).to_have_attribute('data-current-stage', str(board['stages']['To Do']))
    expect(card).not_to_have_attribute('data-moving', 'true')
    card.press('ArrowRight')
    expect(page.locator(f'.kanban-column-revamp[data-stage-id="{progress_id}"]')).to_contain_text(board['task']['title'])
    expect(card).to_have_attribute('data-status', 'in_progress')


def test_role_editor_updates_models_and_preserves_other_drafts(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    roles = [dict(role=name, agent='first', display_name='First', command='first',
                  model='large', models=['large'], mode='interactive', autonomy='auto', installed=True)
             for name in ['worker', 'security']]
    data = {'roles': roles, 'role_names': ['worker', 'security'], 'candidates': [
        dict(agent='first', display_name='First', command='first', models=['large'], installed=True),
        dict(agent='second', display_name='Second', command='second', models=['small'], installed=True),
    ]}
    saved = []
    page.route('**/ui/api/roles', lambda route: route.fulfill(json=data))
    def save(route):
        payload = route.request.post_data_json
        saved.append(payload)
        next(role for role in roles if role['role'] == payload['role']).update(payload)
        route.fulfill(json=data)
    page.route('**/ui/api/roles/assign', save)
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    page.locator('summary').filter(has_text='Advanced').click()
    page.get_by_role('button', name='Team roles', exact=True).click()
    expect(page.locator('#role-mode-worker')).to_have_value('interactive')
    expect(page.locator('#role-autonomy-worker')).to_have_value('auto')
    page.locator('#role-autonomy-security').select_option('supervised')
    page.locator('#role-agent-worker').select_option('second')
    expect(page.locator('#role-model-worker')).to_have_value('small')
    row = page.locator('.role-settings-row').filter(has=page.locator('#role-agent-worker'))
    row.get_by_role('button', name='Save role').click()
    expect(row.get_by_role('status')).to_have_text('Saved')
    assert saved[0]['model'] == 'small'
    assert saved[0]['mode'] == 'interactive'
    assert saved[0]['autonomy'] == 'auto'
    expect(page.locator('#role-autonomy-security')).to_have_value('supervised')
    page.set_viewport_size({'width': 390, 'height': 844})
    dialog = page.locator('.role-settings-modal')
    assert dialog.evaluate('el => el.scrollWidth <= el.clientWidth')
    expect(row.get_by_role('button', name='Save role')).to_be_in_viewport()


def test_edit_modal_focus_and_detailed_error_toast(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")

    card = page.locator(f"#task-card-{task_id}")
    card.locator('button[title="Edit"]').click()
    dialog = page.get_by_role("dialog", name=f"Edit Task #{task_id}")
    expect(dialog).to_be_visible()
    title = page.locator("#task-form-title")
    expect(title).to_be_focused()
    expect(page.locator("#task-form-desc")).to_have_value(
        "Canonical description from the API"
    )
    expect(page.locator("#task-form-status")).to_have_value("pending")

    title.fill("Edited in a real browser")
    dialog.get_by_role("button", name="Save").click()
    expect(page.locator("#toast")).to_contain_text("Task updated!")
    expect(page.locator(f"#task-card-{task_id}")).to_contain_text(
        "Edited in a real browser"
    )

    page.route(
        f"**/ui/tasks/{task_id}/edit",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps({"detail": "Title violates the browser test policy"}),
        ),
    )
    page.locator(f"#task-card-{task_id}").locator('button[title="Edit"]').click()
    page.locator("#task-form-title").fill("Rejected title")
    page.get_by_role("dialog", name=f"Edit Task #{task_id}").get_by_role(
        "button", name="Save"
    ).click()
    expect(page.locator("#toast")).to_contain_text(
        "Title violates the browser test policy"
    )


def test_approval_queue_can_resolve_request(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    suffix = uuid.uuid4().hex[:8]
    agent_response = api.post(
        "/entities/register/agent",
        json={"name": f"browser-agent-{suffix}", "entity_type": "agent"},
        headers=board["headers"],
    )
    agent_response.raise_for_status()
    agent_id = agent_response.json()["id"]
    approval_response = api.post(
        "/agents/approvals",
        json={
            "project_id": board["project_id"],
            "task_id": board["task"]["id"],
            "agent_id": agent_id,
            "approval_type": "shell_command",
            "title": "Approve browser action",
            "message": "Run the browser test command?",
            "command": "pytest tests/e2e",
        },
        headers={"X-Entity-ID": str(agent_id)},
    )
    approval_response.raise_for_status()
    approval_id = approval_response.json()["id"]

    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    expect(page.locator("#header-bell-count")).to_have_text("1")
    page.locator("#header-bell-btn").click()
    page.locator("#bell-dropdown-list .insight-item").filter(
        has_text="Approve browser action"
    ).click()

    dialog = page.get_by_role("dialog", name="Approve browser action")
    expect(dialog).to_be_visible()
    expect(page.locator("#approval-popup-note")).to_be_focused()
    page.locator("#approval-popup-note").fill("Approved by Playwright")
    dialog.get_by_role("button", name="Approve", exact=True).click()
    expect(page.locator("#toast")).to_contain_text("Approval approved")

    resolved = api.get(
        f"/agents/approvals?project_id={board['project_id']}",
        headers=board["headers"],
    )
    resolved.raise_for_status()
    matching = [item for item in resolved.json() if item["id"] == approval_id]
    assert matching and matching[0]["status"] == "approved"
