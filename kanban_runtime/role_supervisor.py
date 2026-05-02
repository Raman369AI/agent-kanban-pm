"""
Role Supervisor

Manages headless agent processes in separate tmux sessions per the
7-role taxonomy defined in AGENTS.md. Each role runs as its own
process with its own heartbeat, session, and terminal stream.

The supervisor is started by `python -m kanban_cli run` and:
1. Reads role assignments from preferences.yaml
2. Resolves each role's adapter spec from the registry
3. Spawns each agent in a tmux session with proper env vars
4. Monitors session health, restarts on failure
5. Captures terminal output per task/session
"""

import os
import sys
import json
import re
import shutil
import signal
import subprocess
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from kanban_runtime.preferences import (
    Preferences, RoleConfig, RoleAssignment, AgentRole,
    load_preferences, PREFERENCES_PATH,
)
from kanban_runtime.adapter_loader import (
    load_all_adapters,
    standalone_assignment_to_adapter,
    AdapterSpec,
)
from kanban_runtime.process_launcher import (
    start_tmux_session,
    tmux_available as shared_tmux_available,
    tmux_has_session as shared_tmux_has_session,
    tmux_kill_session as shared_tmux_kill_session,
)
from kanban_runtime.instance import get_tmux_prefix, get_mcp_config_dir

logger = logging.getLogger(__name__)

TMUX_SESSION_PREFIX = "kanban"

# Prompt patterns are now loaded from kanban_runtime.prompt_patterns which
# merges builtin, adapter YAML, and user-defined patterns.
from kanban_runtime.prompt_patterns import (  # noqa: E402
    PROMPT_PATTERNS,
    detect_prompt,
    load_patterns,
)


@dataclass
class ManagedSession:
    role: str
    agent: str
    tmux_session: str
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    adapter: Optional[AdapterSpec] = None
    restart_count: int = 0
    last_seen: float = 0.0
    entity_id: Optional[int] = None
    agent_session_id: Optional[int] = None
    project_id: Optional[int] = None
    pending_approval_id: Optional[int] = None
    last_pane_signature: Optional[str] = None


