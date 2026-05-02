"""Data-driven prompt pattern detection for headless CLI approval capture.

Prompt patterns identify when a CLI agent is blocked on an interactive
prompt (y/n, allow/deny, numbered menu) inside a tmux pane. The supervisor
and session streamer use these patterns to:

  1. Detect the prompt in captured pane text
  2. File an AgentApproval record so a human can approve/reject
  3. Resume the CLI by sending the appropriate keystroke

Patterns come from three sources (loaded in priority order):
  1. BUILTIN_PATTERNS — shipped with the codebase
  2. Adapter YAML prompt_patterns sections — per-CLI overrides
  3. ~/.kanban/prompt_patterns.yaml — user-defined overrides

No Python changes are needed to add patterns for a new CLI tool.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

USER_PATTERNS_PATH = Path.home() / ".kanban" / "prompt_patterns.yaml"


@dataclass(frozen=True)
class PromptPattern:
    """One prompt detection rule."""
    regex: re.Pattern
    approval_type: str
    approve_reply: str
    reject_reply: str
    source: str = "builtin"


# ---------------------------------------------------------------------------
# Builtin patterns (identical to the prior hardcoded PROMPT_PATTERNS list)
# ---------------------------------------------------------------------------

BUILTIN_PATTERNS: List[PromptPattern] = [
    # Claude Code numbered-menu prompts (edit / apply / plan confirmation)
    PromptPattern(
        regex=re.compile(
            r"(Do you want to make this edit"
            r"|Apply this change\?"
            r"|Do you want to apply"
            r"|Esc to cancel.*Tab to amend"
            r"|Tab to amend)",
            re.IGNORECASE | re.DOTALL,
        ),
        approval_type="tool_call",
        approve_reply="1",
        reject_reply="3",
    ),
    # Existing tool-call / plan-mode patterns
    PromptPattern(
        regex=re.compile(
            r"(Action Required|Answer Questions|Do you want to proceed\?|Which approach do you prefer\?|Allow once|Allow for this session|No, suggest changes|Enter to select)",
            re.IGNORECASE | re.DOTALL,
        ),
        approval_type="tool_call",
        approve_reply="1",
        reject_reply="3",
    ),
    PromptPattern(
        regex=re.compile(r"\b(allow|approve|run|execute)\b.*\?\s*\[?y/?N?\]?\s*$", re.IGNORECASE),
        approval_type="shell_command",
        approve_reply="y",
        reject_reply="n",
    ),
    # Fixed: removed broken \s*$ anchor so it matches multi-line terminal output
    PromptPattern(
        regex=re.compile(r"do you want to (apply|write|create|edit|push|commit).*\?", re.IGNORECASE),
        approval_type="file_write",
        approve_reply="y",
        reject_reply="n",
    ),
    PromptPattern(
        regex=re.compile(r"create (a )?(pull request|pr).*\?\s*\(?y/?n\)?", re.IGNORECASE),
        approval_type="pr_create",
        approve_reply="y",
        reject_reply="n",
    ),
    PromptPattern(
        regex=re.compile(r"push to remote.*\?\s*\(?y/?n\)?", re.IGNORECASE),
        approval_type="git_push",
        approve_reply="y",
        reject_reply="n",
    ),
    PromptPattern(
        regex=re.compile(r"\(y/n\)\s*[:?]?\s*$", re.IGNORECASE),
        approval_type="tool_call",
        approve_reply="y",
        reject_reply="n",
    ),
]


def _parse_yaml_pattern(entry: dict, source: str) -> Optional[PromptPattern]:
    """Parse a single prompt pattern dict from YAML into a PromptPattern."""
    regex_str = entry.get("regex")
    if not regex_str:
        logger.warning("Prompt pattern from %s missing 'regex' field, skipping", source)
        return None
    try:
        compiled = re.compile(regex_str, re.IGNORECASE | re.DOTALL)
    except re.error as exc:
        logger.warning("Invalid regex in prompt pattern from %s: %s (%s)", source, regex_str, exc)
        return None
    return PromptPattern(
        regex=compiled,
        approval_type=entry.get("type", "tool_call"),
        approve_reply=entry.get("approve", "y"),
        reject_reply=entry.get("reject", "n"),
        source=source,
    )


def _load_user_patterns() -> List[PromptPattern]:
    """Load user-defined prompt patterns from ~/.kanban/prompt_patterns.yaml."""
    if not USER_PATTERNS_PATH.exists():
        return []
    try:
        data = yaml.safe_load(USER_PATTERNS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            logger.warning("~/.kanban/prompt_patterns.yaml should be a YAML list")
            return []
        patterns = []
        for entry in data:
            if isinstance(entry, dict):
                pattern = _parse_yaml_pattern(entry, f"user:{USER_PATTERNS_PATH.name}")
                if pattern:
                    patterns.append(pattern)
        return patterns
    except Exception as exc:
        logger.warning("Failed to load user prompt patterns: %s", exc)
        return []


def _load_adapter_patterns() -> List[PromptPattern]:
    """Load prompt patterns declared in adapter YAML files."""
    try:
        from kanban_runtime.adapter_loader import load_all_adapters
        adapters = load_all_adapters()
    except Exception:
        return []

    patterns = []
    for adapter in adapters:
        if not adapter.prompt_patterns:
            continue
        for spec in adapter.prompt_patterns:
            entry = {"regex": spec.regex, "type": spec.type, "approve": spec.approve, "reject": spec.reject}
            pattern = _parse_yaml_pattern(entry, f"adapter:{adapter.name}")
            if pattern:
                patterns.append(pattern)
    return patterns


def load_patterns() -> List[PromptPattern]:
    """Load all prompt patterns from builtin + adapters + user overrides.

    Order: builtin first, then adapter-specific, then user overrides.
    Later patterns take priority when multiple match (detect_prompt returns
    the first match, so user overrides should be prepended if they need to
    win — but for now, adapter and user patterns are appended so they
    catch prompts that builtins miss).
    """
    patterns: List[PromptPattern] = list(BUILTIN_PATTERNS)
    patterns.extend(_load_adapter_patterns())
    patterns.extend(_load_user_patterns())
    return patterns


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
    for pattern in load_patterns():
        match = pattern.regex.search(tail)
        if match:
            return tail[-1000:], pattern.approval_type, pattern.approve_reply, pattern.reject_reply
    return None


# ---------------------------------------------------------------------------
# Legacy compatibility: PROMPT_PATTERNS as a list of tuples
# ---------------------------------------------------------------------------

def _as_legacy_tuple(p: PromptPattern) -> Tuple[re.Pattern, str, str, str]:
    return (p.regex, p.approval_type, p.approve_reply, p.reject_reply)


# Exported for backward compatibility with any code that still references
# the old PROMPT_PATTERNS list directly.
PROMPT_PATTERNS: List[Tuple[re.Pattern, str, str, str]] = [
    _as_legacy_tuple(p) for p in BUILTIN_PATTERNS
]
"""Deprecated: use load_patterns() and detect_prompt() from this module."""
