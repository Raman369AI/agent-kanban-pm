"""
Adapter Registry Loader

Scans ~/.kanban/agents/*.yaml for user adapter definitions.
On startup, copies bundled adapters from kanban_runtime/data/agents if
the user directory is empty.
Upserts Entity rows so adapters become visible to the manager.

No Python changes are needed to add a new agent — just drop a YAML file.
"""

import os
import shutil
import logging
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from database import async_session_maker
from models import Entity, EntityType, Role
from sqlalchemy import select
from kanban_runtime.preferences import load_preferences
from kanban_runtime.paths import bundled_adapters_dir

logger = logging.getLogger(__name__)

BUNDLED_ADAPTERS_DIR = bundled_adapters_dir()
USER_ADAPTERS_DIR = Path.home() / ".kanban" / "agents"


POPULAR_CLI_TOOLS = [
    ("claude", "Claude Code"),
    ("gemini", "Gemini CLI"),
    ("codex", "Codex CLI"),
    ("opencode", "OpenCode"),
    ("aider", "Aider"),
    ("goose", "Goose"),
    ("crush", "Crush"),
    ("continue", "Continue"),
    ("amp", "Amp"),
    ("cursor-agent", "Cursor Agent"),
    ("qwen", "Qwen Code"),
]


# ---------------------------------------------------------------------------
# Pydantic schemas for adapter YAML validation
# ---------------------------------------------------------------------------

