"""Shared local process/tmux/PTY launch helpers."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Optional

from agent_kanban_pm.runtime.pty_manager import pty_manager

logger = logging.getLogger(__name__)

# tmux rejects send-keys arguments above roughly 4-8 KiB ("command too long"),
# so long payloads are typed into the pane in literal chunks well under that.
TMUX_SEND_KEYS_CHUNK_SIZE = 2000


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def tmux_send_literal(
    session_name: str,
    text: str,
    *,
    chunk_size: int = TMUX_SEND_KEYS_CHUNK_SIZE,
    check: bool = False,
) -> None:
    """Inject literal text into a tmux pane.

    tmux's send-keys command buffer is limited to a few KiB, so a single
    large argument fails with "command too long". Long payloads are
    delivered through the tmux paste buffer instead (no size limit, and
    the pane reads it as one paste rather than racing keystrokes);
    send-keys chunks are only a fallback for old tmux builds.
    """
    if not text:
        return
    if _tmux_paste_text(session_name, text, check=check):
        return
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    for chunk in chunks:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "-l", chunk],
            capture_output=True,
            timeout=10,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                ["tmux", "send-keys", "-t", session_name, "-l", "<chunk>"],
                stderr=result.stderr,
            )


def _tmux_paste_text(session_name: str, text: str, *, check: bool) -> bool:
    """Load text into a tmux buffer and paste it into the pane.

    Returns False (without raising when check is False) if the installed
    tmux cannot do stdin buffering, so the caller can fall back to
    send-keys chunks. Requires tmux >= 3.2 for `load-buffer -`.
    """
    try:
        result = subprocess.run(
            ["tmux", "load-buffer", "-"],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, ["tmux", "load-buffer", "-"], stderr=result.stderr
            )
        subprocess.run(
            ["tmux", "paste-buffer", "-t", session_name, "-d"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        return True
    except Exception as exc:
        if check:
            raise
        logger.debug("tmux paste-buffer fallback to send-keys for %s: %s", session_name, exc)
        return False


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
    tmux_send_literal(session_name, command, check=True)
    subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "Enter"],
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
            tmux_send_literal(session_name, text)
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
