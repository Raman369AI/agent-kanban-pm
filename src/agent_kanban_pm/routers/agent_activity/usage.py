from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import Optional, Literal
from datetime import UTC, datetime
from pydantic import BaseModel, Field
import logging

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    AgentSession, Entity, EntityType, UsageRecord, QuotaSnapshot,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager

logger = logging.getLogger(__name__)
router = APIRouter()


class UsageReport(BaseModel):
    model: Optional[str] = None
    provider: Optional[str] = None
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    cache_read_tokens: Optional[int] = Field(default=None, ge=0)
    cache_write_tokens: Optional[int] = Field(default=None, ge=0)
    reasoning_tokens: Optional[int] = Field(default=None, ge=0)
    cost_usd: Optional[float] = Field(default=None, ge=0)
    cost_source: Optional[Literal["reported", "priced"]] = None
    source: Literal["reported", "transcript", "db", "pane"] = "reported"
    external_session_id: str = Field(default="session", min_length=1, max_length=255)
    attribution: Literal["exact", "approximate"] = "exact"


class QuotaReport(BaseModel):
    cli: str = Field(min_length=1, max_length=100)
    plan_type: Optional[str] = Field(default=None, max_length=100)
    window: str = Field(default="primary", min_length=1, max_length=50)
    used_percent: float = Field(ge=0, le=100)
    window_minutes: Optional[int] = Field(default=None, ge=1)
    resets_at: Optional[datetime] = None
    source: Literal["reported", "transcript", "db", "pane"] = "reported"


def _dt(value):
    return value.isoformat() if value else None


def _quota_payload(row: QuotaSnapshot) -> dict:
    now = datetime.now(UTC).replace(tzinfo=None)
    reset = row.resets_at
    if reset is not None and reset.tzinfo is not None:
        reset = reset.astimezone(UTC).replace(tzinfo=None)
    stale = reset is not None and reset <= now
    return {
        "id": row.id, "agent_id": row.agent_id, "cli": row.cli,
        "plan_type": row.plan_type, "window": row.window,
        "used_percent": row.used_percent,
        "remaining_percent": max(0.0, 100.0 - row.used_percent),
        "window_minutes": row.window_minutes, "resets_at": _dt(row.resets_at),
        "source": row.source, "observed_at": _dt(row.observed_at), "stale": stale,
    }


def _require_authenticated(entity: Optional[Entity]) -> Entity:
    if not entity:
        raise HTTPException(status_code=401, detail="Authentication required")
    return entity


async def _latest_quota_payloads(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(
        select(QuotaSnapshot).order_by(
            QuotaSnapshot.cli, QuotaSnapshot.window,
            desc(QuotaSnapshot.observed_at), desc(QuotaSnapshot.id),
        )
    )).scalars().all()
    latest, seen = [], set()
    for row in rows:
        key = (row.cli, row.window)
        if key not in seen:
            latest.append(_quota_payload(row))
            seen.add(key)
    return latest


