"""
Chat Designer — adapter-agnostic LLM call for backlog plan generation.

Resolves the orchestrator role assignment, shells out to the assigned CLI
with a structured system prompt, and parses the response into a PlanV1.

The contract: the orchestrator must respond with a single JSON block
enclosed in <plan>...</plan>. Conversational text around the block is
ignored. On parse failure, ChatDesigner retries once with a stricter
prompt before raising.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

from kanban_runtime.adapter_loader import (
    AdapterSpec,
    ChatDesignerSpec,
    load_all_adapters,
)
from kanban_runtime.preferences import RoleAssignment, load_preferences


ALLOWED_ROLE_HINTS = {
    "orchestrator", "ui", "architecture", "worker",
    "test", "diff_review", "git_pr",
}

MAX_TASKS = 20
PLAN_BLOCK_RE = re.compile(r"<plan>\s*(\{.*?\})\s*</plan>", re.DOTALL)


# ---------------------------------------------------------------------------
# Plan schema (CLI-side validation; mirrors schemas.ChatPlanItem on server)
# ---------------------------------------------------------------------------

class PlanTask(BaseModel):
    title: str
    description: str = ""
    acceptance: List[str] = Field(default_factory=list)
    priority: int = 5
    role_hint: Optional[str] = None
    depends_on: List[int] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title_non_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title must be non-empty")
        return v[:255]

    @field_validator("priority")
    @classmethod
    def _priority_range(cls, v: int) -> int:
        if v < 0 or v > 10:
            raise ValueError("priority must be in [0, 10]")
        return v

    @field_validator("role_hint")
    @classmethod
    def _role_hint_allowed(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = (v or "").strip().lower() or None
        if v is not None and v not in ALLOWED_ROLE_HINTS:
            raise ValueError(f"role_hint must be one of {sorted(ALLOWED_ROLE_HINTS)} or null")
        return v


class PlanV1(BaseModel):
    version: int = 1
    summary: str = ""
    questions: List[str] = Field(default_factory=list)
    tasks: List[PlanTask] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Designer
# ---------------------------------------------------------------------------

class DesignerError(RuntimeError):
    """Raised when the designer cannot produce a valid plan."""


@dataclass
class DesignerInvocation:
    command: str
    args: List[str]
    use_stdin: bool
    env: dict
    timeout_seconds: int
    adapter_name: str
    display_name: str


SYSTEM_PROMPT = """You are the Orchestrator Agent for an Agent Kanban PM system.
The user is in an interactive terminal chat. They will describe work in
natural language. Decompose it into a small set of backlog cards.

Hard rules:
- Respond with EXACTLY ONE JSON block enclosed in <plan>...</plan>.
- No commentary, no markdown, no extra text outside the <plan> block.
- The JSON object MUST match this shape:
  {
    "version": 1,
    "summary": "<one sentence>",
    "questions": ["<open clarification>", ...],   // [] if none
    "tasks": [
      {
        "title": "<imperative, <=255 chars>",
        "description": "<intent + context>",
        "acceptance": ["<criterion>", ...],         // [] if none
        "priority": <int 0..10>,
        "role_hint": "orchestrator|ui|architecture|worker|test|diff_review|git_pr|null",
        "depends_on": [<index of earlier task>, ...]
      },
      ...
    ]
  }
- depends_on entries are 0-based indices into THIS tasks array. No cycles.
- Cap tasks at 20. Prefer 3-7 well-scoped cards over many tiny ones.
- If something is genuinely ambiguous, list it in `questions` rather than
  guessing — the human will see it before commit.
"""

STRICT_RETRY_SUFFIX = (
    "\n\nYour previous response did not contain a valid <plan>{...}</plan> JSON block. "
    "Re-emit the plan now. Output ONLY the <plan> block, nothing else."
)


def resolve_invocation(
    role_assignment: RoleAssignment,
    adapter: Optional[AdapterSpec],
    *,
    api_base: str = "http://localhost:8000",
) -> DesignerInvocation:
    """Build the subprocess invocation for the orchestrator's chat designer."""
    if adapter is not None:
        adapter_name = adapter.name
        display_name = adapter.display_name
        cd: ChatDesignerSpec = adapter.chat_designer
        command = adapter.invoke.command
        prompt_flag = role_assignment.prompt_flag or cd.prompt_flag
        use_stdin = (
            role_assignment.chat_stdin
            if role_assignment.chat_stdin is not None
            else cd.stdin
        )
        timeout_seconds = role_assignment.chat_timeout_seconds or cd.timeout_seconds
        extra_args = list(cd.extra_args)
        auth_env_var = adapter.auth.env_var if adapter.auth else None
    else:
        command = role_assignment.command or role_assignment.agent
        adapter_name = role_assignment.agent
        display_name = role_assignment.display_name or role_assignment.agent
        prompt_flag = role_assignment.prompt_flag or "-p"
        use_stdin = bool(role_assignment.chat_stdin)
        timeout_seconds = role_assignment.chat_timeout_seconds or 120
        extra_args = []
        auth_env_var = None

    if not shutil.which(command):
        raise DesignerError(
            f"Orchestrator CLI '{command}' not found on PATH. "
            f"Run: python -m kanban_cli agents discover"
        )

    if auth_env_var and auth_env_var not in os.environ:
        raise DesignerError(
            f"Provider env var '{auth_env_var}' is not set; "
            f"the orchestrator CLI ({display_name}) needs it to authenticate."
        )

    args: List[str] = []
    args.extend(extra_args)
    if not use_stdin and prompt_flag:
        # The actual prompt string is appended at run-time; just record the flag.
        pass

    env = os.environ.copy()
    env["KANBAN_AGENT_NAME"] = adapter_name
    env["KANBAN_AGENT_ROLE"] = "orchestrator"
    env["KANBAN_CHAT_MODE"] = "1"
    env["KANBAN_API_BASE"] = env.get("KANBAN_API_BASE", api_base)

    return DesignerInvocation(
        command=command,
        args=args + ([prompt_flag] if (not use_stdin and prompt_flag) else []),
        use_stdin=use_stdin,
        env=env,
        timeout_seconds=timeout_seconds,
        adapter_name=adapter_name,
        display_name=display_name,
    )