def tmux_capture_pane(session_name: str, lines: int = 50) -> str:
    """Capture the last N lines from a tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception as exc:
        logger.debug(f"tmux capture-pane failed for {session_name}: {exc}")
    return ""


def tmux_send_text(session_name: str, text: str, press_enter: bool = True) -> None:
    """Send literal text to a tmux pane, optionally followed by Enter."""
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "-l", text],
            capture_output=True, timeout=3,
        )
        if press_enter:
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, timeout=3,
            )
    except Exception as exc:
        logger.warning(f"tmux send-keys failed for {session_name}: {exc}")


def detect_prompt(pane_text: str) -> Optional[Tuple[str, str, str, str]]:
    """Return (matched_line, approval_type, approve_reply, reject_reply) or None.

    We only consider the last few non-empty lines so that an old prompt
    earlier in the scrollback doesn't keep firing.
    """
    if not pane_text:
        return None
    lines = [ln.rstrip() for ln in pane_text.splitlines() if ln.strip()]
    if not lines:
        return None
    tail = "\n".join(lines[-12:])
    for pattern, approval_type, yes_reply, no_reply in PROMPT_PATTERNS:
        match = pattern.search(tail)
        if match:
            return tail[-1000:], approval_type, yes_reply, no_reply
    return None


def tmux_available() -> bool:
    return shared_tmux_available()


def _tmux_session_prefix():
    return get_tmux_prefix()


def tmux_session_name(role: str) -> str:
    return f"{_tmux_session_prefix()}-{role}"


def tmux_kill(session_name: str) -> bool:
    return shared_tmux_kill_session(session_name)


def tmux_is_running(session_name: str) -> bool:
    return shared_tmux_has_session(session_name)


def build_env_for_role(
    role: str,
    assignment: RoleAssignment,
    adapter: AdapterSpec,
    api_base: str,
) -> Dict[str, str]:
    env = os.environ.copy()
    env["KANBAN_AGENT_NAME"] = adapter.name
    env["KANBAN_AGENT_ROLE"] = role
    env["KANBAN_API_BASE"] = api_base
    if adapter.auth.env_var and adapter.auth.env_var in os.environ:
        pass
    return env


def build_command_for_role(
    adapter: AdapterSpec,
    assignment: RoleAssignment,
    role: str,
    api_base: str,
    mcp_config_path: Optional[Path] = None,
) -> list:
    cmd_path = shutil.which(adapter.invoke.command)
    if not cmd_path:
        raise FileNotFoundError(f"CLI tool not found: {adapter.invoke.command}")

    args = [cmd_path]
    if mcp_config_path and adapter.invoke.mcp_flag:
        args.extend([adapter.invoke.mcp_flag, str(mcp_config_path)])
    elif adapter.protocol == "mcp" and adapter.invoke.mcp_flag:
        args.append(adapter.invoke.mcp_flag)
    elif adapter.protocol == "stdio" and adapter.invoke.mcp_flag:
        args.append(adapter.invoke.mcp_flag)

    model = assignment.model or (adapter.models[0].id if adapter.models else None)
    if model and adapter.invoke.model_flag:
        args.extend([adapter.invoke.model_flag, model])

    return args


def generate_mcp_config_for_role(
    adapter: AdapterSpec,
    role: str,
    api_base: str,
) -> Path:
    config_dir = get_mcp_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / f"kanban_mcp_{role}.json"

    config = {
        "mcpServers": {
            "kanban": {
                "command": sys.executable,
                "args": ["-m", "mcp_server"],
                "env": {
                    "KANBAN_AGENT_NAME": adapter.name,
                    "KANBAN_AGENT_ROLE": role,
                    "KANBAN_API_BASE": api_base,
                },
            }
        }
    }

    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def spawn_role_in_tmux(
    role: str,
    assignment: RoleAssignment,
    adapter: AdapterSpec,
    api_base: str,
    workspace_path: Optional[str] = None,
    task_prompt: Optional[str] = None,
) -> ManagedSession:
    session_name = tmux_session_name(role)
    env = build_env_for_role(role, assignment, adapter, api_base)
    mcp_config_path = generate_mcp_config_for_role(adapter, role, api_base)
    args = build_command_for_role(adapter, assignment, role, api_base, mcp_config_path)

    cwd = workspace_path or os.getcwd()
    if task_prompt:
        args.extend(["-p", task_prompt])

    if tmux_available():
        start_tmux_session(
            session_name=session_name,
            cwd=cwd,
            args=args,
            env=env,
            kill_existing=True,
        )

        logger.info(f"Spawned role '{role}' (agent={adapter.name}) in tmux session '{session_name}'")
        return ManagedSession(
            role=role,
            agent=adapter.name,
            tmux_session=session_name,
            adapter=adapter,
            last_seen=time.time(),
        )

    raise RuntimeError(
        "tmux is required for headless role supervision because approval "
        "prompt capture/resume depends on tmux capture-pane/send-keys."
    )


class RoleSupervisor:
    def __init__(self, api_base: str = ""):
        if api_base:
            self.api_base = api_base
        else:
            from kanban_runtime.instance import get_api_base
            self.api_base = get_api_base()
        self.sessions: Dict[str, ManagedSession] = {}
        self._task_session_cache: Dict[str, ManagedSession] = {}
        self._running = False

    def start(self):
        prefs = load_preferences()
        if not prefs:
            logger.error("No preferences found. Run: python -m kanban_cli init")
            return

        adapters = {a.name: a for a in load_all_adapters()}
        role_assignments = prefs.get_role_assignments()

        if not role_assignments:
            logger.warning("No role assignments configured. Only starting manager daemon.")
            return

        for role_name, assignment in role_assignments.items():
            adapter = adapters.get(assignment.agent)
            if not adapter:
                adapter = standalone_assignment_to_adapter(role_name, assignment)

            if not shutil.which(adapter.invoke.command):
                logger.warning(f"CLI '{adapter.invoke.command}' for role '{role_name}' not found in PATH. Skipping.")
                continue

            session = spawn_role_in_tmux(
                role=role_name,
                assignment=assignment,
                adapter=adapter,
                api_base=self.api_base,
            )
            session.entity_id = self._resolve_agent_entity_id(adapter.name)
            if session.entity_id is None:
                logger.warning(
                    f"Could not resolve entity_id for adapter '{adapter.name}'. "
                    "Approval queue integration disabled for this role."
                )
            self.sessions[role_name] = session

        self._running = True
        logger.info(f"Role supervisor started with {len(self.sessions)} sessions.")

    def stop(self):
        self._running = False
        for role_name, session in self.sessions.items():
            if tmux_available() and session.tmux_session:
                tmux_kill(session.tmux_session)
                logger.info(f"Killed tmux session for role '{role_name}'")
            elif session.process:
                session.process.terminate()
                logger.info(f"Terminated process for role '{role_name}' (pid={session.pid})")
        self.sessions.clear()

    def status(self) -> Dict:
        result = {}
        for role_name, session in self.sessions.items():
            alive = False
            if tmux_available() and session.tmux_session:
                alive = tmux_is_running(session.tmux_session)
            elif session.process:
                alive = session.process.poll() is None

            result[role_name] = {
                "agent": session.agent,
                "tmux_session": session.tmux_session,
                "alive": alive,
                "restart_count": session.restart_count,
                "pid": session.pid,
            }
        return result

    def restart(self, role_name: str):
        prefs = load_preferences()
        if not prefs:
            return

        adapters = {a.name: a for a in load_all_adapters()}
        role_assignments = prefs.get_role_assignments()
        assignment = role_assignments.get(role_name)
        if not assignment:
            logger.error(f"No assignment for role '{role_name}'")
            return

        adapter = adapters.get(assignment.agent)
        if not adapter:
            adapter = standalone_assignment_to_adapter(role_name, assignment)

        old_session = self.sessions.get(role_name)
        if old_session:
            if tmux_available() and old_session.tmux_session:
                tmux_kill(old_session.tmux_session)
            elif old_session.process:
                old_session.process.terminate()

        session = spawn_role_in_tmux(
            role=role_name,
            assignment=assignment,
            adapter=adapter,
            api_base=self.api_base,
        )
        session.entity_id = self._resolve_agent_entity_id(adapter.name)
        if session.entity_id is None:
            logger.warning(
                f"Could not resolve entity_id for adapter '{adapter.name}'. "
                "Approval queue integration disabled for this role."
            )
        session.restart_count = (old_session.restart_count if old_session else 0) + 1
        self.sessions[role_name] = session
        logger.info(f"Restarted role '{role_name}' (restart count: {session.restart_count})")

    def wait(self):
        try:
            while self._running:
                time.sleep(5)
                self._check_health()
                self._poll_prompts_and_resume()
        except KeyboardInterrupt:
            logger.info("Supervisor interrupted. Stopping all sessions.")
            self.stop()

    def _check_health(self):
        for role_name, session in self.sessions.items():
            alive = False
            if tmux_available() and session.tmux_session:
                alive = tmux_is_running(session.tmux_session)
            elif session.process:
                alive = session.process.poll() is None

            if not alive and self._running:
                logger.warning(f"Role '{role_name}' session died. Restarting...")
                self.restart(role_name)

    # -----------------------------------------------------------------
    # Approval queue integration
    # -----------------------------------------------------------------

    def _api_request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        entity_id: Optional[int] = None,
    ) -> Optional[dict]:
        url = f"{self.api_base.rstrip('/')}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if entity_id is not None:
            req.add_header("X-Entity-ID", str(entity_id))
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            logger.debug(f"Approval API {method} {path} -> HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            logger.debug(f"Approval API {method} {path} unreachable: {exc}")
        except json.JSONDecodeError:
            logger.debug(f"Approval API {method} {path} returned non-JSON")
        return None

    def _resolve_agent_entity_id(self, agent_name: str) -> Optional[int]:
        result = self._api_request("GET", "/entities?entity_type=agent")
        if not isinstance(result, list):
            return None
        for item in result:
            if item.get("name") == agent_name:
                return int(item["id"])
        return None

    def _refresh_project_binding(self, session: ManagedSession) -> None:
        """Look up the agent's most recent active AgentSession to bind project/session ids.

        AgentSession rows are created by workers via the `start_agent_session`
        MCP tool. The supervisor reuses that as its source of truth for which
        project a role is currently working on.
        """
        if session.entity_id is None:
            return
        result = self._api_request(
            "GET",
            f"/agents/sessions?agent_id={session.entity_id}&active_only=true&limit=1",
        )
        if not isinstance(result, list) or not result:
            return
        record = result[0]
        session.agent_session_id = record.get("id")
        session.project_id = record.get("project_id")

    def _request_approval(self, session: ManagedSession, prompt_line: str, approval_type: str) -> Optional[int]:
        if session.project_id is None or session.entity_id is None:
            return None
        body = {
            "project_id": session.project_id,
            "session_id": session.agent_session_id,
            "agent_id": session.entity_id,
            "approval_type": approval_type,
            "title": f"{session.role}: {approval_type.replace('_', ' ')}",
            "message": prompt_line,
            "command": prompt_line,
        }
        result = self._api_request("POST", "/agents/approvals", body, entity_id=session.entity_id)
        if result and "id" in result:
            logger.info(
                f"Filed approval #{result['id']} for role '{session.role}' "
                f"(type={approval_type}): {prompt_line!r}"
            )
            return int(result["id"])
        return None

    def _fetch_approval(self, approval_id: int, entity_id: int) -> Optional[dict]:
        result = self._api_request("GET", f"/agents/approvals?limit=200", entity_id=entity_id)
        if isinstance(result, list):
            for item in result:
                if item.get("id") == approval_id:
                    return item
        return None

    def _poll_prompts_and_resume(self):
        if not tmux_available():
            return
        for role_name, session in self.sessions.items():
            if not session.tmux_session or not tmux_is_running(session.tmux_session):
                continue

            if session.pending_approval_id is not None:
                if session.entity_id is None:
                    continue
                approval = self._fetch_approval(session.pending_approval_id, session.entity_id)
                if not approval:
                    continue
                status_value = approval.get("status")
                if status_value == "pending":
                    continue
                # Resolved — replay the appropriate stdin reply.
                response_message = approval.get("response_message") or ""
                detection = detect_prompt(tmux_capture_pane(session.tmux_session, lines=10))
                yes_reply = detection[2] if detection else "y"
                no_reply = detection[3] if detection else "n"
                if status_value == "approved":
                    reply = response_message.strip() or yes_reply
                elif status_value == "rejected":
                    reply = response_message.strip() or no_reply
                else:  # cancelled / expired
                    reply = no_reply
                tmux_send_text(session.tmux_session, reply)
                logger.info(
                    f"Resumed role '{role_name}' after approval #{approval.get('id')} "
                    f"(decision={status_value}, reply={reply!r})"
                )
                session.pending_approval_id = None
                session.last_pane_signature = None
                continue

            pane_text = tmux_capture_pane(session.tmux_session, lines=20)
            signature = pane_text[-400:] if pane_text else None
            if signature and signature == session.last_pane_signature:
                continue
            detection = detect_prompt(pane_text)
            if not detection:
                session.last_pane_signature = signature
                continue
            prompt_line, approval_type, _, _ = detection
            if session.project_id is None or session.agent_session_id is None:
                self._refresh_project_binding(session)
            approval_id = self._request_approval(session, prompt_line, approval_type)
            if approval_id:
                session.pending_approval_id = approval_id
                session.last_pane_signature = signature

        self._poll_task_sessions()

    def _poll_task_sessions(self):
        """Monitor kanban-task-* tmux sessions for approval prompts."""
        if not tmux_available():
            return
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5,
            )
            session_names = result.stdout.strip().splitlines()
        except Exception as exc:
            logger.warning("tmux list-sessions failed: %s", exc)
            return

        current_prefix = re.escape(_tmux_session_prefix())
        task_pattern = re.compile(rf"^{current_prefix}-task-(\d+)-(.+)$")
        for session_name in session_names:
            m = task_pattern.match(session_name)
            if not m:
                continue
            task_id = int(m.group(1))
            agent_name = m.group(2)

            cached = self._task_session_cache.get(session_name)
            if cached is None:
                entity_id = self._resolve_agent_entity_id(agent_name)
                if not entity_id:
                    continue
                sessions_data = self._api_request(
                    "GET", f"/agents/sessions?task_id={task_id}&limit=5", entity_id=entity_id
                )
                agent_session_id = None
                project_id = None
                if isinstance(sessions_data, list):
                    for s in sessions_data:
                        if s.get("status") == "active" and s.get("agent_id") == entity_id:
                            agent_session_id = s.get("id")
                            project_id = s.get("project_id")
                            break
                cached = ManagedSession(
                    role=f"task-{task_id}",
                    agent=agent_name,
                    tmux_session=session_name,
                    adapter=None,
                    last_seen=time.time(),
                )
                cached.entity_id = entity_id
                cached.agent_session_id = agent_session_id
                cached.project_id = project_id
                self._task_session_cache[session_name] = cached

            session = cached
            if not tmux_is_running(session_name):
                self._task_session_cache.pop(session_name, None)
                continue

            if session.pending_approval_id is not None:
                if session.entity_id is None:
                    continue
                approval = self._fetch_approval(session.pending_approval_id, session.entity_id)
                if not approval:
                    continue
                status_value = approval.get("status")
                if status_value == "pending":
                    continue
                response_message = approval.get("response_message") or ""
                detection = detect_prompt(tmux_capture_pane(session_name, lines=10))
                yes_reply = detection[2] if detection else "y"
                no_reply = detection[3] if detection else "n"
                if status_value == "approved":
                    reply = response_message.strip() or yes_reply
                elif status_value == "rejected":
                    reply = response_message.strip() or no_reply
                else:
                    reply = no_reply
                tmux_send_text(session_name, reply)
                logger.info(
                    "Resumed task session '%s' after approval #%s (decision=%s)",
                    session_name, approval.get("id"), status_value,
                )
                session.pending_approval_id = None
                session.last_pane_signature = None
                continue

            pane_text = tmux_capture_pane(session_name, lines=20)
            signature = pane_text[-400:] if pane_text else None
            if signature and signature == session.last_pane_signature:
                continue
            detection = detect_prompt(pane_text)
            if not detection:
                session.last_pane_signature = signature
                continue
            prompt_line, approval_type, _, _ = detection
            approval_id = self._request_approval(session, prompt_line, approval_type)
            if approval_id:
                session.pending_approval_id = approval_id
                session.last_pane_signature = signature
