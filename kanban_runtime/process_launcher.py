"""Shared local process/tmux/PTY launch helpers."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Optional

from kanban_runtime.pty_manager import pty_manager

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


# ---------------------------------------------------------------------------
# Unified Execution Runner APIs (tmux with PTY subprocess fallback)
# ---------------------------------------------------------------------------

def runner_available() -> bool:
    """Returns True if a runner is available (tmux or native PTY fallback)."""
    return tmux_available() or True


def has_session(session_name: str) -> bool:
    """Checks if a session is currently active."""
    if tmux_available():
        return tmux_has_session(session_name)
    return pty_manager.exists(session_name)


def kill_session(session_name: str) -> bool:
    """Kills an active session by name."""
    if tmux_available():
        return tmux_kill_session(session_name)
    return pty_manager.kill_session(session_name)


def start_session(
    *,
    session_name: str,
    cwd: str | Path,
    args: list[str],
    env: Optional[Mapping[str, str]] = None,
    kill_existing: bool = True,
) -> None:
    """Spawns a background process session (using tmux if available, else PTY)."""
    if kill_existing and has_session(session_name):
        kill_session(session_name)

    if tmux_available():
        start_tmux_session(
            session_name=session_name,
            cwd=cwd,
            args=args,
            env=env,
            kill_existing=False,  # Already handled by check above
        )
    else:
        pty_manager.start_pty_session(
            session_name=session_name,
            cwd=cwd,
            args=args,
            env=env,
        )


def capture_pane(session_name: str, lines: int = 50) -> str:
    """Captures the output lines from a session."""
    if tmux_available():
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                return result.stdout
        except Exception as exc:
            logger.debug("tmux capture-pane failed for %s: %s", session_name, exc)
        return ""
    return pty_manager.capture_pane(session_name, lines)


def send_text(session_name: str, text: str, press_enter: bool = True) -> None:
    """Sends keystrokes / text inputs to a session's stdin."""
    if tmux_available():
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l", text],
                capture_output=True,
                timeout=3,
            )
            if press_enter:
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name, "Enter"],
                    capture_output=True,
                    timeout=3,
                )
        except Exception as exc:
            logger.warning("tmux send-keys failed for %s: %s", session_name, exc)
    else:
        pty_manager.send_text(session_name, text, press_enter=press_enter)