def build_prompt(history: List[dict], user_turn: str, *, strict_retry: bool = False) -> str:
    """Assemble the full prompt the orchestrator subprocess receives."""
    parts: List[str] = [SYSTEM_PROMPT]
    if history:
        parts.append("\n--- conversation so far ---")
        for entry in history:
            role = entry.get("role", "user")
            content = (entry.get("content") or "").strip()
            if not content:
                continue
            parts.append(f"{role.upper()}: {content}")
    parts.append("\n--- new user turn ---")
    parts.append(user_turn.strip())
    if strict_retry:
        parts.append(STRICT_RETRY_SUFFIX)
    return "\n".join(parts)


def parse_plan_block(text: str) -> PlanV1:
    """Extract the last <plan>...</plan> block and validate it."""
    if not text:
        raise DesignerError("orchestrator returned empty output")
    matches = PLAN_BLOCK_RE.findall(text)
    if not matches:
        raise DesignerError("no <plan>...</plan> block in orchestrator output")
    raw = matches[-1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DesignerError(f"plan JSON did not parse: {exc}")
    try:
        plan = PlanV1(**data)
    except ValidationError as exc:
        raise DesignerError(f"plan failed validation: {exc}")
    if len(plan.tasks) > MAX_TASKS:
        plan.tasks = plan.tasks[:MAX_TASKS]
    # Validate depends_on indices reference earlier tasks and skip self-refs.
    for idx, task in enumerate(plan.tasks):
        cleaned: List[int] = []
        for dep in task.depends_on:
            if isinstance(dep, int) and 0 <= dep < len(plan.tasks) and dep != idx:
                cleaned.append(dep)
        task.depends_on = cleaned
    return plan


def run_subprocess(invocation: DesignerInvocation, prompt: str) -> str:
    """Run the orchestrator CLI once and return raw stdout."""
    if invocation.use_stdin:
        cmd = [invocation.command, *invocation.args]
        stdin_data: Optional[str] = prompt
    else:
        cmd = [invocation.command, *invocation.args, prompt]
        stdin_data = None

    try:
        proc = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=invocation.timeout_seconds,
            env=invocation.env,
        )
    except subprocess.TimeoutExpired as exc:
        raise DesignerError(
            f"orchestrator timed out after {invocation.timeout_seconds}s"
        ) from exc
    except FileNotFoundError as exc:
        raise DesignerError(f"failed to launch orchestrator CLI: {exc}") from exc

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise DesignerError(
            f"orchestrator exited {proc.returncode}: {stderr[:500] or '<no stderr>'}"
        )
    return proc.stdout or ""


@dataclass
class DesignerResult:
    plan: PlanV1
    raw_output: str
    invocation: DesignerInvocation


class ChatDesigner:
    """Adapter-agnostic wrapper around the orchestrator CLI."""

    def __init__(
        self,
        role_assignment: RoleAssignment,
        adapter: Optional[AdapterSpec],
        *,
        api_base: str = "http://localhost:8000",
    ) -> None:
        self.role_assignment = role_assignment
        self.adapter = adapter
        self.invocation = resolve_invocation(role_assignment, adapter, api_base=api_base)

    @classmethod
    def from_preferences(cls, *, api_base: str = "http://localhost:8000") -> "ChatDesigner":
        prefs = load_preferences()
        if not prefs:
            raise DesignerError(
                "No preferences found. Run: python -m kanban_cli init"
            )
        roles = prefs.get_role_assignments()
        ra = roles.get("orchestrator")
        if ra is None:
            raise DesignerError(
                "No orchestrator role assigned. "
                "Run: python -m kanban_cli roles assign orchestrator <agent>"
            )
        adapters = {a.name: a for a in load_all_adapters()}
        adapter = adapters.get(ra.agent)
        if adapter is None and not ra.command:
            raise DesignerError(
                f"Orchestrator agent '{ra.agent}' has no adapter and no standalone command. "
                "Re-run roles assign with --command for standalone CLIs."
            )
        return cls(ra, adapter, api_base=api_base)

    def design(self, history: List[dict], user_turn: str) -> DesignerResult:
        """One designer turn. history is a list of {role, content} dicts."""
        prompt = build_prompt(history, user_turn)
        raw = run_subprocess(self.invocation, prompt)
        try:
            plan = parse_plan_block(raw)
        except DesignerError:
            retry_prompt = build_prompt(history, user_turn, strict_retry=True)
            raw = run_subprocess(self.invocation, retry_prompt)
            plan = parse_plan_block(raw)
        return DesignerResult(plan=plan, raw_output=raw, invocation=self.invocation)
