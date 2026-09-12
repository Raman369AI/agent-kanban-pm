"""Stable workflow identities, independent of user-facing stage labels."""
import re


def normalize_stage_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return {"todo": "to_do", "completed": "done"}.get(key, key)


def default_stage_key(context):
    return normalize_stage_key(context.get_current_parameters().get("name", ""))


STAGE_STATUSES = {
    "backlog": "pending", "to_do": "pending", "in_progress": "in_progress",
    "review": "in_review", "done": "completed",
}
