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


def _complete_workflow_override(page: Page, reason: str) -> None:
    dialog = page.locator("#done-override-modal")
    expect(dialog).to_be_visible()
    dialog.locator("#done-override-reason").fill(reason)
    dialog.locator("#done-override-submit").click()
    expect(dialog).to_be_hidden()


def test_drag_and_keyboard_card_movement(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")

    card = page.locator(f"#task-card-{task_id}")
    todo_zone = page.locator(
        '.kanban-column-revamp[data-stage-name="To Do"] .kanban-drop-zone-revamp'
    )
    card.drag_to(todo_zone)
    _complete_workflow_override(page, "Queue this card for browser movement coverage.")
    expect(todo_zone.locator(f"#task-card-{task_id}")).to_be_visible()
    expect(page.locator("#toast")).to_contain_text("Task moved to To Do")

    card.focus()
    card.press("ArrowRight")
    _complete_workflow_override(page, "Start this card for keyboard movement coverage.")
    progress_zone = page.locator(
        '.kanban-column-revamp[data-stage-name="In Progress"] .kanban-drop-zone-revamp'
    )
    expect(progress_zone.locator(f"#task-card-{task_id}")).to_be_visible()
    expect(card).to_be_focused()


def test_completion_gates_and_reasoned_done_override(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    moved = api.patch(
        f"/ui/tasks/{task_id}/move",
        json={
            "stage_id": board["stages"]["Review"],
            "status": "in_review",
            "override_reason": "Place the task in Review to inspect completion gates.",
        },
        headers=board["headers"],
    )
    moved.raise_for_status()
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")

    card = page.locator(f"#task-card-{task_id}")
    card.click()
    panel = page.locator("#task-detail-panel")
    expect(panel.get_by_role("heading", name="Completion gates")).to_be_visible()
    expect(panel.locator(".completion-gate")).to_have_count(6)
    done_zone = page.locator(
        '.kanban-column-revamp[data-stage-key="done"] .kanban-drop-zone-revamp'
    )
    panel.locator("#task-panel-stage").select_option(str(board["stages"]["Done"]))
    dialog = page.get_by_role("dialog", name="Move to Done anyway?")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#done-override-gates li").first).to_be_visible()
    dialog.locator("#done-override-submit").click()
    expect(dialog.locator("#done-override-error")).to_contain_text("Enter a short reason")
    reason = "Accept incomplete evidence for this explicit browser-test decision."
    dialog.locator("#done-override-reason").fill(reason)
    dialog.locator("#done-override-submit").click()
    expect(done_zone.locator(f"#task-card-{task_id}")).to_be_visible()
    expect(page.locator("#toast")).to_contain_text("Task moved to Done")

    logs = api.get(f"/tasks/{task_id}/logs", headers=board["headers"])
    logs.raise_for_status()
    assert any(reason in item["message"] for item in logs.json())


def test_layout_preferences_and_refresh_preserve_task_context(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    task_id = board['task']['id']
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    page.locator('#view-toggle').click()
    expect(page.locator('html')).to_have_attribute('data-view', 'list')
    assert page.locator('.kanban-container-revamp').evaluate("el => getComputedStyle(el).flexDirection") == 'column'
    page.locator('.appearance-menu summary').click()
    page.locator('#density-select').select_option('compact')
    face = page.locator(f'#task-card-{task_id} .task-compact-face')
    compact = face.evaluate('el => parseFloat(getComputedStyle(el).paddingTop)')
    page.locator('#density-select').select_option('spacious')
    assert face.evaluate('el => parseFloat(getComputedStyle(el).paddingTop)') > compact
    card = page.locator(f'#task-card-{task_id}')
    card.focus()
    card.press('Enter')
    panel = page.locator('#task-detail-panel')
    panel.locator('[data-tab="activity"]').click()
    page.evaluate('() => refreshBoardFromServer()')
    expect(panel).to_be_visible()
    expect(panel.locator('[data-tab="activity"]')).to_have_class('expansion-tab active')
    expect(panel.locator('#task-panel-title')).to_have_text(board['task']['title'])
    page.reload()
    expect(panel).to_be_visible()
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
    _complete_workflow_override(page, 'Queue the renamed-stage card for keyboard coverage.')
    expect(card).to_have_attribute('data-current-stage', str(board['stages']['To Do']))
    expect(card).not_to_have_attribute('data-moving', 'true')
    card.press('ArrowRight')
    _complete_workflow_override(page, 'Start the renamed-stage card for keyboard coverage.')
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
    page.goto(f"{live_server}/ui/users")
    page.get_by_role('button', name='Configure roles', exact=True).click()
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
    editor = page.locator('#role-editor-container')
    assert editor.evaluate('el => el.scrollWidth <= el.clientWidth')
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


def test_plan_work_pending_guard_blocks_duplicate_submissions(
    page: Page, live_server: str, api: httpx.Client
):
    """One Enter press produces one request; repeats while pending are ignored."""
    board = _prepare_board(api)
    page.add_init_script(
        """
        window.__planCalls = 0;
        window.__planRelease = null;
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/ui/tasks/chat-plan') !== -1) {
                window.__planCalls += 1;
                return new Promise(resolve => { window.__planRelease = resolve; })
                    .then(() => new Response(
                        JSON.stringify({items: [{title: 'Ship login fix', description: 'From preview', priority: 5}]}),
                        {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
        };
        """
    )
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")

    inp = page.locator("#chat-task-input")
    inp.fill("Ship the login fix")
    inp.press("Enter")
    # Repeats while the request is pending must not create duplicate work.
    inp.press("Enter")
    page.locator("#chat-plan-btn").dispatch_event("click")
    page.locator("#chat-plan-btn").dispatch_event("click")
    expect(page.locator("#chat-plan-btn")).to_be_disabled()
    expect(page.locator("#chat-plan-btn")).to_have_text("Preparing…")
    assert page.evaluate("window.__planCalls") == 1

    page.evaluate("window.__planRelease && window.__planRelease()")
    expect(page.locator("#chat-plan-btn")).to_be_enabled()
    expect(page.locator("#plan-preview-modal")).to_be_visible()
    expect(inp).to_have_value("Ship the login fix")
    page.locator("#plan-preview-modal").get_by_role("button", name="Cancel").click()
    expect(inp).to_have_value("Ship the login fix")


def test_plan_work_failure_keeps_text_and_allows_retry(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    page.add_init_script(
        """
        window.__planMode = 'fail';
        window.__planCalls = 0;
        const originalFetch = window.fetch.bind(window);
        window.fetch = function(input, init) {
            const url = typeof input === 'string' ? input : (input && input.url) || '';
            if (url.indexOf('/ui/tasks/chat-plan') !== -1) {
                window.__planCalls += 1;
                if (window.__planMode === 'fail') {
                    return Promise.resolve(new Response(
                        JSON.stringify({detail: 'Project approvals are paused'}),
                        {status: 422, headers: {'Content-Type': 'application/json'}}));
                }
                return Promise.resolve(new Response(
                    JSON.stringify({items: [{title: 'Onboarding', description: 'From preview', priority: 5}]}),
                    {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return originalFetch(input, init);
        };
        """
    )
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")

    inp = page.locator("#chat-task-input")
    inp.fill("Plan the onboarding flow")
    inp.press("Enter")
    expect(page.locator("#chat-plan-error")).to_contain_text("Project approvals are paused")
    # The entered text is retained so the request can be retried.
    expect(inp).to_have_value("Plan the onboarding flow")

    page.evaluate("window.__planMode = 'ok'")
    inp.press("Enter")
    expect(page.locator("#chat-plan-error")).to_be_hidden()
    expect(page.locator("#plan-preview-modal")).to_be_visible()
    expect(inp).to_have_value("Plan the onboarding flow")
    assert page.evaluate("window.__planCalls") == 2


def test_mobile_navigation_reachable_on_phone_width(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script("localStorage.setItem('sidebar', 'collapsed')")
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    expect(page.locator("html")).to_have_attribute("data-sidebar", "collapsed")

    toggle = page.locator("#mobile-nav-toggle")
    expect(toggle).to_be_visible()
    nav_projects = page.locator(".sidebar-nav a[href='/ui/projects']")
    expect(nav_projects).to_be_hidden()
    toggle.focus()
    page.keyboard.press("Shift+Tab")
    assert not page.evaluate(
        "document.querySelector('#app-sidebar').contains(document.activeElement)"
    ), "Closed drawer must not receive keyboard focus"

    toggle.click()
    expect(nav_projects).to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "true")

    # Mobile drawer must have full width and unclipped labels even when the
    # saved desktop sidebar setting is collapsed.
    sidebar = page.locator("#app-sidebar")
    expect(sidebar).to_have_css("width", "260px")
    sidebar_box = sidebar.bounding_box()
    assert sidebar_box is not None
    assert sidebar_box["width"] >= 240, f"Drawer too narrow: {sidebar_box['width']}px"

    nav_text = nav_projects.locator(".nav-text")
    expect(nav_text).to_be_visible()
    text_box = nav_text.bounding_box()
    assert text_box is not None
    assert text_box["x"] + text_box["width"] <= sidebar_box["x"] + sidebar_box["width"], (
        "Navigation text overflows or clips inside drawer"
    )

    page.keyboard.press("Escape")
    expect(nav_projects).to_be_hidden()
    expect(toggle).to_have_attribute("aria-expanded", "false")

    toggle.click()
    nav_projects.click()
    assert page.url.endswith("/ui/projects")


def test_card_overflow_menu_focus_visibility_and_move_to_todo(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    card = page.locator(f"#task-card-{task_id}")

    # Task actions reveal for keyboard focus, not only pointer hover.
    actions = card.locator(".task-actions-revamp")
    more = card.locator(".task-menu-btn")
    assert actions.evaluate("el => parseFloat(getComputedStyle(el).opacity)") == 0
    more.focus()
    # The reveal animates over 0.2s; wait for the transition to finish.
    page.wait_for_timeout(300)
    assert actions.evaluate("el => parseFloat(getComputedStyle(el).opacity)") == 1

    # Destructive action sits in an accessible overflow menu.
    more.press("Enter")
    menu_item = card.locator(".task-menu-item")
    expect(menu_item).to_be_visible()
    expect(menu_item).to_have_text("Delete task")
    expect(more).to_have_attribute("aria-expanded", "true")
    page.keyboard.press("Escape")
    expect(menu_item).to_be_hidden()
    expect(more).to_be_focused()

    # The backlog action moves the card and says what it does.
    move_btn = card.get_by_role("button", name="Move to To Do")
    expect(move_btn).to_be_visible()
    move_btn.click()
    _complete_workflow_override(page, "Queue this card from its explicit backlog action.")
    expect(page.locator("#toast")).to_contain_text("Task is ready in To Do")
    todo_zone = page.locator(
        '.kanban-column-revamp[data-stage-key="to_do"] .kanban-drop-zone-revamp'
    )
    expect(todo_zone.locator(f"#task-card-{task_id}")).to_be_visible()

    # Deleting from the menu removes the card after confirmation.
    more.click()
    # The local task_moved WebSocket echo must not replace the open menu.
    page.wait_for_timeout(700)
    expect(menu_item).to_be_visible()
    menu_item.click()
    confirm = page.get_by_role("dialog", name="Delete task?")
    expect(confirm).to_be_visible()
    confirm.get_by_role("button", name="Delete task").click()
    expect(page.locator(f"#task-card-{task_id}")).to_have_count(0)


def test_new_task_modal_shows_destination_stage(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")

    page.locator("#new-task-btn").click()
    dialog = page.get_by_role("dialog", name="New task")
    expect(dialog).to_be_visible()
    stage = page.locator("#task-form-stage")
    expect(stage).to_be_visible()
    expect(stage).to_have_value(str(board["stages"]["Backlog"]))

    # Failures keep the entered values and explain the problem inline.
    page.route(
        "**/ui/tasks/create",
        lambda route: route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps({"detail": "Stage rejected this task"}),
        ),
    )
    page.locator("#task-form-title").fill("Created from the New task dialog")
    stage.select_option(str(board["stages"]["To Do"]))
    dialog.get_by_role("button", name="Create task").click()
    expect(page.locator("#task-form-error")).to_contain_text("Stage rejected this task")
    expect(page.locator("#task-form-title")).to_have_value(
        "Created from the New task dialog"
    )
    expect(dialog).to_be_visible()
    page.unroute("**/ui/tasks/create")

    dialog.get_by_role("button", name="Create task").click()
    expect(page.locator("#toast")).to_contain_text("Task created")
    todo_zone = page.locator(
        '.kanban-column-revamp[data-stage-key="to_do"] .kanban-drop-zone-revamp'
    )
    expect(
        todo_zone.locator(".task-title-revamp").filter(
            has_text="Created from the New task dialog"
        )
    ).to_be_visible()


def test_phase2_setup_checklist_and_navigation_e2e(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    pid = board["project_id"]
    page.goto(f"{live_server}/ui/projects/{pid}/board")

    # Consistent sub-nav is visible and has all 4 items
    subnav = page.locator(".project-sub-nav")
    expect(subnav).to_be_visible()
    expect(subnav.get_by_role("link", name="Board")).to_have_class("sub-nav-pill active")
    expect(subnav.get_by_role("link", name="Activity")).to_be_visible()
    expect(subnav.get_by_role("link", name="Changes")).to_be_visible()
    expect(subnav.get_by_role("link", name="Settings")).to_be_visible()

    # Checklist is visible on board
    checklist = page.locator("#project-setup-checklist")
    expect(checklist).to_be_visible()
    expect(checklist.locator("#step-choose-folder")).to_be_visible()
    expect(checklist.locator("#step-configure-worker")).to_be_visible()
    expect(checklist.locator("#step-create-task")).to_be_visible()
    expect(checklist.locator("#step-start-work")).to_be_visible()

    # Empty assignment dialog provides 'Configure agents' action
    page.route(
        "**/ui/api/roles",
        lambda route: route.fulfill(
            json={"roles": [], "candidates": [], "role_names": []}
        ),
    )
    page.evaluate(f"() => openAssignModal({board['task']['id']})")
    assign_dialog = page.get_by_role("dialog", name="Assign Role to Task")
    expect(assign_dialog).to_be_visible()
    config_btn = assign_dialog.get_by_role("button", name="Configure agents")
    expect(config_btn).to_be_visible()
    config_btn.click()
    expect(assign_dialog).to_be_hidden()
    expect(page.get_by_role("dialog", name="Global agent roles")).to_be_visible()
    page.unroute("**/ui/api/roles")
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog", name="Global agent roles")).to_be_hidden()

    # Navigation between project pages retains active indicator
    subnav.get_by_role("link", name="Activity").click()
    assert page.url.endswith(f"/ui/projects/{pid}/workbench")
    expect(page.locator(".project-sub-nav .sub-nav-pill.active")).to_contain_text("Activity")

    page.locator(".project-sub-nav").get_by_role("link", name="Changes").click()
    assert page.url.endswith(f"/ui/projects/{pid}/git")
    expect(page.locator(".project-sub-nav .sub-nav-pill.active")).to_contain_text("Changes")

    page.locator(".project-sub-nav").get_by_role("link", name="Settings").click()
    assert page.url.endswith(f"/ui/projects/{pid}/settings")
    expect(page.locator(".project-sub-nav .sub-nav-pill.active")).to_contain_text("Settings")

    # Settings Browse uses the same live folder API as the board.
    page.get_by_role("button", name="Browse").click()
    folder_dialog = page.get_by_role("dialog", name="Select Folder")
    expect(folder_dialog).to_be_visible()
    expect(folder_dialog.locator("#folder-picker-current")).not_to_have_value("")
    folder_dialog.get_by_role("button", name="Use This Folder").click()
    expect(folder_dialog).to_be_hidden()
    expect(page.locator("#settings-path")).not_to_have_value("")

    page.goto(f"{live_server}/ui/users")
    expect(page.get_by_role("heading", name="Agents & Roles")).to_be_visible()
    expect(page.get_by_role("columnheader", name="Current work")).to_be_visible()
    page.get_by_role("button", name="Configure roles").click()
    expect(page.locator("#role-editor-container")).to_be_visible()
    expect(page.locator("#team-list .role-settings-row").first).to_be_visible()




def test_project_card_and_folder_rows_navigate(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    project_id = board["project_id"]

    page.goto(f"{live_server}/ui/projects")
    card = page.locator(f"#project-card-{project_id}")
    card.locator("h3").click()
    page.wait_for_url(f"**/ui/projects/{project_id}/board")
    assert page.url.endswith(f"/ui/projects/{project_id}/board")

    folder_tree = {
        None: {
            "path": "/virtual",
            "parent": "/",
            "home": "/virtual",
            "folders": [{"name": "child", "path": "/virtual/child"}],
        },
        "/virtual/child": {
            "path": "/virtual/child",
            "parent": "/virtual",
            "home": "/virtual",
            "folders": [{"name": "grandchild", "path": "/virtual/child/grandchild"}],
        },
        "/virtual/child/grandchild": {
            "path": "/virtual/child/grandchild",
            "parent": "/virtual/child",
            "home": "/virtual",
            "folders": [],
        },
    }

    def serve_folders(route):
        requested = httpx.URL(route.request.url).params.get("path")
        route.fulfill(json=folder_tree.get(requested, folder_tree[None]))

    page.route("**/ui/api/folders*", serve_folders)

    def assert_nested_navigation(url: str, opener: str, dialog_name: str):
        page.goto(url)
        page.locator(opener).click()
        picker = page.get_by_role("dialog", name=dialog_name)
        expect(picker).to_be_visible()
        expect(picker.locator("#folder-picker-current")).to_have_value("/virtual")
        picker.locator("#folder-picker-list button").filter(has_text="child").click()
        expect(picker.locator("#folder-picker-current")).to_have_value("/virtual/child")
        picker.locator("#folder-picker-list button").filter(has_text="grandchild").click()
        expect(picker.locator("#folder-picker-current")).to_have_value(
            "/virtual/child/grandchild"
        )

    page.goto(f"{live_server}/ui/projects")
    page.get_by_role("button", name="New Project").click()
    create_dialog = page.get_by_role("dialog", name="Create Project")
    create_dialog.get_by_role("button", name="Browse").click()
    picker = page.get_by_role("dialog", name="Select Folder")
    expect(picker.locator("#folder-picker-current")).to_have_value("/virtual")
    picker.locator("#folder-picker-list button").filter(has_text="child").click()
    expect(picker.locator("#folder-picker-current")).to_have_value("/virtual/child")
    picker.locator("#folder-picker-list button").filter(has_text="grandchild").click()
    expect(picker.locator("#folder-picker-current")).to_have_value(
        "/virtual/child/grandchild"
    )

    assert_nested_navigation(
        f"{live_server}/ui/projects/{project_id}/board",
        "#step-choose-folder button",
        "Select Project Folder",
    )


def test_phase2_folder_picker_ignores_stale_browse_response(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    home_data = api.get("/ui/api/folders").json()
    target_path = home_data["parent"]
    assert target_path and target_path != home_data["path"]

    def check_picker(url: str, trigger: str, dialog_name: str, release_before_go: bool):
        page.goto(url)
        page.evaluate("""homeData => {
            const fetchOriginal = window.fetch.bind(window);
            window.fetch = (input, options) => {
                const url = typeof input === 'string' ? input : input.url;
                if (url === '/ui/api/folders') {
                    return new Promise(resolve => {
                        window.releaseOldFolderResponse = () => resolve(
                            new Response(JSON.stringify(homeData), {
                                status: 200,
                                headers: {'Content-Type': 'application/json'}
                            })
                        );
                    });
                }
                return fetchOriginal(input, options);
            };
        }""", home_data)
        page.locator(trigger).click()
        picker = page.get_by_role("dialog", name=dialog_name)
        expect(picker).to_be_visible()
        page.wait_for_function("typeof window.releaseOldFolderResponse === 'function'")
        if release_before_go:
            picker.locator("#folder-picker-current").focus()
            page.evaluate("window.releaseOldFolderResponse()")
            expect(picker.locator("#folder-picker-current")).to_have_value("")
        picker.locator("#folder-picker-current").fill(target_path)
        if release_before_go:
            page.evaluate("window.releaseOldFolderResponse()")
            expect(picker.locator("#folder-picker-current")).to_have_value(target_path)
        picker.get_by_role("button", name="Go").click()
        expect(picker.locator("#folder-picker-current")).to_have_value(target_path)
        if not release_before_go:
            page.evaluate("window.releaseOldFolderResponse()")
            expect(picker.locator("#folder-picker-current")).to_have_value(target_path)

    check_picker(
        f"{live_server}/ui/projects/{board['project_id']}/board",
        "#step-choose-folder button",
        "Select Project Folder",
        True,
    )
    check_picker(
        f"{live_server}/ui/projects/{board['project_id']}/settings",
        "button:has-text('Browse')",
        "Select Folder",
        False,
    )


def test_phase1_viewport_theme_walkthrough(
    page: Page, live_server: str, api: httpx.Client
):
    """Primary task actions and navigation remain reachable at supported widths."""
    board = _prepare_board(api)
    url = f"{live_server}/ui/projects/{board['project_id']}/board"
    page.add_init_script("localStorage.setItem('sidebar', 'collapsed')")
    for width in (1440, 1024, 768, 390):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(url)
        expect(page.locator("#new-task-btn")).to_be_visible()
        expect(page.locator("#chat-task-input")).to_be_visible()
        expect(page.locator("#chat-plan-btn")).to_be_visible()
        if width == 1024:
            page.evaluate("document.documentElement.setAttribute('data-sidebar', 'expanded')")
            assert page.locator("#chat-task-input").bounding_box()["width"] >= 160
        if width <= 992:
            toggle = page.locator("#mobile-nav-toggle")
            toggle.click()
            expect(page.locator("#app-sidebar")).to_have_css("width", "260px")
            sidebar_box = page.locator("#app-sidebar").bounding_box()
            assert sidebar_box is not None
            for label in page.locator(".sidebar-nav .nav-text").all():
                box = label.bounding_box()
                assert box is not None
                assert box["x"] + box["width"] <= sidebar_box["x"] + sidebar_box["width"] + 1
            page.keyboard.press("Escape")
            expect(page.locator("#sidebar-backdrop")).to_be_hidden()
        else:
            expect(page.locator(".sidebar-nav")).to_be_visible()

    page.set_viewport_size({"width": 390, "height": 844})
    for theme in ("light", "dark"):
        page.evaluate("(value) => localStorage.setItem('theme', value)", theme)
        page.goto(url)
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        expect(page.locator("#new-task-btn")).to_be_visible()
        expect(page.locator("#chat-plan-btn")).to_be_visible()
        expect(page.locator(f"#task-card-{board['task']['id']}")).to_be_visible()


def test_white_night_choices_keep_sidebar_readable(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    url = f"{live_server}/ui/projects/{board['project_id']}/board"
    page.set_viewport_size({"width": 1280, "height": 800})
    page.add_init_script(
        "if (!sessionStorage.getItem('seeded-theme')) { "
        "localStorage.setItem('theme', 'blue'); sessionStorage.setItem('seeded-theme', '1'); }"
    )
    page.goto(url)
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    assert page.evaluate("localStorage.getItem('theme')") == "dark"
    page.locator(".appearance-menu summary").click()
    choices = page.locator("[data-set-theme]")
    expect(choices).to_have_count(2)
    white = page.locator('[data-set-theme="light"]')
    night = page.locator('[data-set-theme="dark"]')
    expect(night).to_have_attribute("aria-pressed", "true")

    for button, theme, background in ((white, "light", [255, 255, 255]),
                                       (night, "dark", [11, 17, 32])):
        button.click()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(page.locator("#app-sidebar")).to_have_css("width", "260px")
        expect(page.locator(".content-area")).to_have_css("margin-left", "260px")
        expect(page.locator(".board-header-section")).to_have_css("border-radius", "24px")
        nav_link = page.locator(".sidebar-nav .nav-link:not(.active)").first
        expected_color = "rgb(51, 65, 85)" if theme == "light" else "rgb(203, 213, 225)"
        expect(nav_link).to_have_css("color", expected_color)
        ratio = nav_link.evaluate(
            r"""(link, background) => {
                const values = getComputedStyle(link).color.match(/[\d.]+/g).slice(0, 3).map(Number);
                const lum = channels => channels.map(value => {
                    const channel = value / 255;
                    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
                }).reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
                const text = lum(values), surface = lum(background);
                return (Math.max(text, surface) + 0.05) / (Math.min(text, surface) + 0.05);
            }""",
            background,
        )
        assert ratio >= 4.5, f"{theme} sidebar labels have contrast {ratio:.2f}:1"

    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.evaluate("localStorage.setItem('theme', 'rose')")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")


def test_phase1_controls_at_200_percent_phone_zoom_equivalent(
    page: Page, live_server: str, api: httpx.Client
):
    """Emulate 200% zoom with half the CSS viewport and twice the pixel density."""
    board = _prepare_board(api)
    page.set_viewport_size({"width": 390, "height": 900})
    cdp = page.context.new_cdp_session(page)
    cdp.send(
        "Emulation.setDeviceMetricsOverride",
        {"width": 195, "height": 450, "deviceScaleFactor": 2, "mobile": False},
    )
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    assert page.evaluate("innerWidth === 195 && devicePixelRatio === 2")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    for selector in (
        "#mobile-nav-toggle", ".appearance-menu summary",
        "#new-task-btn", "#chat-task-input", "#chat-plan-btn",
    ):
        box = page.locator(selector).bounding_box()
        assert box is not None
        assert box["x"] >= 0 and box["x"] + box["width"] <= 196, selector

    page.locator("#new-task-btn").click()
    expect(page.get_by_role("dialog", name="New task")).to_be_visible()
    for selector in ("#task-form-title", "#task-form-stage", "#task-form-submit"):
        box = page.locator(selector).bounding_box()
        assert box is not None
        assert box["x"] >= 0 and box["x"] + box["width"] <= 196, selector


def test_mobile_drawer_closes_when_resized_to_desktop(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    page.locator("#mobile-nav-toggle").click()
    expect(page.locator("#sidebar-backdrop")).to_be_visible()

    page.set_viewport_size({"width": 1024, "height": 844})
    expect(page.locator("html")).to_have_attribute("data-sidebar-open", "false")
    expect(page.locator("#sidebar-backdrop")).to_be_hidden()
    page.locator("#new-task-btn").click()
    expect(page.get_by_role("dialog", name="New task")).to_be_visible()


def test_initial_socket_connection_keeps_rendered_card(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    card = page.locator(f"#task-card-{board['task']['id']}")
    original = card.element_handle()
    expect(page.locator("#board-connection-status")).to_have_text("Live")
    assert original is not None and original.evaluate("element => element.isConnected")


def test_board_refreshes_after_websocket_reconnect(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    sockets = []
    page.route_web_socket("**/ws/projects/**", lambda socket: sockets.append(socket))
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    deadline = time.monotonic() + 5
    while not sockets and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert sockets, "Board did not open its WebSocket"

    sockets[0].close()
    api.patch(
        f"/ui/tasks/{board['task']['id']}/move",
        json={"stage_id": board["stages"]["To Do"], "status": "pending", "override_reason": "Exercise WebSocket reconnection after a manual move."},
        headers=board["headers"],
    ).raise_for_status()
    expect(page.locator(f"#task-card-{board['task']['id']}")).to_have_attribute(
        "data-current-stage", str(board["stages"]["To Do"]), timeout=7000
    )
    assert len(sockets) >= 2
    expect(page.locator("#board-connection-status")).to_have_text("Live", timeout=7000)


def test_task_actions_are_usable_on_touch(
    browser: Browser, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    try:
        page = context.new_page()
        page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
        card = page.locator(f"#task-card-{board['task']['id']}")
        card.scroll_into_view_if_needed()
        assert page.evaluate("matchMedia('(hover: none)').matches")
        assert card.locator(".task-actions-revamp").evaluate(
            "el => getComputedStyle(el).opacity"
        ) == "1"
        card.locator(".task-menu-btn").tap()
        expect(card.locator(".task-menu-item")).to_be_visible()
    finally:
        context.close()


def test_phase3_task_panel_deep_link_focus_and_stage_control(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    url = f"{live_server}/ui/projects/{board['project_id']}/board"
    page.goto(url)

    card = page.locator(f"#task-card-{task_id}")
    card.focus()
    card.press("Enter")
    panel = page.locator("#task-detail-panel")
    expect(panel).to_be_visible()
    expect(panel.locator("#task-panel-title")).to_have_text(
        card.locator(".task-title-revamp").inner_text()
    )
    expect(panel.get_by_role("tab", name="Overview")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(panel.locator(".task-overview-description")).to_contain_text(
        "Canonical description from the API"
    )
    assert page.url.endswith(f"?task={task_id}")
    panel.get_by_role("tab", name="Logs").click()
    expect(panel.get_by_role("tab", name="Logs")).to_have_attribute(
        "aria-selected", "true"
    )
    panel.get_by_role("tab", name="Overview").click()
    panel.get_by_role("combobox", name="Task stage").select_option(
        str(board["stages"]["To Do"])
    )
    _complete_workflow_override(page, "Queue this card from the task detail panel.")
    expect(page.locator(
        f'.kanban-column-revamp[data-stage-name="To Do"] #task-card-{task_id}'
    )).to_be_visible()
    expect(panel).to_be_visible()
    page.go_back()
    expect(panel).to_be_hidden()
    expect(card).to_be_focused()

    page.goto(url + f"?task={task_id}")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name="Close task details").click()
    expect(panel).to_be_hidden()
    assert "?task=" not in page.url


def test_phase3_edit_draft_survives_refresh_and_conflict(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    page.locator(f"#task-card-{task_id}").focus()
    page.locator(f"#task-card-{task_id}").press("Enter")
    panel = page.locator("#task-detail-panel")
    expect(panel).to_be_visible()
    panel.get_by_role("button", name="Edit").click()
    dialog = page.get_by_role("dialog", name=f"Edit Task #{task_id}")
    title = dialog.locator("#task-form-title")
    expect(title).to_have_value(board["task"]["title"])
    title.fill("My unsaved draft")

    changed = api.patch(
        f"/ui/tasks/{task_id}/edit",
        json={"title": "Updated by another user"},
        headers=board["headers"],
    )
    changed.raise_for_status()
    page.evaluate("refreshBoardFromServer()")
    expect(title).to_have_value("My unsaved draft")
    expect(dialog.locator("#task-form-error")).to_contain_text(
        "changed while you were editing"
    )
    dialog.get_by_role("button", name="Save changes").click()
    expect(dialog.locator("#task-form-error")).to_contain_text(
        "changed while you were editing"
    )
    expect(title).to_have_value("My unsaved draft")
    expect(panel).to_be_visible()


def test_phase3_named_priority_preserves_existing_numeric_value(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    page.locator(f"#task-card-{task_id}").get_by_role(
        "button", name=f"Edit task {task_id}", exact=True
    ).click()
    dialog = page.get_by_role("dialog", name=f"Edit Task #{task_id}")
    priority = dialog.locator("#task-form-priority")
    expect(priority).to_have_value("4")
    expect(priority.locator("option:checked")).to_contain_text("Normal (4)")
    dialog.locator("#task-form-title").fill("Updated without changing priority")
    dialog.get_by_role("button", name="Save changes").click()
    expect(dialog).to_be_hidden()
    assert api.get(f"/tasks/{task_id}").json()["priority"] == 4

    page.locator(f"#task-card-{task_id}").get_by_role(
        "button", name=f"Edit task {task_id}", exact=True
    ).click()
    priority.select_option("8")
    dialog.get_by_role("button", name="Save changes").click()
    expect(dialog).to_be_hidden()
    assert api.get(f"/tasks/{task_id}").json()["priority"] == 8



def test_phase4_board_search_filters_and_refresh(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    created = api.post(
        "/ui/tasks/create",
        json={"project_id": board["project_id"], "stage_id": board["stages"]["Backlog"],
              "title": "Urgent second task", "priority": 9, "status": "pending"},
        headers=board["headers"],
    )
    created.raise_for_status()
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    cards = page.locator(".kanban-task-revamp")
    expect(cards).to_have_count(2)
    page.locator("#board-search").fill("Urgent second")
    expect(page.locator(".kanban-task-revamp:visible")).to_have_count(1)
    expect(page.locator("#board-match-count")).to_have_text("1 of 2 tasks")
    page.locator("#board-search").fill("#" + str(board["task"]["id"]))
    expect(page.locator(".kanban-task-revamp:visible")).to_have_count(1)
    page.locator("#board-search").fill("")
    page.locator("#board-priority-filter").select_option("urgent")
    expect(page.locator(".kanban-task-revamp:visible")).to_have_count(1)
    page.evaluate("refreshBoardFromServer()")
    expect(page.locator(".kanban-task-revamp:visible")).to_have_count(1)
    page.locator("#board-filter").select_option("running")
    expect(page.locator("#board-filter-empty")).to_be_visible()
    expect(page.locator("#board-match-count")).to_have_text("0 of 2 tasks")
    page.locator("#board-clear-filters").click()
    expect(page.locator(".kanban-task-revamp:visible")).to_have_count(2)
    expect(page.locator("#board-filter-empty")).to_be_hidden()


def test_phase5_plan_preview_cancel_and_selected_commit_once(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    input_box = page.locator("#chat-task-input")
    input_box.fill("Build a small onboarding flow")
    input_box.press("Enter")
    preview = page.locator("#plan-preview-modal")
    expect(preview).to_be_visible()
    expect(preview.locator(".plan-preview-row")).to_have_count(4)
    expect(page.locator(".kanban-task-revamp")).to_have_count(1)
    preview.get_by_role("button", name="Cancel").click()
    expect(preview).to_be_hidden()
    expect(page.locator(".kanban-task-revamp")).to_have_count(1)
    expect(input_box).to_have_value("Build a small onboarding flow")

    input_box.press("Enter")
    expect(preview).to_be_visible()
    rows = preview.locator(".plan-preview-row")
    rows.nth(0).locator(".plan-title").fill("Only selected proposal")
    rows.nth(1).get_by_role("button", name="Remove proposal").click()
    for index in (1, 2):
        rows.nth(index).locator(".plan-include").uncheck()
    expect(preview.locator("#plan-preview-count")).to_have_text("1 selected task")
    preview.locator("#plan-preview-create").dispatch_event("click")
    preview.locator("#plan-preview-create").dispatch_event("click")
    expect(preview).to_be_hidden()
    expect(page.locator(".kanban-task-revamp")).to_have_count(2)
    expect(page.locator(".kanban-task-revamp").filter(has_text="Only selected proposal")).to_have_count(1)
    expect(input_box).to_have_value("")


def test_terminal_focus_keeps_raw_output_and_task_link(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    agent_response = api.post(
        "/entities/register/agent",
        json={"name": f"terminal-agent-{uuid.uuid4().hex[:8]}", "entity_type": "agent"},
        headers=board["headers"],
    )
    agent_response.raise_for_status()
    agent_id = agent_response.json()["id"]
    agent_headers = {"X-Entity-ID": str(agent_id)}
    session_response = api.post(
        f"/agents/{agent_id}/sessions",
        json={
            "project_id": board["project_id"],
            "task_id": board["task"]["id"],
            "workspace_path": "/tmp/terminal-browser-test",
            "command": "agent --work",
        },
        headers=agent_headers,
    )
    session_response.raise_for_status()
    session_id = session_response.json()["id"]
    pane = "\n".join(
        ["╭─────────╮", "│ Implementing parser │", "╰─────────╯", "Type your message"]
        + [f"Output line {number}" for number in range(80)]
        + ["Implementing parser", "Tests passed"]
    )
    activity_response = api.post(
        f"/agents/{agent_id}/activity",
        json={
            "project_id": board["project_id"],
            "task_id": board["task"]["id"],
            "session_id": session_id,
            "activity_type": "observation",
            "source": "tmux_pane",
            "message": pane,
        },
        headers=agent_headers,
    )
    activity_response.raise_for_status()

    task_id = board["task"]["id"]
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/workbench#terminal:task:{task_id}")
    viewer = page.locator("#wb-terminal-pre")
    expect(page.locator("#wb-pane-sessions")).to_have_class("wb-pane active")
    expect(page.locator("#wb-terminal-info")).to_contain_text(f"Task #{task_id}")
    expect(viewer).to_contain_text("Tests passed")
    expect(viewer).not_to_contain_text("Type your message")
    expect(viewer).not_to_contain_text("╭─────────╮")
    assert viewer.text_content().count("Implementing parser") == 1
    expect(page.locator("#wb-terminal-focused")).to_have_attribute("aria-pressed", "true")

    page.locator("#wb-terminal-raw").click()
    expect(viewer).to_contain_text("Type your message")
    expect(viewer).to_contain_text("╭─────────╮")
    expect(page.locator("#wb-terminal-raw")).to_have_attribute("aria-pressed", "true")
    page.evaluate("document.getElementById('wb-terminal-pre').scrollTop = 100")
    before = page.evaluate("document.getElementById('wb-terminal-pre').scrollTop")
    assert before > 0

    update = api.post(
        f"/agents/{agent_id}/activity",
        json={
            "project_id": board["project_id"],
            "task_id": task_id,
            "session_id": session_id,
            "activity_type": "observation",
            "source": "tmux_pane",
            "message": "New output after refresh",
        },
        headers=agent_headers,
    )
    update.raise_for_status()
    page.evaluate("refreshWbTerminal()")
    expect(viewer).to_contain_text("New output after refresh")
    after = page.evaluate("document.getElementById('wb-terminal-pre').scrollTop")
    assert abs(after - before) < 2

    page.goto(f"{live_server}/ui/projects/{board['project_id']}/board")
    card = page.locator(f"#task-card-{task_id}")
    card.focus()
    card.press("Enter")
    panel = page.locator("#task-detail-panel")
    panel.get_by_role("tab", name="Terminal").click()
    preview = panel.locator(f"#task-terminal-pre-{task_id}")
    expect(preview).to_contain_text("Tests passed")
    expect(preview).not_to_contain_text("Type your message")
    live_update = api.post(
        f"/agents/{agent_id}/activity",
        json={
            "project_id": board["project_id"],
            "task_id": task_id,
            "session_id": session_id,
            "activity_type": "observation",
            "source": "tmux_pane",
            "message": "Live agent update",
        },
        headers=agent_headers,
    )
    live_update.raise_for_status()
    expect(preview).to_contain_text("Live agent update")



def test_activity_handoff_refresh_ignores_previous_session_response(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    sessions = [
        {"id": session_id, "agent_id": session_id, "status": "active"}
        for session_id in (901, 902)
    ]
    page.route("**/agents/sessions?**", lambda route: route.fulfill(json=sessions))
    for session in sessions:
        page.route(
            f"**/agents/sessions/{session['id']}/terminal?**",
            lambda route, request, item=session: route.fulfill(json={"session": item, "activities": []}),
        )
    delayed = []
    page.route("**/agents/sessions/901/handoff", lambda route: delayed.append(route))
    handoff = {"exists": False, "durable": {"summary": "Second session handoff"}}
    page.route("**/agents/sessions/902/handoff", lambda route: route.fulfill(json=handoff))
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/workbench")
    items = page.locator("#wb-terminal-session-list .terminal-session-item")
    expect(items).to_have_count(2)
    items.nth(0).click()
    expect(page.locator("#wb-handoff-view")).to_contain_text("Loading handoff for session #901")
    items.nth(1).click()
    handoff_view = page.locator("#wb-handoff-view")
    expect(handoff_view).to_contain_text("Second session handoff")
    assert delayed
    with page.expect_response("**/agents/sessions/901/handoff") as old_response:
        delayed[0].fulfill(json={"exists": False, "durable": {"summary": "Stale first session"}})
    old_response.value.finished()
    expect(handoff_view).to_contain_text("Second session handoff")

    handoff["durable"]["summary"] = "Updated second session handoff"
    page.get_by_role("button", name="Refresh output", exact=True).click()
    expect(handoff_view).to_contain_text("Updated second session handoff")
    expect(handoff_view).not_to_contain_text("Stale first session")


def test_reviews_tab_displays_task_git_diff_by_file(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    project_id = board["project_id"]
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
        "-old\n+new\n"
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n+++ b/src/app.py\n@@ -0,0 +1 @@\n"
        "+<img src=x onerror=alert(1)>\n"
    )
    page.route(
        f"**/agents/projects/{project_id}/tasks/{task_id}/git-diff",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"source": "worktree", "base_ref": "main", "branch": "kanban/task",
                             "diff": patch, "truncated": False}),
        ),
    )
    page.goto(f"{live_server}/ui/projects/{project_id}/board")
    card = page.locator(f"#task-card-{task_id}")
    card.focus()
    card.press("Enter")
    panel = page.locator("#task-detail-panel")
    panel.get_by_role("tab", name="Reviews").click()
    expect(panel.locator(".task-diff-file")).to_have_count(2)
    expect(panel.locator(".task-diff-file").first).to_contain_text("README.md")
    expect(panel.locator(".task-diff-line.added").first).to_have_text("+new")
    assert panel.locator("img[src=x]").count() == 0
    panel.locator(".task-diff-file").nth(1).locator("summary").click()
    expect(panel.locator(".task-diff-file").nth(1)).to_contain_text("<img src=x onerror=alert(1)>")



def test_reviews_tab_can_approve_and_reject_saved_patches(
    page: Page, live_server: str, api: httpx.Client
):
    board = _prepare_board(api)
    task_id = board["task"]["id"]
    project_id = board["project_id"]

    def create_review(label: str) -> int:
        response = api.post(
            f"/agents/projects/{project_id}/diff-reviews",
            json={
                "project_id": project_id,
                "task_id": task_id,
                "diff_content": f"diff --git a/{label}.txt b/{label}.txt\n+++ b/{label}.txt\n+{label}\n",
                "summary": f"{label} patch",
            },
            headers=board["headers"],
        )
        response.raise_for_status()
        return response.json()["id"]

    approve_id = create_review("approve")
    page.goto(f"{live_server}/ui/projects/{project_id}/board?task={task_id}")
    panel = page.locator("#task-detail-panel")
    panel.get_by_role("tab", name="Reviews").click()
    approved_card = panel.locator(f"#task-review-{approve_id}")
    expect(approved_card.get_by_role("button", name="Approve")).to_be_visible()
    approved_card.locator(".task-review-snapshot > summary").click()
    expect(approved_card.locator(".task-diff-line.added")).to_have_text("+approve")
    approved_card.locator(".task-review-note").fill("Looks ready")
    approved_card.get_by_role("button", name="Approve").click()
    expect(approved_card.locator(".task-review-status")).to_have_text("approved")
    expect(approved_card.get_by_role("button", name="Approve")).to_have_count(0)

    reject_id = create_review("reject")
    page.evaluate(f"fetchTaskReviews({task_id})")
    rejected_card = panel.locator(f"#task-review-{reject_id}")
    expect(rejected_card.get_by_role("button", name="Reject")).to_be_visible()
    rejected_card.locator(".task-review-note").fill("Needs revision")
    rejected_card.get_by_role("button", name="Reject").click()
    expect(rejected_card.locator(".task-review-status")).to_have_text("rejected")
    expect(rejected_card).to_contain_text("Needs revision")

    reviews = api.get(
        f"/agents/projects/{project_id}/diff-reviews?task_id={task_id}",
        headers=board["headers"],
    )
    reviews.raise_for_status()
    statuses = {review["id"]: review["status"] for review in reviews.json()}
    assert statuses[approve_id] == "approved"
    assert statuses[reject_id] == "rejected"



def test_launch_queue_controls_cancel_and_retry(page: Page, live_server: str, api: httpx.Client):
    board = _prepare_board(api)
    state = {
        "id": 901, "event_id": 801, "actor_id": board["owner_id"],
        "task_id": board["task"]["id"], "agent_id": 77, "role": "worker",
        "source_session_id": None, "stage_id": board["stages"]["Backlog"],
        "status": "blocked", "attempts": 3, "retry_at": None,
        "last_error": "<img src=x onerror=alert(1)> unavailable",
        "session_id": None, "created_at": "2026-09-19T00:00:00",
        "archived_at": None,
    }
    actions = []

    def queue_api(route):
        request = route.request
        if request.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps([state]))
            return
        if request.url.endswith("/cancel"):
            state["status"] = "cancelled"
            state["last_error"] = "Cancelled from workbench"
            actions.append("cancel")
        elif request.url.endswith("/retry"):
            state["status"] = "queued"
            state["last_error"] = "Retried from workbench"
            actions.append("retry")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(state))

    page.route("**/agents/launch-requests?*", queue_api)
    page.route("**/agents/launch-requests/**", queue_api)
    page.goto(f"{live_server}/ui/projects/{board['project_id']}/workbench")
    page.get_by_role("button", name="Launch queue").click()

    row = page.locator("#wb-queue-901")
    expect(row).to_contain_text("Task #" + str(board["task"]["id"]))
    expect(row).to_contain_text("blocked")
    expect(row.get_by_role("button", name="Cancel")).to_be_visible()
    expect(row.get_by_role("button", name="Retry")).to_be_visible()
    assert row.locator("img").count() == 0

    row.get_by_role("button", name="Cancel").click()
    expect(row).to_contain_text("cancelled")
    row.get_by_role("button", name="Retry").click()
    expect(row).to_contain_text("queued")
    assert actions == ["cancel", "retry"]
