"""
Manager Daemon

Spawns the configured manager CLI tool, inheriting provider env vars.
The manager agent uses MCP tools to poll pending tasks, assign work,
watch heartbeats, and escalate to human on blockers.

The server stops deciding. The manager agent decides.

Phase 5.2 additions:
- Restart loop with exponential backoff
- PID file tracking
- MCP config generation
- CLI status/stop commands

Phase 6 — Env-based identity:
- KANBAN_AGENT_NAME identifies the adapter to the MCP server
- KANBAN_AGENT_ROLE sets RBAC scope
"""

import os
import sys
import shutil
import subprocess
import signal
import time
import logging
from pathlib import Path

from kanban_runtime.preferences import load_preferences, PREFERENCES_PATH
from kanban_runtime.adapter_loader import load_all_adapters

logger = logging.getLogger(__name__)

DAEMON_PID_PATH = Path.home() / ".kanban" / "daemon.pid"
MCP_CONFIG_DIR = Path.home() / ".kanban" / "mcp"

MAX_BACKOFF_SECONDS = 300  # 5 minutes
INITIAL_BACKOFF_SECONDS = 5


def get_manager_command() -> tuple:
    """Resolve the manager agent's CLI command from preferences + adapter spec."""
    prefs = load_preferences()
    if not prefs:
        logger.error(f"No preferences found. Run: python -m kanban_cli init")
        sys.exit(1)

    adapters = {a.name: a for a in load_all_adapters()}
    roles = prefs.get_roles()

    manager_name = None
    if roles and roles.orchestrator:
        manager_name = roles.orchestrator.agent
    elif prefs.manager:
        manager_name = prefs.manager.agent

    if not manager_name:
        logger.error("No manager/orchestrator agent configured in preferences")
        sys.exit(1)

    spec = adapters.get(manager_name)
    if not spec:
        logger.error(f"Manager adapter '{manager_name}' not found in ~/.kanban/agents/")
        sys.exit(1)

    cmd_path = shutil.which(spec.invoke.command)
    if not cmd_path:
        logger.error(f"Manager CLI tool not found in PATH: {spec.invoke.command}")
        sys.exit(1)

    return spec, cmd_path


def write_pid_file(pid: int):
    """Write daemon PID and start time to file."""
    DAEMON_PID_PATH.write_text(f"{pid}\n{int(time.time())}\n")


def read_pid_file() -> tuple:
    """Read daemon PID and start time from file. Returns (pid, start_time) or (None, None)."""
    if not DAEMON_PID_PATH.exists():
        return None, None
    try:
        lines = DAEMON_PID_PATH.read_text().strip().splitlines()
        return int(lines[0]), int(lines[1]) if len(lines) > 1 else None
    except Exception:
        return None, None


def remove_pid_file():
    """Remove daemon PID file."""
    if DAEMON_PID_PATH.exists():
        DAEMON_PID_PATH.unlink()


def generate_mcp_config(spec, api_base: str) -> Path:
    """Generate a per-session MCP config file for the spawned CLI tool."""
    MCP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_path = MCP_CONFIG_DIR / "kanban_mcp.json"

    config = {
        "mcpServers": {
            "kanban": {
                "command": sys.executable,
                "args": ["-m", "mcp_server"],
                "env": {
                    "KANBAN_AGENT_NAME": spec.name,
                    "KANBAN_AGENT_ROLE": "manager",
                    "KANBAN_API_BASE": api_base
                }
            }
        }
    }

    import json
    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def start_manager_daemon():
    """Start the manager agent daemon process with restart loop."""
    prefs = load_preferences()
    if not prefs:
        logger.error(f"No preferences at {PREFERENCES_PATH}. Run: python -m kanban_cli init")
        sys.exit(1)

    spec, cmd_path = get_manager_command()

    # Build env
    env = os.environ.copy()
    env["KANBAN_API_BASE"] = env.get("KANBAN_API_BASE", "http://localhost:8000")
    env["KANBAN_AGENT_NAME"] = spec.name
    env["KANBAN_AGENT_ROLE"] = "orchestrator"
    if spec.auth.env_var and spec.auth.env_var in os.environ:
        env["KANBAN_AGENT_ENV_VAR"] = spec.auth.env_var

    # Optionally generate MCP config
    mcp_config_path = generate_mcp_config(spec, env["KANBAN_API_BASE"])
    logger.info(f"Generated MCP config: {mcp_config_path}")

    # Build command
    args = [cmd_path]
    if spec.invoke.mcp_flag:
        args.append(spec.invoke.mcp_flag)

    roles = prefs.get_roles()
    mode = "auto"
    if roles and roles.orchestrator:
        mode = roles.orchestrator.mode
    elif prefs.manager:
        mode = prefs.manager.mode

    logger.info(f"Starting manager daemon: {' '.join(args)}")
    logger.info(f"Manager: {spec.display_name}, Mode: {mode}")

    # Write PID file for this parent process
    write_pid_file(os.getpid())

    backoff = INITIAL_BACKOFF_SECONDS
    try:
        while True:
            logger.info(f"Spawning manager subprocess...")
            try:
                proc = subprocess.Popen(args, env=env)
                # Update PID file with child PID
                write_pid_file(proc.pid)
                proc.wait()

                if proc.returncode == 0:
                    logger.info("Manager subprocess exited cleanly.")
                    break
                else:
                    logger.warning(f"Manager subprocess exited with code {proc.returncode}. Restarting in {backoff}s...")
            except Exception as e:
                logger.error(f"Manager subprocess failed: {e}. Restarting in {backoff}s...")

            time.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    except KeyboardInterrupt:
        logger.info("Manager daemon stopped by user.")
    finally:
        remove_pid_file()


def daemon_status() -> dict:
    """Check daemon status. Returns dict with running status."""
    pid, start_time = read_pid_file()
    if not pid:
        return {"running": False, "message": "No daemon PID file found"}

    # Check if process exists
    try:
        os.kill(pid, 0)
        uptime = int(time.time()) - start_time if start_time else None
        return {
            "running": True,
            "pid": pid,
            "uptime_seconds": uptime,
            "message": f"Daemon running (pid={pid}, uptime={uptime}s)" if uptime else f"Daemon running (pid={pid})"
        }
    except ProcessLookupError:
        return {"running": False, "message": f"Daemon pid={pid} not running (stale PID file)"}


def daemon_stop() -> dict:
    """Stop the daemon. Returns dict with result."""
    pid, _ = read_pid_file()
    if not pid:
        return {"success": False, "message": "No daemon PID file found"}

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except ProcessLookupError:
                break
        remove_pid_file()
        return {"success": True, "message": f"Daemon pid={pid} stopped"}
    except ProcessLookupError:
        remove_pid_file()
        return {"success": False, "message": f"Daemon pid={pid} was not running (stale PID file removed)"}
    except PermissionError:
        return {"success": False, "message": f"Permission denied to stop daemon pid={pid}"}
