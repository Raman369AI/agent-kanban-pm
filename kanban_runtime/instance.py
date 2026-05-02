"""Instance isolation for worktree-safe multi-instance operation.

When running multiple copies of the Kanban server from different git
worktrees, each instance needs its own:

  - HTTP port (uvicorn)
  - SQLite database path
  - tmux session name prefix
  - MCP config directory

This module derives all of those from the project root (git worktree
path) so that two worktrees running side-by-side never collide.

Port selection probes for availability. The algorithm:

  1. If KANBAN_PORT is set, use it (no probing — user know best).
  2. For the primary worktree, try 8000 then scan up.
  3. For a secondary worktree, hash the instance ID into a starting
     port in 8000-8099, then scan up.
  4. If a port is occupied, increment up to 100 ports before giving up.

Override any value via environment variables:

  KANBAN_PORT        — force a specific port (e.g. 8001)
  KANBAN_INSTANCE_ID — force a specific instance tag (e.g. "review")
  DATABASE_URL       — force a specific database URL
  KANBAN_API_BASE    — force the API base URL (e.g. http://localhost:8001)
  KANBAN_PROJECT_ROOT — force the project root path
"""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8000
_MAX_PORT_PROBES = 9000


def _git_worktree_root() -> Optional[str]:
    """Return the git worktree root for CWD, or None if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass

    cwd = os.getcwd()
    if os.path.isdir(os.path.join(cwd, ".git")):
        return cwd
    return None


def _project_root() -> str:
    """Return the project root directory.

    Priority:
      1. KANBAN_PROJECT_ROOT env var
      2. git worktree root
      3. CWD
    """
    env_root = os.getenv("KANBAN_PROJECT_ROOT")
    if env_root:
        return env_root
    git_root = _git_worktree_root()
    if git_root:
        return git_root
    return os.getcwd()


def _derive_instance_id(project_root: str) -> str:
    """Derive a short, stable instance ID from the project root path.

    Uses a 4-character hex hash of the absolute path so that different
    worktree paths produce different IDs but the same path always
    produces the same ID.

    Override with KANBAN_INSTANCE_ID.
    """
    env_id = os.getenv("KANBAN_INSTANCE_ID")
    if env_id:
        return env_id

    abs_path = os.path.abspath(project_root)
    short_hash = hashlib.sha1(abs_path.encode()).hexdigest()[:4]
    basename = os.path.basename(abs_path)
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in basename)
    safe = safe.strip("-")[:16]
    return f"{safe}-{short_hash}"


def _is_primary_worktree() -> bool:
    """Return True if the project root is the main (primary) worktree.

    The primary worktree keeps the default behaviour (port 8000 if
    available, "kanban" tmux prefix, legacy ./kanban.db path).
    """
    abs_root = os.path.abspath(_project_root())
    default_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return abs_root == default_root


def _port_is_available(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if *port* on *host* is not currently bound."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            result = s.connect_ex((host, port))
            return result != 0
    except (OSError, socket.error):
        return True


def _find_available_port(start: int, host: str = "127.0.0.1") -> Optional[int]:
    """Scan from *start* upward, up to _MAX_PORT_PROBES, for a free port.

    Returns the first available port, or None if all are occupied.
    """
    for offset in range(_MAX_PORT_PROBES):
        candidate = start + offset
        if candidate > 65535:
            break
        if _port_is_available(candidate, host):
            return candidate
    return None


def get_port(host: str = "0.0.0.0") -> int:
    """Return the HTTP port for this instance.

    Priority:
      1. KANBAN_PORT env var — used verbatim, no probing.
      2. Probe 8000 (primary) or a hash-derived start port (secondary),
         then scan upward for the first available port.

    The probe ensures we never collide with React (3000/5173), Vite,
    Django, Postgres, or any other service already bound.
    """
    env_port = os.getenv("KANBAN_PORT")
    if env_port:
        return int(env_port)

    if _is_primary_worktree():
        start = _DEFAULT_PORT
    else:
        instance_id = _derive_instance_id(_project_root())
        h = hashlib.sha1(instance_id.encode()).hexdigest()
        start = _DEFAULT_PORT + (int(h[:4], 16) % 100)

    available = _find_available_port(start, host if host != "0.0.0.0" else "127.0.0.1")
    if available is not None:
        if available != start:
            logger.info("Port %d occupied, using port %d instead.", start, available)
        return available

    logger.warning("No available port found in %d-%d range. Falling back to %d.", start, start + _MAX_PORT_PROBES, start)
    return start


def get_api_base(port: Optional[int] = None) -> str:
    """Return the API base URL for this instance.

    Priority:
      1. KANBAN_API_BASE env var
      2. http://localhost:{port}
    """
    env_base = os.getenv("KANBAN_API_BASE")
    if env_base:
        return env_base
    p = port if port is not None else get_port()
    return f"http://localhost:{p}"


def get_tmux_prefix() -> str:
    """Return the tmux session name prefix for this instance.

    Default is "kanban". In a worktree, it becomes
    "kanban-{instance_id}" so role sessions like "kanban-orchestrator"
    become "kanban-review-a1b2-orchestrator" and don't collide.
    """
    if _is_primary_worktree():
        return "kanban"
    instance_id = _derive_instance_id(_project_root())
    return f"kanban-{instance_id}"


def get_database_url() -> str:
    """Return the database URL for this instance.

    Priority:
      1. DATABASE_URL env var
      2. Legacy CWD-relative path for the primary worktree
      3. Instance-specific path for secondary worktrees:
         .kanban/instances/{instance_id}/kanban.db
    """
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url

    if _is_primary_worktree():
        return "sqlite+aiosqlite:///./kanban.db"

    instance_id = _derive_instance_id(_project_root())
    abs_root = os.path.abspath(_project_root())
    db_dir = os.path.join(abs_root, ".kanban", "instances", instance_id)
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "kanban.db")
    return f"sqlite+aiosqlite:///{db_path}"


def get_mcp_config_dir() -> Path:
    """Return the per-instance MCP config directory.

    Role-specific MCP config files (kanban_mcp_{role}.json) are written
    one per running instance so that different ports don't overwrite each
    other.
    """
    if _is_primary_worktree():
        return Path.home() / ".kanban" / "mcp"

    instance_id = _derive_instance_id(_project_root())
    abs_root = os.path.abspath(_project_root())
    config_dir = Path(abs_root) / ".kanban" / "instances" / instance_id / "mcp"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_instance_info(host: str = "0.0.0.0") -> dict:
    """Return a summary dict of all instance-specific values.

    Useful for `kanban sheet` and `kanban run` startup output.
    """
    project_root = _project_root()
    instance_id = _derive_instance_id(project_root)
    port = get_port(host)
    return {
        "project_root": project_root,
        "instance_id": instance_id,
        "port": port,
        "api_base": get_api_base(port),
        "tmux_prefix": get_tmux_prefix(),
        "database_url": get_database_url(),
        "mcp_config_dir": str(get_mcp_config_dir()),
    }