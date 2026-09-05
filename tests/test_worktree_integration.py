"""Tests for branch-per-task worktrees, base-ref detection, rebase sync, and
the auto-mode CLI flags in the bundled adapter YAMLs.

Most cases drive real git repos in tmp dirs rather than mocking subprocess —
the underlying logic is git-correctness-dependent.
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import tests_helper  # noqa: F401  — autouse cleanup listeners

from agent_kanban_pm.runtime.assignment_launcher import (
    AssignmentLauncher,
    _build_agent_command,
    _build_prompt,
    _create_git_worktree,
    _detect_base_ref,
    _sync_worktree_with_base,
    _task_branch_name,
)
from agent_kanban_pm.runtime.adapter_loader import load_adapter


GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git binary not available")

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "src" / "agent_kanban_pm" / "data" / "agents"


def _run(*args, cwd=None):
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


def _init_repo(path: Path, initial_branch: str = "main") -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(GIT, "init", "-b", initial_branch, str(path))
    _run(GIT, "-C", str(path), "config", "user.email", "test@test.local")
    _run(GIT, "-C", str(path), "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n")
    _run(GIT, "-C", str(path), "add", ".")
    _run(GIT, "-C", str(path), "commit", "-m", "init")


# ---------------------------------------------------------------------------
# Adapter YAML flag assertions — bypass/yolo flags must stay OUT of the
# default (supervised) invocation and live in task_command.auto_args, which
# the launcher only appends when a role opts into autonomy: auto.
# ---------------------------------------------------------------------------


def _adapter_args(name: str) -> list[str]:
    data = yaml.safe_load((ADAPTER_DIR / f"{name}.yaml").read_text())
    return data["task_command"]["args"]


def _adapter_auto_args(name: str) -> list[str]:
    data = yaml.safe_load((ADAPTER_DIR / f"{name}.yaml").read_text())
    return data["task_command"].get("auto_args") or []


def test_claude_adapter_uses_bypass_permissions():
    auto_args = _adapter_auto_args("claude")
    assert "--permission-mode" in auto_args
    assert auto_args[auto_args.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--permission-mode" not in _adapter_args("claude")


def test_gemini_adapter_uses_yolo_approval_mode():
    auto_args = _adapter_auto_args("gemini")
    assert "--approval-mode" in auto_args
    assert auto_args[auto_args.index("--approval-mode") + 1] == "yolo"
    assert "--approval-mode" not in _adapter_args("gemini")


def test_codex_adapter_uses_full_auto():
    auto_args = _adapter_auto_args("codex")
    assert "--full-auto" in auto_args
    assert "--ask-for-approval" not in auto_args
    assert "--full-auto" not in _adapter_args("codex")


def test_aider_adapter_uses_yes_always():
    auto_args = _adapter_auto_args("aider")
    assert "--yes-always" in auto_args
    assert "--yes-always" not in _adapter_args("aider")


# ---------------------------------------------------------------------------
# _task_branch_name
# ---------------------------------------------------------------------------


def test_task_branch_name_sanitises_agent_name():
    class FakeTask:
        id = 42

    class FakeAgent:
        name = "Claude Sonnet 4.6"

    assert _task_branch_name(FakeTask(), FakeAgent()) == "kanban/task-42-Claude-Sonnet-4.6"


# ---------------------------------------------------------------------------
# _detect_base_ref
# ---------------------------------------------------------------------------


def test_detect_base_ref_local_main(tmp_path):
    _init_repo(tmp_path, "main")
    assert _detect_base_ref(str(tmp_path), GIT) == "main"


def test_detect_base_ref_local_master(tmp_path):
    _init_repo(tmp_path, "master")
    assert _detect_base_ref(str(tmp_path), GIT) == "master"


def test_detect_base_ref_with_origin_remote(tmp_path):
    upstream = tmp_path / "upstream"
    _init_repo(upstream, "main")

    clone = tmp_path / "clone"
    _run(GIT, "clone", str(upstream), str(clone))

    assert _detect_base_ref(str(clone), GIT) == "origin/main"


def test_detect_base_ref_returns_none_for_unconventional_default(tmp_path):
    _run(GIT, "init", "-b", "trunk", str(tmp_path))
    _run(GIT, "-C", str(tmp_path), "config", "user.email", "t@t")
    _run(GIT, "-C", str(tmp_path), "config", "user.name", "T")
    (tmp_path / "f").write_text("x")
    _run(GIT, "-C", str(tmp_path), "add", ".")
    _run(GIT, "-C", str(tmp_path), "commit", "-m", "init")
    assert _detect_base_ref(str(tmp_path), GIT) is None


# ---------------------------------------------------------------------------
# _create_git_worktree
# ---------------------------------------------------------------------------


def test_create_git_worktree_creates_named_branch(tmp_path):
    project = tmp_path / "proj"
    _init_repo(project, "main")
    wt = tmp_path / "wt"

    result = _create_git_worktree(
        str(project), wt, branch_name="kanban/task-1-claude", base_ref="main"
    )
    assert result == str(wt)
    assert wt.exists()

    branch = _run(GIT, "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "kanban/task-1-claude"


def test_create_git_worktree_reuses_existing_path(tmp_path):
    project = tmp_path / "proj"
    _init_repo(project, "main")
    wt = tmp_path / "wt"

    first = _create_git_worktree(str(project), wt, branch_name="kanban/task-1-x", base_ref="main")
    second = _create_git_worktree(str(project), wt, branch_name="kanban/task-1-x", base_ref="main")
    assert first == second == str(wt)


def test_create_git_worktree_reuses_existing_branch_when_path_removed(tmp_path):
    project = tmp_path / "proj"
    _init_repo(project, "main")
    wt = tmp_path / "wt"

    assert _create_git_worktree(
        str(project), wt, branch_name="kanban/task-1-x", base_ref="main"
    ) == str(wt)
    _run(GIT, "-C", str(project), "worktree", "remove", str(wt), "--force")

    again = _create_git_worktree(
        str(project), wt, branch_name="kanban/task-1-x", base_ref="main"
    )
    assert again == str(wt)
    branch = _run(GIT, "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "kanban/task-1-x"


def test_create_git_worktree_detached_fallback_without_branch(tmp_path):
    project = tmp_path / "proj"
    _init_repo(project, "main")
    wt = tmp_path / "wt"

    assert _create_git_worktree(str(project), wt) == str(wt)
    branch = _run(GIT, "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "HEAD"  # detached


def test_create_git_worktree_returns_none_for_non_git_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "f").write_text("hi")
    assert _create_git_worktree(str(project), tmp_path / "wt") is None


# ---------------------------------------------------------------------------
# _sync_worktree_with_base
# ---------------------------------------------------------------------------


def test_sync_with_base_rebases_clean_worktree(tmp_path):
    project = tmp_path / "proj"
    _init_repo(project, "main")
    wt = tmp_path / "wt"
    _create_git_worktree(str(project), wt, branch_name="kanban/task-1-x", base_ref="main")

    (project / "new.txt").write_text("new on main")
    _run(GIT, "-C", str(project), "add", ".")
    _run(GIT, "-C", str(project), "commit", "-m", "main moves on")

    rebased, msg = _sync_worktree_with_base(str(wt), "main")
    assert rebased is True
    assert "rebased" in msg.lower()
    assert (wt / "new.txt").exists()


def test_sync_with_base_skips_dirty_worktree(tmp_path):
    project = tmp_path / "proj"
    _init_repo(project, "main")
    wt = tmp_path / "wt"
    _create_git_worktree(str(project), wt, branch_name="kanban/task-1-x", base_ref="main")

    (wt / "dirty.txt").write_text("uncommitted edit")

    rebased, msg = _sync_worktree_with_base(str(wt), "main")
    assert rebased is False
    assert "uncommitted" in msg.lower()


def test_sync_with_base_returns_false_when_no_base(tmp_path):
    rebased, msg = _sync_worktree_with_base(str(tmp_path), None)
    assert rebased is False
    assert "no base" in msg.lower()


def test_sync_with_base_aborts_on_conflict(tmp_path):
    project = tmp_path / "proj"
    _init_repo(project, "main")
    wt = tmp_path / "wt"
    _create_git_worktree(str(project), wt, branch_name="kanban/task-1-x", base_ref="main")

    (project / "README.md").write_text("CHANGED ON MAIN\n")
    _run(GIT, "-C", str(project), "add", ".")
    _run(GIT, "-C", str(project), "commit", "-m", "main edit")

    (wt / "README.md").write_text("CHANGED ON BRANCH\n")
    _run(GIT, "-C", str(wt), "add", ".")
    _run(GIT, "-C", str(wt), "commit", "-m", "branch edit")

    rebased, msg = _sync_worktree_with_base(str(wt), "main")
    assert rebased is False
    assert "conflict" in msg.lower()

    # Rebase was cleanly aborted: HEAD is back on the task branch.
    branch = _run(GIT, "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    assert branch == "kanban/task-1-x"


# ---------------------------------------------------------------------------
# Prompt + launcher contract (source-level — matches existing test style)
# ---------------------------------------------------------------------------


class _FakeTask:
    id = 7
    title = "Demo task"
    description = "Do the thing"
    logs = []
    comments = []


class _FakeProject:
    id = 3


class _FakeAgent:
    name = "claude"


def test_build_prompt_auto_mode_tells_agent_to_operate_autonomously():
    prompt = _build_prompt(
        _FakeTask(), _FakeProject(), _FakeAgent(), "/tmp/wt", autonomy="auto"
    )
    assert "Operate autonomously" in prompt
    assert "auto-approval" in prompt
    assert "stop to ask" in prompt  # "do not stop to ask…"
    assert "approval queue" not in prompt


def test_build_prompt_supervised_is_the_default():
    prompt = _build_prompt(_FakeTask(), _FakeProject(), _FakeAgent(), "/tmp/wt")
    assert "supervised" in prompt
    assert "approval queue" in prompt
    assert "Operate autonomously" not in prompt
    assert "auto-approval" not in prompt


def test_build_agent_command_supervised_omits_bypass_flags():
    spec = load_adapter(ADAPTER_DIR / "claude.yaml")
    cmd = _build_agent_command(spec, "/tmp/wt", "do it", autonomy="supervised")
    assert "--permission-mode" not in cmd
    assert "--print" in cmd
    assert "/tmp/wt" in cmd


def test_build_agent_command_auto_appends_bypass_flags():
    spec = load_adapter(ADAPTER_DIR / "claude.yaml")
    cmd = _build_agent_command(spec, "/tmp/wt", "do it", autonomy="auto")
    assert "--permission-mode" in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--print" in cmd
    assert "/tmp/wt" in cmd


def test_launch_for_assignment_wires_branch_and_sync():
    source = inspect.getsource(AssignmentLauncher._launch_for_assignment)
    assert "_detect_base_ref" in source
    assert "asyncio.to_thread" in source
    assert "_task_branch_name" in source
    assert "_sync_worktree_with_base" in source
    assert "branch_name=branch_name" in source
    assert "base_ref=base_ref" in source


@pytest.mark.asyncio
async def test_launch_admission_is_serialized(monkeypatch):
    launcher = AssignmentLauncher()
    running = 0
    peak = 0

    async def fake_launch(task_id, entity_id, assigned_role=None):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return task_id

    monkeypatch.setattr(launcher, "_launch_for_assignment", fake_launch)
    results = await asyncio.gather(
        launcher.launch_for_assignment(1, 10),
        launcher.launch_for_assignment(2, 20),
    )

    assert results == [1, 2]
    assert peak == 1
