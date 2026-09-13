"""Isolated browser-to-CLI smoke test for first-time project setup."""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect, sync_playwright  # noqa: E402


pytestmark = pytest.mark.e2e
_ROOT = Path(__file__).resolve().parents[2]


def test_first_task_launches_from_setup_checklist(tmp_path: Path):
    real_tmux = shutil.which("tmux")
    if not real_tmux:
        pytest.skip("tmux is needed for the isolated CLI smoke test")

    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    bin_dir = tmp_path / "bin"
    for directory in (home, workspace, bin_dir):
        directory.mkdir()
    marker = tmp_path / "worker-started.txt"
    socket_name = "phase2-smoke-" + uuid.uuid4().hex[:10]

    # The wrapper gives this test its own tmux server; cleanup cannot touch
    # existing local agent sessions.
    tmux_wrapper = bin_dir / "tmux"
    tmux_wrapper.write_text(
        f'#!/bin/sh\nexec "{real_tmux}" -L {socket_name} "$@"\n',
        encoding="utf-8",
    )
    tmux_wrapper.chmod(0o755)
    worker = bin_dir / "claude"
    worker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$KANBAN_AGENT_NAME" > "$PHASE2_MARKER"\nsleep 45\n',
        encoding="utf-8",
    )
    worker.chmod(0o755)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "smoke.db"
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
        "HOME": str(home),
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
        "PHASE2_MARKER": str(marker),
        "KANBAN_TESTING": "1",
        "KANBAN_REGISTER_ALL_ADAPTERS": "0",
        "KANBAN_ACTIVE_WORKSPACE": str(workspace),
        "KANBAN_API_BASE": base_url,
        "PYTHONPATH": str(_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", ""),
    })

    log_path = tmp_path / "server.log"
    with log_path.open("w+", encoding="utf-8") as log:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "agent_kanban_pm.app:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as api:
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    assert server.poll() is None, "Smoke server exited during startup"
                    try:
                        if api.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        time.sleep(0.15)
                else:
                    pytest.fail("Smoke server did not become healthy")

                owner = api.get("/entities/me")
                owner.raise_for_status()
                headers = {"X-Entity-ID": str(owner.json()["id"])}
                project = api.post(
                    "/projects", json={"name": "Isolated Phase 2 UI smoke"}, headers=headers,
                )
                project.raise_for_status()
                project_id = project.json()["id"]
                api.post(f"/projects/{project_id}/approve", json={}, headers=headers).raise_for_status()

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    page = browser.new_page()
                    page.set_default_timeout(10000)
                    page.goto(f"{base_url}/ui/projects/{project_id}/board")
                    checklist = page.locator("#project-setup-checklist")

                    checklist.locator("#step-choose-folder").get_by_role(
                        "button", name="Choose folder",
                    ).click()
                    picker = page.get_by_role("dialog", name="Select Project Folder")
                    expect(picker).to_be_visible()
                    picker.locator("#folder-picker-current").fill(str(workspace))
                    picker.get_by_role("button", name="Go").click()
                    expect(picker.locator("#folder-picker-current")).to_have_value(str(workspace))
                    picker.get_by_role("button", name="Use This Folder").click()
                    page.get_by_role("dialog", name="Edit Project").get_by_role(
                        "button", name="Save",
                    ).click()
                    expect(checklist.locator("#step-choose-folder")).to_have_class(
                        "setup-step-item step-complete",
                    )

                    checklist.locator("#step-configure-worker").get_by_role(
                        "button", name="Configure agents",
                    ).click()
                    team = page.get_by_role("dialog", name="Team roles")
                    expect(team).to_be_visible()
                    worker_row = page.locator(".role-settings-row").filter(
                        has=page.locator("#role-agent-worker"),
                    )
                    worker_row.locator("#role-agent-worker").select_option("claude")
                    worker_row.get_by_role("button", name="Save role").click()
                    expect(worker_row.get_by_role("status")).to_have_text("Saved")
                    team.get_by_role("button", name="Close team dialog").click()
                    expect(checklist.locator("#step-configure-worker")).to_have_class(
                        "setup-step-item step-complete",
                    )

                    checklist.locator("#step-create-task").get_by_role(
                        "button", name="New task",
                    ).click()
                    task_dialog = page.get_by_role("dialog", name="New task")
                    expect(task_dialog).to_be_visible()
                    task_dialog.locator("#task-form-title").fill("Smoke worker task")
                    task_dialog.get_by_role("button", name="Create task").click()
                    expect(checklist.locator("#step-create-task")).to_have_class(
                        "setup-step-item step-complete",
                    )

                    checklist.locator("#step-start-work").get_by_role(
                        "button", name="Move to To Do",
                    ).click()
                    assign = checklist.locator("#step-start-work").get_by_role(
                        "button", name="Assign worker",
                    )
                    expect(assign).to_be_visible()
                    assign.click()
                    expect(checklist.locator("#step-start-work")).to_have_class(
                        "setup-step-item step-complete", timeout=20000,
                    )
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline and not marker.exists():
                        time.sleep(0.1)
                    assert marker.read_text(encoding="utf-8").strip() == "claude"
                    with sqlite3.connect(db_path) as db:
                        session = db.execute(
                            "SELECT status, task_id, workspace_path "
                            "FROM agent_sessions ORDER BY id DESC LIMIT 1"
                        ).fetchone()
                    assert session is not None
                    assert session[0] == "ACTIVE"
                    assert session[1] is not None
                    assert session[2] == str(workspace)
                    browser.close()
        except Exception:
            log.flush()
            print("Smoke server log tail:\n" + log_path.read_text(encoding="utf-8")[-4500:])
            raise
        finally:
            subprocess.run(
                [real_tmux, "-L", socket_name, "kill-server"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
