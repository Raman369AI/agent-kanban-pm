"""Tests for branch-per-task worktrees, base-ref detection, rebase sync, and
the auto-mode CLI flags in the bundled adapter YAMLs.

Most cases drive real git repos in tmp dirs rather than mocking subprocess —
the underlying logic is git-correctness-dependent.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

import tests_helper  # noqa: F401  — autouse cleanup listeners

from kanban_runtime.assignment_launcher import (
    AssignmentLauncher,
    _build_prompt,
    _create_git_worktree,
    _detect_base_ref,
    _sync_worktree_with_base,
    _task_branch_name,
)


GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git binary not available")

ADAPTER_DIR = Path(__file__).resolve().parent.parent / "kanban_runtime" / "data" / "agents"


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
# Adapter YAML flag assertions — locks in the auto/non-interactive flags so a
# future edit cannot silently put an agent back into prompt-the-human mode.
# ---------------------------------------------------------------------------


def _adapter_args(name: str) -> list[str]:
    data = yaml.safe_load((ADAPTER_DIR / f"{name}.yaml").read_text())
    return data["task_command"]["args"]


def test_claude_adapter_uses_bypass_permissions():
    args = _adapter_args("claude")
    assert "--permission-mode" in args
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"


def test_gemini_adapter_uses_yolo_approval_mode():
    args = _adapter_args("gemini")
    assert "--approval-mode" in args
    assert args[args.index("--approval-mode") + 1] == "yolo"


def test_codex_adapter_uses_full_auto():
    args = _adapter_args("codex")
    assert "--full-auto" in args
    assert "--ask-for-approval" not in args


def test_aider_adapter_uses_yes_always():
    args = _adapter_args("aider")
    assert "--yes-always" in args


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


def test_build_prompt_tells_agent_to_operate_autonomously():
    source = inspect.getsource(_build_prompt)
    assert "Operate autonomously" in source
    assert "auto-approval" in source
    assert "stop to ask" in source  # "do not stop to ask…" — line-wrapped in source


def test_launch_for_assignment_wires_branch_and_sync():
    source = inspect.getsource(AssignmentLauncher.launch_for_assignment)
    assert "_detect_base_ref" in source
    assert "_task_branch_name" in source
    assert "_sync_worktree_with_base" in source
    assert "branch_name=branch_name" in source
    assert "base_ref=base_ref" in source
