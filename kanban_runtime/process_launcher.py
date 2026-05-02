"""Shared local process/tmux launch helpers."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Optional

logger = logging.getLogger(__name__)


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def tmux_has_session(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.debug("tmux has-session check failed for %s: %s", session_name, exc)
        return False


def tmux_kill_session(session_name: str) -> bool:
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
            timeout=5,
        )
        return True
    except Exception as exc:
        logger.warning("tmux kill-session failed for %s: %s", session_name, exc)
        return False


def shell_env_prefix(env: Mapping[str, str], prefix: str = "KANBAN_") -> str:
    return " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in env.items()
        if key.startswith(prefix)
    )


def shell_command(args: list[str]) -> str:
    return shlex.join(args)


def start_tmux_session(
    *,
    session_name: str,
    cwd: str | Path,
    args: list[str],
    env: Optional[Mapping[str, str]] = None,
    kill_existing: bool = True,
) -> None:
    """Start a detached tmux session and run a shell-escaped command in it."""
    if not tmux_available():
        raise RuntimeError("tmux is required for headless agent execution")
    if kill_existing and tmux_has_session(session_name):
        tmux_kill_session(session_name)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-c", str(cwd)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    env_prefix = shell_env_prefix(env or os.environ)
    command = shell_command(args)
    if env_prefix:
        command = f"{env_prefix} {command}"
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, command, "Enter"],
        capture_output=True,
        check=True,
        timeout=10,
    )