@router.post("/sessions/{session_id}/usage")
async def report_session_usage(
    session_id: int,
    report: UsageReport,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Upsert cumulative counters so re-reporting never double-counts."""
    current_entity = _require_authenticated(current_entity)
    agent_session = await db.get(AgentSession, session_id)
    if not agent_session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_session.agent_id:
        raise HTTPException(status_code=403, detail="Agents can only report their own usage")
    entity = await db.get(Entity, agent_session.agent_id)
    record = await db.scalar(select(UsageRecord).where(
        UsageRecord.session_id == session_id,
        UsageRecord.source == report.source,
        UsageRecord.external_session_id == report.external_session_id,
    ))
    values = report.model_dump()
    if record is None:
        record = UsageRecord(
            session_id=session_id, task_id=agent_session.task_id,
            project_id=agent_session.project_id, agent_id=agent_session.agent_id,
            role=agent_session.assigned_role,
            cli=entity.name if entity else f"agent-{agent_session.agent_id}",
            **values,
        )
        db.add(record)
    else:
        for key, value in values.items():
            setattr(record, key, value)
        record.captured_at = datetime.now(UTC)
    if report.model:
        agent_session.resolved_model = report.model
    await db.commit()
    await db.refresh(record)
    return {"id": record.id, "session_id": session_id, "captured_at": _dt(record.captured_at)}


@router.post("/quota", status_code=status.HTTP_201_CREATED)
async def report_agent_quota(
    report: QuotaReport,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    current_entity = _require_authenticated(current_entity)
    if current_entity.entity_type == EntityType.AGENT and current_entity.name != report.cli:
        raise HTTPException(status_code=403, detail="Agents can only report their own quota")
    if current_entity.entity_type == EntityType.HUMAN and not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=403, detail="Only managers can report agent quota")
    agent = await db.scalar(select(Entity).where(
        Entity.entity_type == EntityType.AGENT, Entity.name == report.cli,
    ))
    snapshot = QuotaSnapshot(
        agent_id=agent.id if agent else None, **report.model_dump(),
        observed_at=datetime.now(UTC),
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return _quota_payload(snapshot)


@router.get("/quota")
async def get_agent_quota(
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    _require_authenticated(current_entity)
    return await _latest_quota_payloads(db)


@router.get("/usage")
async def get_agent_usage(
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Return authenticated, dashboard-ready per-agent token totals."""
    _require_authenticated(current_entity)
    query = select(UsageRecord)
    if project_id is not None:
        query = query.where(UsageRecord.project_id == project_id)
    if task_id is not None:
        query = query.where(UsageRecord.task_id == task_id)
    records = (await db.execute(query)).scalars().all()

    from agent_kanban_pm.runtime.preferences import load_preferences
    configured: dict[str, set[str]] = {}
    routing: dict[str, list[dict]] = {}
    prefs = load_preferences()
    if prefs:
        for role, assignment in prefs.get_role_assignments().items():
            configured.setdefault(assignment.agent, set()).add(role)
            candidates = [assignment.agent] + [item.agent for item in assignment.fallbacks]
            if assignment.fallbacks:
                for priority, cli in enumerate(candidates):
                    routing.setdefault(cli, []).append({
                        "role": role, "strategy": assignment.routing.strategy,
                        "priority": priority, "primary": priority == 0,
                    })
            for fallback in assignment.fallbacks:
                configured.setdefault(fallback.agent, set()).add(role)
    for record in records:
        configured.setdefault(record.cli, set())

    def empty(cli, roles=()):
        return {
            "cli": cli, "roles": set(roles), "sessions": set(),
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "reasoning_tokens": 0,
            "cost_usd": 0.0, "has_cost": False, "tracked": False,
            "models": set(), "routing": routing.get(cli, []),
        }

    totals = {cli: empty(cli, roles) for cli, roles in configured.items()}
    for record in records:
        item = totals.setdefault(record.cli, empty(record.cli))
        item["sessions"].add(record.session_id)
        if record.role:
            item["roles"].add(record.role)
        if record.model:
            item["models"].add(record.model)
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
            value = getattr(record, field)
            if value is not None:
                item[field] += value
                item["tracked"] = True
        if record.cost_usd is not None:
            item["cost_usd"] += record.cost_usd
            item["has_cost"] = True

    quotas = {
        row["cli"]: row for row in await _latest_quota_payloads(db)
        if row["window"] == "primary" and not row["stale"]
    }
    result = []
    for cli in sorted(totals):
        item = totals[cli]
        item["sessions"] = len(item["sessions"])
        item["roles"] = sorted(item["roles"])
        item["models"] = sorted(item["models"])
        item["total_tokens"] = item["input_tokens"] + item["output_tokens"] + item["reasoning_tokens"]
        item["cost_usd"] = round(item["cost_usd"], 6) if item.pop("has_cost") else None
        item["quota"] = quotas.get(cli)
        result.append(item)
    return result
