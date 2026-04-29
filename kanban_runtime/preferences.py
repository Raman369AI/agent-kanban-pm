"""
Preferences loader for ~/.kanban/preferences.yaml

Supports the 7-role taxonomy from AGENTS.md:
  orchestrator, ui, architecture, worker, test, diff_review, git_pr

Legacy `manager` and `workers` keys are auto-migrated to the new shape.
"""

import os
import re
from pathlib import Path
from typing import List, Optional, Dict
from enum import Enum

import yaml
from pydantic import BaseModel, Field

logger = __import__("logging").getLogger(__name__)

PREFERENCES_PATH = Path.home() / ".kanban" / "preferences.yaml"


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    UI = "ui"
    ARCHITECTURE = "architecture"
    WORKER = "worker"
    TEST = "test"
    DIFF_REVIEW = "diff_review"
    GIT_PR = "git_pr"


class RoleAssignment(BaseModel):
    agent: str
    mode: str = "headless"
    model: Optional[str] = None
    models: List[str] = Field(default_factory=list)
    command: Optional[str] = None
    display_name: Optional[str] = None
    protocol: str = "stdio"
    capabilities: List[str] = Field(default_factory=list)
    prompt_flag: Optional[str] = None
    chat_stdin: Optional[bool] = None
    chat_timeout_seconds: Optional[int] = None

    @property
    def is_standalone_cli(self) -> bool:
        """True when this role directly names a local CLI instead of an adapter."""
        return self.command is not None


class RoleConfig(BaseModel):
    orchestrator: Optional[RoleAssignment] = None
    ui: Optional[RoleAssignment] = None
    architecture: Optional[RoleAssignment] = None
    worker: Optional[RoleAssignment] = None
    test: Optional[RoleAssignment] = None
    diff_review: Optional[RoleAssignment] = None
    git_pr: Optional[RoleAssignment] = None


class WorkerConfig(BaseModel):
    agent: str
    roles: List[str] = Field(default_factory=lambda: ["worker"])


class AutonomyConfig(BaseModel):
    require_approval_for: List[str] = Field(default_factory=lambda: ["project_create", "agent_add"])
    auto_approve: List[str] = Field(default_factory=lambda: ["task_move", "task_assign", "comment"])


class ManagerConfig(BaseModel):
    agent: str
    model: str
    mode: str = "auto"


class Preferences(BaseModel):
    manager: Optional[ManagerConfig] = None
    workers: List[WorkerConfig] = Field(default_factory=list)
    roles: Optional[RoleConfig] = None
    custom_roles: Dict[str, RoleAssignment] = Field(default_factory=dict)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)

    def get_roles(self) -> RoleConfig:
        if self.roles:
            return self.roles
        migrated = RoleConfig()
        if self.manager:
            migrated.orchestrator = RoleAssignment(
                agent=self.manager.agent,
                mode=self.manager.mode,
                model=self.manager.model,
            )
        for w in self.workers:
            for r in w.roles:
                ra = RoleAssignment(agent=w.agent, mode="headless")
                if r == "worker":
                    if migrated.worker is None:
                        migrated.worker = ra
                elif r == "ui":
                    migrated.ui = ra
                elif r == "architecture":
                    migrated.architecture = ra
                elif r == "test":
                    migrated.test = ra
                elif r == "diff_review":
                    migrated.diff_review = ra
                elif r == "git_pr":
                    migrated.git_pr = ra
        return migrated

    def get_role_assignments(self) -> Dict[str, RoleAssignment]:
        rc = self.get_roles()
        assignments = {}
        for field_name in RoleConfig.model_fields:
            ra = getattr(rc, field_name)
            if ra is not None:
                assignments[field_name] = ra
        assignments.update(self.custom_roles)
        return assignments

    def set_role_assignment(self, role_name: str, assignment: RoleAssignment) -> None:
        validate_role_name(role_name)
        if role_name in RoleConfig.model_fields:
            if self.roles is None:
                self.roles = RoleConfig()
            setattr(self.roles, role_name, assignment)
        else:
            self.custom_roles[role_name] = assignment


def validate_role_name(role_name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,39}", role_name or ""):
        raise ValueError("Role must be 2-40 chars: lowercase letters, numbers, underscores; start with a letter")
    return role_name


def load_preferences() -> Optional[Preferences]:
    if not PREFERENCES_PATH.exists():
        return None
    try:
        with open(PREFERENCES_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return Preferences(**data)
    except Exception as e:
        logger.error(f"Failed to load preferences: {e}")
        return None


def save_preferences(prefs: Preferences):
    PREFERENCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PREFERENCES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(prefs.model_dump(exclude_none=True), f, sort_keys=False, allow_unicode=True)
    logger.info(f"Preferences saved to {PREFERENCES_PATH}")


def get_manager_agent_name() -> Optional[str]:
    prefs = load_preferences()
    if not prefs:
        return None
    roles = prefs.get_roles()
    if roles.orchestrator:
        return roles.orchestrator.agent
    if prefs.manager:
        return prefs.manager.agent
    return None


def get_manager_mode() -> str:
    prefs = load_preferences()
    if not prefs:
        return "auto"
    roles = prefs.get_roles()
    if roles.orchestrator:
        return roles.orchestrator.mode
    if prefs.manager:
        return prefs.manager.mode
    return "auto"
