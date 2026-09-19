"""Worktree-local multi-agent handoff protocol.

This module keeps the repo/worktree coordination rules out of AGENTS.md state
and turns them into concrete runtime instructions for CLI agents.
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Iterable, List, Optional

import yaml
from pydantic import BaseModel, Field

from agent_kanban_pm.runtime.adapter_loader import AdapterSpec, BUNDLED_ADAPTERS_DIR, discover_popular_clis, load_adapter
from agent_kanban_pm.runtime.preferences import Preferences, load_preferences

logger = logging.getLogger(__name__)


STATUS_FILENAME = "STATUS.md"
INSTRUCTION_ALIAS_FILENAMES = ("CLAUDE.md", "CODEX.md")


STATUS_TEMPLATE = """---
state: assigned
handoff_ready: false
task_id: null
project_id: null
session_id: null
run_token: null
current_agent: null
assigned_role: null
workspace_path: null
summary: |
  What is happening in this worktree, in 3-5 lines.
  What decisions I made and why.
  What remains or was deliberately left out.
outputs:
  - path/to/file.ts
artifacts: []
signals_to_next: |
  Write exact facts here, not pointers to code.
  e.g. POST /login -> {token: JWT, expiresIn: 3600}
  e.g. Bearer auth required on all /api/* routes
blockers: none
---
"""


class StatusFrontmatter(BaseModel):
    """Typed schema for STATUS.md YAML frontmatter.

    Used for validation and structured access. Unknown fields are preserved
    via model_config so agent-written extra keys are not discarded.
    """
    model_config = {"extra": "allow"}

    state: str = "assigned"
    handoff_ready: bool = False
    task_id: Optional[int] = None
    project_id: Optional[int] = None
    session_id: Optional[int] = None
    run_token: Optional[str] = None
    current_agent: Optional[str] = None
    assigned_role: Optional[str] = None
    workspace_path: Optional[str] = None
    summary: str = ""
    outputs: List[str] = Field(default_factory=list)
    artifacts: List[dict[str, Any]] = Field(default_factory=list)
    signals_to_next: str = ""
    blockers: str = "none"
    updated_at: Optional[str] = None
    task_title: Optional[str] = None


def _validate_frontmatter(data: dict) -> StatusFrontmatter:
    """Validate raw frontmatter dict through the Pydantic model.

    Returns a validated model on success, or a default model with the raw
    data merged in on failure (so downstream code never crashes).
    """
    try:
        return StatusFrontmatter.model_validate(data)
    except Exception as exc:
        logger.warning("STATUS.md frontmatter validation failed: %s", exc)
        return StatusFrontmatter()


@dataclass(frozen=True)
class HandoffAgentProfile:
    agent: str
    role: str
    owns: tuple[str, ...]
    review_only: bool = False

    @property
    def worktree_dir_name(self) -> str:
        return self.agent


def profile_for_agent(agent_name: str) -> HandoffAgentProfile:
    """Resolve the handoff profile for an agent.

    Resolution order:
    1. Preferences (RoleAssignment.owns / review_only) — allows
       preference-based roles to customize path ownership.
    2. Adapter YAML (data-driven) — bundled adapters define owns/review_only.
    3. Generic fallback for unknown agents.
    """
    canonical = (agent_name or "").strip().lower()

    # Try preferences first (allows standalone CLI roles to customise ownership)
    try:
        from agent_kanban_pm.runtime.preferences import load_preferences
        prefs = load_preferences()
        if prefs:
            for role_name, ra in prefs.get_role_assignments().items():
                if ra.agent == canonical and (ra.owns or ra.review_only):
                    return HandoffAgentProfile(
                        agent=canonical,
                        role=role_name,
                        owns=tuple(ra.owns),
                        review_only=ra.review_only,
                    )
    except Exception:
        pass

    # Try adapter YAML (data-driven)
    try:
        from agent_kanban_pm.runtime.adapter_loader import load_all_adapters
        for spec in load_all_adapters():
            if spec.name == canonical:
                return HandoffAgentProfile(
                    agent=canonical,
                    role=spec.roles[0] if spec.roles else "implementation",
                    owns=tuple(spec.owns),
                    review_only=spec.review_only,
                )
    except Exception:
        pass

    # Generic fallback for unknown agents
    return HandoffAgentProfile(
        agent=canonical or agent_name,
        role="implementation",
        owns=(),
    )


def status_path_for_workspace(workspace_path: str | Path) -> Path:
    return Path(workspace_path) / STATUS_FILENAME


def agents_path_for_workspace(workspace_path: str | Path) -> Path:
    return Path(workspace_path) / "AGENTS.md"


def parse_status_frontmatter(status_text: str) -> dict[str, Any]:
    """Return STATUS.md YAML frontmatter as a dict."""
    stripped = status_text.strip()
    if not stripped.startswith("---"):
        return {}
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        data = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_status_state(status_text: str) -> Optional[str]:
    """Return the YAML frontmatter `state` from a STATUS.md file."""
    data = parse_status_frontmatter(status_text)
    state = data.get("state")
    return str(state).strip().lower() if state is not None else None


def render_status_frontmatter(data: dict[str, Any]) -> str:
    return "---\n" + yaml.safe_dump(data, sort_keys=False, allow_unicode=False) + "---\n"


def _status_body(content: str) -> str:
    parts = content.split("---", 2)
    return parts[2] if len(parts) == 3 and content.startswith("---") else ""


def _write_status(path: Path, data: dict[str, Any], body: str = "") -> None:
    """Replace the handoff atomically while retaining notes below its header."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(render_status_frontmatter(data) + body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def read_status_state(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return parse_status_state(path.read_text(encoding="utf-8"))


def read_status_file(workspace_path: str | Path) -> dict[str, Any]:
    """Read and validate STATUS.md from a workspace path.

    Returns a dict with keys: exists, path, frontmatter (raw dict),
    validated (StatusFrontmatter model), content, state, handoff_ready.
    """
    path = status_path_for_workspace(workspace_path)
    if not path.exists():
        return {
            "exists": False,
            "path": str(path),
            "frontmatter": {},
            "validated": StatusFrontmatter(),
            "content": "",
            "state": None,
            "handoff_ready": False,
        }
    content = path.read_text(encoding="utf-8")
    frontmatter = parse_status_frontmatter(content)
    validated = _validate_frontmatter(frontmatter)
    return {
        "exists": True,
        "path": str(path),
        "frontmatter": frontmatter,
        "validated": validated,
        "content": content,
        "state": parse_status_state(content),
        "handoff_ready": validated.handoff_ready,
    }


def status_identity_matches_session(status_data: dict[str, Any], session: Any) -> bool:
    """Check file ownership independently of whether the handoff is ready."""
    frontmatter = status_data.get("validated")
    return bool(
        status_data.get("exists")
        and frontmatter
        and frontmatter.project_id == session.project_id
        and frontmatter.task_id == session.task_id
        and frontmatter.session_id == session.id
        and session.run_token
        and frontmatter.run_token == session.run_token
    )


def status_matches_session(status_data: dict[str, Any], session: Any) -> bool:
    """Accept a completion signal only for the exact active run."""
    return bool(
        status_identity_matches_session(status_data, session)
        and status_data.get("handoff_ready")
        and status_data.get("state") in {"done", "completed", "review"}
    )


def ensure_instruction_aliases(workspace_path: str | Path) -> dict[str, str]:
    """Best-effort `CLAUDE.md`/`CODEX.md` symlinks to AGENTS.md.

    Real files are left untouched. Existing symlinks are replaced so this is
    equivalent to a safe `ln -sf AGENTS.md <alias>` for managed aliases.
    """
    workspace = Path(workspace_path)
    agents_path = agents_path_for_workspace(workspace)
    results: dict[str, str] = {}
    if not agents_path.exists():
        return {alias: "missing AGENTS.md" for alias in INSTRUCTION_ALIAS_FILENAMES}

    for alias in INSTRUCTION_ALIAS_FILENAMES:
        alias_path = workspace / alias
        try:
            if alias_path.is_symlink():
                alias_path.unlink()
            elif alias_path.exists():
                results[alias] = "kept existing real file"
                continue
            alias_path.symlink_to("AGENTS.md")
            results[alias] = "linked to AGENTS.md"
        except OSError as exc:
            results[alias] = f"error: {exc}"
    return results


def initialize_status_file(
    workspace_path: str | Path,
    *,
    task_id: Optional[int],
    project_id: Optional[int],
    current_agent: str,
    assigned_role: str,
    task_title: Optional[str] = None,
    session_id: Optional[int] = None,
    run_token: Optional[str] = None,
    overwrite: bool = False,
) -> Path:
    """Create or update the worktree-local STATUS.md assignment header."""
    path = status_path_for_workspace(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = read_status_file(workspace_path)
    data = dict(existing["frontmatter"]) if existing["exists"] else {}
    same_assignment = (
        data.get("task_id") == task_id
        and data.get("project_id") == project_id
        and (session_id is None or data.get("session_id") == session_id)
        and (run_token is None or data.get("run_token") == run_token)
    )
    if existing["exists"] and same_assignment and data.get("state") == "done" and not overwrite:
        return path
    if not same_assignment or overwrite:
        data = {}

    data.update({
        "state": data.get("state") or "assigned",
        "handoff_ready": bool(data.get("handoff_ready", False)),
        "task_id": task_id,
        "project_id": project_id,
        "session_id": session_id,
        "run_token": run_token,
        "current_agent": current_agent,
        "assigned_role": assigned_role,
        "workspace_path": str(Path(workspace_path)),
        "updated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    })
    if task_title:
        data["task_title"] = task_title
    data.setdefault("summary", "Task assigned; agent has not written a handoff summary yet.")
    data.setdefault("outputs", [])
    data.setdefault("artifacts", [])
    data.setdefault("signals_to_next", "")
    data.setdefault("blockers", "none")
    _write_status(path, data, _status_body(existing["content"]) if same_assignment and not overwrite else "")
    return path


def update_status_file(workspace_path: str | Path, updates: dict[str, Any]) -> Path:
    """Merge updates into a worktree-local STATUS.md frontmatter file."""
    path = status_path_for_workspace(workspace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_status_file(workspace_path)
    data = dict(existing["frontmatter"]) if existing["exists"] else {}
    data.update(updates)
    data["workspace_path"] = str(Path(workspace_path))
    data["updated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    _write_status(path, data, _status_body(existing["content"]))
    return path


def available_handoff_agents(
    prefs: Optional[Preferences] = None,
    adapters: Optional[Iterable[AdapterSpec]] = None,
) -> list[str]:
    """Return agent names that can participate in branch-local handoff.

    Role assignments are the active team. Adapter YAMLs and installed popular
    CLIs are candidates and are included so setup screens/CLI output can explain
    the whole handoff surface without registering anything.
    """
    _normalize = lambda n: (n or "").strip().lower()
    names: set[str] = set()
    # Bundled choices must be visible even before the user's adapter cache is seeded.
    for path in BUNDLED_ADAPTERS_DIR.glob("*.yaml"):
        spec = load_adapter(path)
        if spec and not spec.deprecated:
            names.add(_normalize(spec.name))
    # Include locally installed/custom adapters.
    try:
        from agent_kanban_pm.runtime.adapter_loader import load_all_adapters
        names.update(_normalize(a.name) for a in load_all_adapters())
    except Exception:
        pass
    prefs = prefs if prefs is not None else load_preferences()
    if prefs:
        names.update(_normalize(a.agent) for a in prefs.get_role_assignments().values())
    if adapters:
        names.update(_normalize(a.name) for a in adapters)
    names.update(_normalize(item.command) for item in discover_popular_clis() if item.installed)
    return sorted(name for name in names if name)


def build_handoff_instructions(agent_name: str, workspace_path: str | Path) -> str:
    profile = profile_for_agent(agent_name)
    status_path = status_path_for_workspace(workspace_path)
    owns = ", ".join(profile.owns) if profile.owns else "nothing except branch-local STATUS.md"
    review_line = (
        "You are review-only: do not edit product code; write review findings/comments and update this worktree's STATUS.md."
        if profile.review_only
        else "Do the assigned work only inside your owned paths unless the task explicitly says otherwise."
    )
    return (
        "Multi-agent handoff protocol:\n"
        "- Read AGENTS.md for instructions only; do not write mutable state there.\n"
        "- Agent-specific instruction aliases CLAUDE.md and CODEX.md should be symlinks to AGENTS.md.\n"
        f"- Agent profile: {profile.agent} -> {profile.role}.\n"
        f"- Owned paths: {owns}.\n"
        "- The Kanban session record is the durable handoff source of truth; this worktree's STATUS.md is a readable report imported for this run.\n"
        f"- Read and update {status_path}; do not write sibling worktree STATUS.md files.\n"
        "- On takeover, read STATUS.md first to understand state, outputs, blockers, and signals_to_next.\n"
        f"- {review_line}\n"
        "- Keep task_id, project_id, session_id, and run_token unchanged; they identify this exact run.\n"
        "- Fill STATUS.md with state, handoff_ready, current_agent, summary, outputs, artifacts, self-contained signals_to_next, and blockers.\n"
        "- A git_pr handoff must include an artifact like {kind: pull_request, provider: github, url: https://github.com/owner/repo/pull/123}; the server verifies its merged state and head revision.\n"
        "- When work is ready for another agent or the human, set handoff_ready: true and state: done, completed, or review. If blocked, record blockers and leave handoff_ready: false."
    )
