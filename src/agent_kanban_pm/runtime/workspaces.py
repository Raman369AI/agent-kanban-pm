"""Ownership and revision checks for runtime-created Git workspaces."""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


class WorkspacePreparationError(RuntimeError):
    pass


def ownership_path(workspace: str | Path) -> Path:
    path = Path(workspace).resolve()
    return path.parent / (path.name + '.kanban-owned.json')


def record_ownership(project: str, workspace: str | Path) -> None:
    """Called only after this process successfully creates a new worktree."""
    ownership_path(workspace).write_text(json.dumps({
        'project': str(Path(project).resolve()),
        'workspace': str(Path(workspace).resolve()),
        'created_at': time.time(),
    }), encoding='utf-8')


def owns_workspace(project: str, workspace: str | Path, *, minimum_age: int = 0) -> bool:
    try:
        data = json.loads(ownership_path(workspace).read_text(encoding='utf-8'))
        return (
            data['project'] == str(Path(project).resolve())
            and data['workspace'] == str(Path(workspace).resolve())
            and time.time() - float(data['created_at']) >= minimum_age
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def git_revision(workspace: str) -> str | None:
    """Return a committed work product; reject uncommitted implementation files."""
    root = Path(workspace)
    if not (root / '.git').exists():
        return None
    def git(*args):
        result = subprocess.run(['git', '-C', workspace, *args], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise WorkspacePreparationError(result.stderr.strip() or 'Git inspection failed')
        return result.stdout.strip()
    # The runtime owns STATUS.md and instruction aliases; other untracked files
    # must be committed before another agent can review this revision.
    changed = git('diff', '--name-only', 'HEAD').splitlines()
    untracked = git('ls-files', '--others', '--exclude-standard').splitlines()
    runtime_files = {'STATUS.md'}
    for alias in ('CLAUDE.md', 'CODEX.md'):
        if (root / alias).is_symlink() and (root / alias).resolve() == (root / 'AGENTS.md').resolve():
            runtime_files.add(alias)
    if any(name not in runtime_files for name in changed + untracked):
        raise WorkspacePreparationError('Commit implementation changes before submitting the handoff.')
    return git('rev-parse', 'HEAD')
