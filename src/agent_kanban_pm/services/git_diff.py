"""Read-only Git diff snapshots for a task's isolated workspace."""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from agent_kanban_pm.runtime.assignment_launcher import _detect_base_ref


MAX_DIFF_CHARS = 500_000
MAX_UNTRACKED_BYTES = 100_000


def _git(path: Path, *args: str, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(path), *args]
    try:
        return subprocess.run(
            command, capture_output=True, text=True, errors="replace",
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess(command, 1, "", "")


def _common_git_dir(path: Path) -> Path | None:
    result = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def _untracked_patch(workspace: Path, relative_path: str) -> str:
    file = workspace / relative_path
    # Never follow a symlink outside the task workspace when generating a preview.
    try:
        if not file.is_file() or not file.resolve().is_relative_to(workspace.resolve()):
            return f"diff --git a/{relative_path} b/{relative_path}\nUntracked file (preview unavailable)\n"
        if file.stat().st_size > MAX_UNTRACKED_BYTES:
            return f"diff --git a/{relative_path} b/{relative_path}\nUntracked file (too large to preview)\n"
        contents = file.read_bytes()
    except OSError:
        return f"diff --git a/{relative_path} b/{relative_path}\nUntracked file changed while reading\n"
    if b"\0" in contents:
        return f"diff --git a/{relative_path} b/{relative_path}\nUntracked binary file\n"
    lines = contents.decode("utf-8", errors="replace").splitlines(keepends=True)
    patch = "".join(difflib.unified_diff([], lines, fromfile="/dev/null", tofile=f"b/{relative_path}"))
    return f"diff --git a/{relative_path} b/{relative_path}\nnew file mode 100644\n{patch}"


def read_task_git_diff(
    project_path: str,
    workspace_path: str,
    branch: str | None = None,
    base_revision: str | None = None,
) -> dict | None:
    """Return changes from the task base, including staged, unstaged and untracked files.

    A missing worktree can still expose committed task changes through its branch.
    Never read a workspace whose Git repository differs from the configured project.
    """
    project = Path(project_path).resolve()
    workspace = Path(workspace_path).resolve()
    if not project.is_dir() or _common_git_dir(project) is None:
        return None
    project_git_dir = _common_git_dir(project)
    live_worktree = (
        workspace != project and workspace.is_dir()
        and _common_git_dir(workspace) == project_git_dir
        and _git(workspace, "rev-parse", "--show-toplevel").stdout.strip() == str(workspace)
    )
    source = "worktree" if live_worktree else "branch"
    if not live_worktree and not branch:
        return None
    repo = workspace if live_worktree else project
    if not live_worktree:
        verified = _git(project, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if verified.returncode != 0:
            return None
    head = "HEAD" if live_worktree else f"refs/heads/{branch}"
    base_ref = _detect_base_ref(str(project), "git")
    base = base_revision
    if base is not None:
        verified_base = _git(repo, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
        if verified_base.returncode != 0:
            return None
        base = verified_base.stdout.strip()
    elif base_ref:
        merge_base = _git(repo, "merge-base", base_ref, head)
        if merge_base.returncode == 0 and merge_base.stdout.strip():
            base = merge_base.stdout.strip()
    else:
        base = head
    target = None if live_worktree else head
    command = ["diff", "--no-ext-diff", "--no-color", "--find-renames", base]
    if target:
        command.append(target)
    command.extend(["--", ".", ":(exclude)STATUS.md"])
    result = _git(repo, *command)
    if result.returncode != 0:
        return None
    patch = result.stdout
    if live_worktree:
        untracked = _git(repo, "ls-files", "--others", "--exclude-standard", "-z")
        if untracked.returncode == 0:
            for relative_path in untracked.stdout.split("\0"):
                if not relative_path or relative_path == "STATUS.md":
                    continue
                patch += _untracked_patch(repo, relative_path)
                if len(patch) > MAX_DIFF_CHARS:
                    break
    truncated = len(patch) > MAX_DIFF_CHARS
    if truncated:
        patch = patch[:MAX_DIFF_CHARS] + "\n… Diff truncated. Open the workspace in Git for the full patch.\n"
    return {
        "source": source,
        "base_ref": base_ref or "HEAD",
        "base_revision": base,
        "branch": branch if source == "branch" else _git(repo, "branch", "--show-current").stdout.strip(),
        "diff": patch,
        "truncated": truncated,
        "message": (
            "No committed changes on this task branch. Uncommitted changes from a removed worktree are unavailable."
            if source == "branch" and not patch else None
        ),
    }