class InvokeSpec(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    command: str
    mcp_flag: Optional[str] = None
    info_flag: Optional[str] = None
    model_flag: Optional[str] = None


class ModelSpec(BaseModel):
    id: str
    context_window: int = 128000


class AuthSpec(BaseModel):
    type: str = "env_key"
    env_var: Optional[str] = None


class ReportingSpec(BaseModel):
    heartbeat_interval: int = 30


class ChatDesignerSpec(BaseModel):
    prompt_flag: str = "-p"
    stdin: bool = False
    output_format: str = "stdout"
    timeout_seconds: int = 120
    extra_args: List[str] = Field(default_factory=list)


class TaskCommandSpec(BaseModel):
    args: List[str] = Field(default_factory=lambda: ["{prompt}"])
    prompt_file: Optional[str] = None


class PromptPatternSpec(BaseModel):
    """A single prompt detection rule defined in adapter YAML."""
    regex: str
    type: str = "tool_call"
    approve: str = "y"
    reject: str = "n"


class AdapterSpec(BaseModel):
    name: str
    display_name: str
    version: str = "1.0"
    invoke: InvokeSpec
    capabilities: List[str] = Field(default_factory=list)
    models: List[ModelSpec] = Field(default_factory=list)
    protocol: str = "mcp"
    auth: AuthSpec = Field(default_factory=AuthSpec)
    roles: List[str] = Field(default_factory=lambda: ["worker"])
    modes: List[str] = Field(default_factory=lambda: ["supervised", "auto"])
    reporting: ReportingSpec = Field(default_factory=ReportingSpec)
    chat_designer: ChatDesignerSpec = Field(default_factory=ChatDesignerSpec)
    task_command: TaskCommandSpec = Field(default_factory=TaskCommandSpec)
    prompt_patterns: List[PromptPatternSpec] = Field(default_factory=list)
    owns: List[str] = Field(default_factory=list, description="File/directory patterns this agent owns for handoff routing")
    review_only: bool = Field(default=False, description="If true, agent only reviews — does not own files")


class CliDiscoveryResult(BaseModel):
    command: str
    display_name: str
    path: Optional[str] = None

    @property
    def installed(self) -> bool:
        return self.path is not None


# ---------------------------------------------------------------------------
# Loader logic
# ---------------------------------------------------------------------------

def ensure_user_adapters_dir() -> Path:
    """Create ~/.kanban/agents if it doesn't exist."""
    USER_ADAPTERS_DIR.mkdir(parents=True, exist_ok=True)
    return USER_ADAPTERS_DIR


def copy_bundled_adapters():
    """Copy bundled adapter YAMLs to user dir if user dir has no YAMLs."""
    ensure_user_adapters_dir()
    existing_yamls = list(USER_ADAPTERS_DIR.glob("*.yaml"))
    if existing_yamls:
        logger.info(f"User adapters dir already has {len(existing_yamls)} adapters, skipping copy")
        return

    if not BUNDLED_ADAPTERS_DIR.exists():
        logger.warning(f"Bundled adapters dir not found: {BUNDLED_ADAPTERS_DIR}")
        return

    for src in BUNDLED_ADAPTERS_DIR.glob("*.yaml"):
        dst = USER_ADAPTERS_DIR / src.name
        shutil.copy2(src, dst)
        logger.info(f"Copied bundled adapter: {src.name}")


def load_adapter(path: Path) -> Optional[AdapterSpec]:
    """Load and validate a single adapter YAML."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return AdapterSpec(**data)
    except Exception as e:
        logger.error(f"Failed to load adapter {path.name}: {e}")
        return None


def load_all_adapters() -> List[AdapterSpec]:
    """Load all valid adapters from the user adapters directory."""
    ensure_user_adapters_dir()
    adapters = []
    for path in USER_ADAPTERS_DIR.glob("*.yaml"):
        spec = load_adapter(path)
        if spec:
            adapters.append(spec)
    return adapters


def discover_popular_clis() -> List[CliDiscoveryResult]:
    """Check popular local CLI tools without registering them as agents."""
    return [
        CliDiscoveryResult(
            command=command,
            display_name=display_name,
            path=shutil.which(command),
        )
        for command, display_name in POPULAR_CLI_TOOLS
    ]


def standalone_assignment_to_adapter(role: str, assignment) -> AdapterSpec:
    """Build an in-memory adapter for a role-bound standalone CLI.

    This intentionally does not write an adapter YAML or register a DB entity.
    Discovery remains read-only; explicit role assignment is enough to launch
    the process in tmux.
    """
    command = assignment.command or assignment.agent
    return AdapterSpec(
        name=assignment.agent,
        display_name=assignment.display_name or assignment.agent,
        invoke=InvokeSpec(command=command),
        capabilities=assignment.capabilities or [role],
        models=[
            ModelSpec(id=model_id)
            for model_id in ([assignment.model] if assignment.model else assignment.models)
        ],
        protocol=assignment.protocol or "stdio",
        auth=AuthSpec(type="none"),
        roles=[role],
        modes=[assignment.mode],
    )


def _configured_agent_names() -> set:
    prefs = load_preferences()
    if not prefs:
        return set()
    return {assignment.agent for assignment in prefs.get_role_assignments().values()}


def adapter_role_to_db_role(adapter_roles: List[str]) -> Role:
    """Map adapter roles to the most permissive DB Role.

    Supports both legacy 'manager' and new 'orchestrator' role names.
    """
    role_set = set(adapter_roles)
    if "manager" in role_set or "orchestrator" in role_set:
        return Role.MANAGER
    if "worker" in role_set or "ui" in role_set or "architecture" in role_set or "test" in role_set or "diff_review" in role_set or "git_pr" in role_set:
        return Role.WORKER
    return Role.VIEWER


async def sync_adapters_to_entities():
    """Upsert Entity rows for configured adapter-backed roles.

    Adapter YAML files are candidates. They do not create a local team by
    themselves; role assignment is what makes a CLI a Kanban agent entity.
    Set KANBAN_REGISTER_ALL_ADAPTERS=1 to restore the old eager behavior.
    """
    adapters = load_all_adapters()
    if not adapters:
        logger.info("No adapters found to sync")
        return
    configured_agents = _configured_agent_names()
    register_all = os.getenv("KANBAN_REGISTER_ALL_ADAPTERS") == "1"

    async with async_session_maker() as session:
        result = await session.execute(
            select(Entity).filter(Entity.entity_type == EntityType.AGENT)
        )
        existing_agents = {e.name: e for e in result.scalars().all()}

        for spec in adapters:
            if not register_all:
                if not configured_agents:
                    logger.info("No configured roles; skipping adapter entity creation")
                    break
                if spec.name not in configured_agents:
                    continue
            # Use adapter 'name' (not display_name) for consistency
            # display_name is surfaced in UI only
            name = spec.name
            # Check CLI availability
            cli_available = shutil.which(spec.invoke.command) is not None

            if name in existing_agents:
                entity = existing_agents[name]
                # Update fields from adapter
                entity.skills = ", ".join(spec.capabilities)
                entity.role = adapter_role_to_db_role(spec.roles)
                entity.is_active = cli_available
                logger.info(f"Updated adapter entity: {name} (active={cli_available})")
            else:
                entity = Entity(
                    name=name,
                    entity_type=EntityType.AGENT,
                    skills=", ".join(spec.capabilities),
                    role=adapter_role_to_db_role(spec.roles),
                    is_active=cli_available,
                )
                session.add(entity)
                logger.info(f"Created adapter entity: {name} (active={cli_available})")

        await session.commit()


async def init_adapter_registry():
    """Full initialization: copy bundled adapters, then sync to DB."""
    copy_bundled_adapters()
    await sync_adapters_to_entities()
