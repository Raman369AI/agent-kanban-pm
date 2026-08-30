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


def get_data_home() -> Path:
    """Return the user-owned directory for persistent Kanban state."""
    override = os.getenv("KANBAN_DATA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    xdg_home = os.getenv("XDG_DATA_HOME")
    if xdg_home:
        return Path(xdg_home).expanduser().resolve() / "agent-kanban-pm"
    return Path.home() / ".kanban"


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

    The primary worktree keeps the default port and tmux prefix. Git itself,
    rather than the installed package path, identifies the primary worktree.
    """
    root = Path(_project_root()).resolve()
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=root,
            capture_output=True, text=True, timeout=5,
        )
        first = next((line.removeprefix("worktree ") for line in result.stdout.splitlines()
                      if line.startswith("worktree ")), None)
        return result.returncode == 0 and first is not None and Path(first).resolve() == root
    except Exception:
        return True


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


def get_port(host: str = "127.0.0.1") -> int:
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
      2. CWD-relative path in explicit development mode or for a legacy database
      3. Per-project instance path beneath the user data home
    """
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        return env_url

    legacy_db = Path.cwd() / "kanban.db"
    development = os.getenv("KANBAN_DEV", "").lower() in {"1", "true", "yes", "on"}
    if development or legacy_db.exists():
        return "sqlite+aiosqlite:///./kanban.db"

    instance_id = _derive_instance_id(_project_root())
    db_dir = get_data_home() / "instances" / instance_id
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "kanban.db"
    return f"sqlite+aiosqlite:///{db_path}"


def get_mcp_config_dir() -> Path:
    """Return the per-instance MCP config directory.

    Role-specific MCP config files (kanban_mcp_{role}.json) are written
    one per running instance so that different ports don't overwrite each
    other.
    """
    instance_id = _derive_instance_id(_project_root())
    config_dir = get_data_home() / "instances" / instance_id / "mcp"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_instance_info(host: str = "127.0.0.1") -> dict:
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


# Hostnames the HTTP server considers legitimate. Requests whose Host header
# does not match are rejected before routing, which blocks DNS-rebinding
# attacks against the loopback UI. Ports are ignored by the check. IPv6
# loopback ([::1]) is not listed: Starlette matches against the Host header
# with the port stripped naively, so bracketed IPv6 literals cannot match —
# use KANBAN_ALLOWED_HOSTS plus a hostname if you bind to ::1.
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]


def get_auth_token() -> str:
    """Retrieve or generate a secure, cached auth token for the Kanban PM service.

    The token is stored in ~/.kanban/token to allow authenticating CLI
    actions and role supervisors locally. The file is owner-only (0600);
    pre-existing files with looser permissions are tightened on read.
    """
    token_dir = Path.home() / ".kanban"
    token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = token_dir / "token"

    if token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                _chmod_owner_only(token_file)
                return token
        except Exception:
            pass

    import secrets
    token = secrets.token_hex(32)
    try:
        # Create with 0600 up front so the secret never sits on disk with
        # default (umask-derived) permissions.
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
    except Exception as e:
        logger.warning("Could not write auth token file: %s", e)
    return token


def _chmod_owner_only(path: Path) -> None:
    """Best-effort tightening of *path* to owner-only read/write."""
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError as exc:
        logger.warning("Could not tighten permissions on %s: %s", path, exc)


def get_csrf_token() -> str:
    """Return the CSRF token paired with this instance's auth token.

    Derived statelessly via HMAC so every server process (and every page
    render) agrees on the value without extra storage. Browser mutations
    authenticated by the kanban-token cookie must also present this value in
    the X-CSRF-Token header; it is embedded in UI pages as a meta tag.
    """
    import hashlib
    import hmac

    return hmac.new(
        get_auth_token().encode("utf-8"), b"kanban-csrf-v1", hashlib.sha256
    ).hexdigest()
