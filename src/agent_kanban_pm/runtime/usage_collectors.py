"""Defensive, numeric-only usage collection from local CLI telemetry.

Collectors never persist prompts, responses, tool inputs, or source content.
Unknown and changed log shapes produce no record instead of a false zero.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import AgentSession, Entity, QuotaSnapshot, UsageRecord

logger = logging.getLogger(__name__)


@dataclass
class CollectedUsage:
    source: str
    external_session_id: str
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    quotas: list[dict] = field(default_factory=list)


def _number(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _parse_reset(value) -> Optional[datetime]:
    if isinstance(value, (int, float)):
        # Some CLIs report seconds, others milliseconds.
        if value > 10_000_000_000:
            value /= 1000
        try:
            return datetime.fromtimestamp(value, UTC).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _recent_files(root: Path, pattern: str, started_at: datetime, ended_at: datetime) -> list[Path]:
    if not root.exists():
        return []
    start = started_at.replace(tzinfo=UTC).timestamp() - 3600
    end = ended_at.replace(tzinfo=UTC).timestamp() + 3600
    candidates = []
    try:
        for path in root.glob(pattern):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if start <= mtime <= end:
                candidates.append((mtime, path))
    except OSError:
        return []
    return [path for _mtime, path in sorted(candidates, reverse=True)[:200]]


def _collect_codex(workspace: str, started_at: datetime, ended_at: datetime) -> Optional[CollectedUsage]:
    files = _recent_files(Path.home() / ".codex" / "sessions", "**/rollout-*.jsonl", started_at, ended_at)
    for path in files:
        matched = False
        external_id = path.stem
        model = None
        totals = None
        quotas: list[dict] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    payload = event.get("payload") if isinstance(event, dict) else None
                    if not isinstance(payload, dict):
                        continue
                    if event.get("type") == "session_meta":
                        if payload.get("cwd") == workspace:
                            matched = True
                            external_id = str(payload.get("id") or external_id)
                    elif matched and event.get("type") == "turn_context" and payload.get("model"):
                        model = str(payload["model"])
                    elif matched and payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        candidate = info.get("total_token_usage")
                        if isinstance(candidate, dict):
                            totals = candidate
                        rate_limits = payload.get("rate_limits") or info.get("rate_limits") or {}
                        if isinstance(rate_limits, dict):
                            quotas = []
                            for window, value in rate_limits.items():
                                if not isinstance(value, dict) or not isinstance(value.get("used_percent"), (int, float)):
                                    continue
                                quotas.append({
                                    "window": str(window),
                                    "used_percent": float(value["used_percent"]),
                                    "window_minutes": _number(value.get("window_minutes")),
                                    "resets_at": _parse_reset(value.get("resets_at")),
                                    "plan_type": payload.get("plan_type") or info.get("plan_type"),
                                })
        except OSError:
            continue
        if matched and isinstance(totals, dict):
            return CollectedUsage(
                source="transcript", external_session_id=external_id, model=model,
                input_tokens=_number(totals.get("input_tokens")),
                output_tokens=_number(totals.get("output_tokens")),
                cache_read_tokens=_number(totals.get("cached_input_tokens")),
                reasoning_tokens=_number(totals.get("reasoning_output_tokens")),
                quotas=quotas,
            )
    return None


def _collect_claude(workspace: str, started_at: datetime, ended_at: datetime) -> Optional[CollectedUsage]:
    files = _recent_files(Path.home() / ".claude" / "projects", "**/*.jsonl", started_at, ended_at)
    for path in files:
        matched = False
        external_id = path.stem
        model = None
        totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        observations = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw in handle:
                    try:
                        event = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(event, dict) or event.get("cwd") != workspace:
                        continue
                    matched = True
                    external_id = str(event.get("sessionId") or event.get("session_id") or external_id)
                    if event.get("type") != "assistant":
                        continue
                    message = event.get("message") or {}
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    observations += 1
                    if message.get("model"):
                        model = str(message["model"])
                    totals["input"] += _number(usage.get("input_tokens")) or 0
                    totals["output"] += _number(usage.get("output_tokens")) or 0
                    totals["cache_read"] += _number(usage.get("cache_read_input_tokens")) or 0
                    totals["cache_write"] += _number(usage.get("cache_creation_input_tokens")) or 0
        except OSError:
            continue
        if matched and observations:
            return CollectedUsage(
                source="transcript", external_session_id=external_id, model=model,
                input_tokens=totals["input"], output_tokens=totals["output"],
                cache_read_tokens=totals["cache_read"], cache_write_tokens=totals["cache_write"],
            )
    return None


def _collect(cli: str, workspace: str, started_at: datetime, ended_at: datetime) -> Optional[CollectedUsage]:
    if cli == "codex":
        return _collect_codex(workspace, started_at, ended_at)
    if cli == "claude":
        return _collect_claude(workspace, started_at, ended_at)
    return None


async def collect_session_usage(session_id: int) -> None:
    """Collect and upsert final counters without affecting session outcome."""
    try:
        async with async_session_maker() as db:
            session = await db.get(AgentSession, session_id)
            if not session:
                return
            agent = await db.get(Entity, session.agent_id)
            if not agent or agent.name not in {"codex", "claude"}:
                return
            started = session.started_at or datetime.now(UTC).replace(tzinfo=None)
            ended = session.ended_at or datetime.now(UTC).replace(tzinfo=None)
            workspace = session.workspace_path
            cli = agent.name
        snapshot = await asyncio.to_thread(_collect, cli, workspace, started, ended)
        if snapshot is None:
            return
        async with async_session_maker() as db:
            session = await db.get(AgentSession, session_id)
            if not session:
                return
            record = await db.scalar(select(UsageRecord).where(
                UsageRecord.session_id == session_id,
                UsageRecord.source == snapshot.source,
                UsageRecord.external_session_id == snapshot.external_session_id,
            ))
            values = {
                "model": snapshot.model,
                "input_tokens": snapshot.input_tokens,
                "output_tokens": snapshot.output_tokens,
                "cache_read_tokens": snapshot.cache_read_tokens,
                "cache_write_tokens": snapshot.cache_write_tokens,
                "reasoning_tokens": snapshot.reasoning_tokens,
                "captured_at": datetime.now(UTC),
            }
            if record is None:
                record = UsageRecord(
                    session_id=session.id, task_id=session.task_id,
                    project_id=session.project_id, agent_id=session.agent_id,
                    role=session.assigned_role, cli=cli, source=snapshot.source,
                    external_session_id=snapshot.external_session_id,
                    attribution="exact", **values,
                )
                db.add(record)
            else:
                for key, value in values.items():
                    setattr(record, key, value)
            if snapshot.model:
                session.resolved_model = snapshot.model
            for quota in snapshot.quotas:
                db.add(QuotaSnapshot(
                    agent_id=session.agent_id, cli=cli, source=snapshot.source,
                    observed_at=datetime.now(UTC), **quota,
                ))
            await db.commit()
    except Exception:
        logger.warning("Usage collection failed for session %s", session_id, exc_info=True)
