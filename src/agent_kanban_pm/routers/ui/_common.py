from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from datetime import UTC, datetime
import logging
import re

from agent_kanban_pm.models import (
    Task, Entity, Stage, ActivityType,
)
from agent_kanban_pm.runtime.stage_identity import normalize_stage_key
from agent_kanban_pm.runtime.instance import get_csrf_token
from agent_kanban_pm.runtime.paths import templates_dir
from agent_kanban_pm.runtime.pty_manager import strip_ansi


logger = logging.getLogger(__name__)

# Initialize templates
templates = Jinja2Templates(directory=str(templates_dir()))

# Every page embeds the per-instance CSRF token as a meta tag; the fetch
# wrapper in base.html forwards it as X-CSRF-Token on mutations. Derived from
# the auth token, so no per-session storage is needed.
templates.env.globals["current_year"] = lambda: datetime.now(UTC).year
templates.env.globals["kanban_csrf_token"] = get_csrf_token

# Human-readable labels for raw TaskStatus values ("in_progress" -> "In
# progress"). Keeps board badges readable without changing API contracts.
STATUS_LABELS = {
    "pending": "Pending",
    "in_progress": "In progress",
    "in_review": "In review",
    "completed": "Completed",
    "blocked": "Blocked",
}

ACTIVITY_LABELS = {
    ActivityType.THOUGHT.value: "Thought",
    ActivityType.ACTION.value: "Action",
    ActivityType.OBSERVATION.value: "Output",
    ActivityType.RESULT.value: "Result",
    ActivityType.ERROR.value: "Error",
    ActivityType.FILE_CHANGE.value: "File change",
    ActivityType.COMMAND.value: "Command",
    ActivityType.TOOL_CALL.value: "Tool call",
    ActivityType.HANDOFF.value: "Handoff",
}

MEANINGFUL_ACTIVITY_TYPES = (
    ActivityType.ERROR,
    ActivityType.HANDOFF,
    ActivityType.RESULT,
    ActivityType.TOOL_CALL,
    ActivityType.ACTION,
    ActivityType.COMMAND,
    ActivityType.FILE_CHANGE,
)


def _status_label(value) -> str:
    key = getattr(value, "value", value)
    return STATUS_LABELS.get(str(key), str(key).replace("_", " "))


def _activity_preview(message: str, limit: int = 180) -> str:
    """Return safe, compact text for a board card activity preview."""
    cleaned = strip_ansi(message or "")
    cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        return cleaned[:limit - 1].rstrip() + "…"
    return cleaned


def _utc_iso(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


templates.env.filters["status_label"] = _status_label


def _stage_name_matches(stage: Optional[Stage], *names: str) -> bool:
    if not stage or not stage.name:
        return False
    return stage.key in {normalize_stage_key(n) for n in names}


async def _notify_stage_policy_for_todo(
    task: Task,
    current_entity: Entity,
    db: AsyncSession,
) -> Optional[dict]:
    """Check stage policy for a card moved to To Do and return a policy hint.
    
    Does NOT auto-assign. Returns the policy expectations so the UI can
    display them. The orchestrator or human makes the actual assignment.
    """
    if not _stage_name_matches(task.stage, "to do", "todo"):
        return None

    try:
        from agent_kanban_pm.runtime.stage_policy import get_stage_policy_for_stage, policy_roles
    except ImportError:
        return None

    stage = task.stage
    if not stage:
        return None

    policy = await get_stage_policy_for_stage(db, task.project_id, stage.id)
    if not policy:
        return None

    roles = policy_roles(policy)
    if not roles:
        return None

    return {
        "stage_key": policy.stage_key,
        "expected_roles": roles,
        "required_outputs": policy_outputs_if_available(policy),
        "message": f"Stage '{policy.stage_key}' expects roles: {', '.join(roles)}. Orchestrator or human should assign.",
    }


def policy_outputs_if_available(policy) -> list[str]:
    try:
        from agent_kanban_pm.runtime.stage_policy import policy_outputs
        return policy_outputs(policy)
    except Exception as exc:
        logger.warning("Could not load policy_outputs: %s", exc)
        return []


def _plan_items_from_chat(text: str) -> list[dict]:
    raw_lines = [
        re.sub(r"^\s*[-*0-9.)\[\] ]+", "", line).strip()
        for line in text.splitlines()
        if line.strip()
    ]
    if len(raw_lines) > 1:
        items = raw_lines[:8]
    else:
        base = raw_lines[0] if raw_lines else "Requested work"
        items = [
            f"Clarify scope for {base}",
            f"Implement {base}",
            f"Add tests or verification for {base}",
            f"Review and document {base}",
        ]
    return [
        {
            "title": item[:255],
            "description": f"Created from chat request:\n\n{text.strip()}",
            "priority": max(0, 10 - index),
        }
        for index, item in enumerate(items)
    ]
