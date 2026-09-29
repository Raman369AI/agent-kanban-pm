"""Deterministic, opt-in routing across explicitly configured CLI agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Mapping, Optional, Sequence

from sqlalchemy import desc, select

from agent_kanban_pm.models import QuotaSnapshot


@dataclass(frozen=True)
class RouteCandidate:
    agent: str
    model: Optional[str] = None


@dataclass(frozen=True)
class RouteDecision:
    agent: str
    model: Optional[str]
    reason: str
    rejected: tuple[str, ...] = field(default_factory=tuple)


def choose_route(
    candidates: Sequence[RouteCandidate],
    *,
    available: Mapping[str, bool],
    used_percent: Mapping[str, Optional[float]],
    strategy: str = "ordered",
    min_headroom_percent: float = 10.0,
) -> Optional[RouteDecision]:
    """Choose among user-consented candidates without inventing telemetry.

    Unknown quota does not disqualify the primary candidate. This keeps a
    missing collector/report from unexpectedly moving work to another CLI.
    """
    if not candidates:
        return None
    rejected: list[str] = []
    eligible: list[RouteCandidate] = []
    for candidate in candidates:
        if not available.get(candidate.agent, False):
            rejected.append(f"{candidate.agent}: not installed or inactive")
            continue
        used = used_percent.get(candidate.agent)
        if used is not None and (100.0 - used) < min_headroom_percent:
            rejected.append(
                f"{candidate.agent}: {max(0.0, 100.0 - used):.1f}% headroom"
            )
            continue
        eligible.append(candidate)

    if not eligible:
        return None

    primary = candidates[0]
    primary_used = used_percent.get(primary.agent)
    primary_eligible = next((item for item in eligible if item.agent == primary.agent), None)
    normalized_strategy = strategy if strategy in {"ordered", "headroom"} else "ordered"

    if normalized_strategy == "headroom" and primary_used is not None:
        known = [item for item in eligible if used_percent.get(item.agent) is not None]
        selected = min(known, key=lambda item: used_percent[item.agent]) if known else primary_eligible
        if selected is None:
            selected = eligible[0]
        reason = (
            f"highest quota headroom ({100.0 - used_percent[selected.agent]:.1f}% remaining)"
            if used_percent.get(selected.agent) is not None
            else "first available fallback; quota unavailable"
        )
    elif primary_eligible is not None:
        selected = primary_eligible
        reason = "primary agent selected"
        if primary_used is None:
            reason += "; quota unavailable"
    else:
        selected = eligible[0]
        reason = "primary agent below headroom threshold; selected configured fallback"

    return RouteDecision(
        agent=selected.agent,
        model=selected.model,
        reason=reason,
        rejected=tuple(rejected),
    )


async def latest_quota_by_cli(db, cli_names: Sequence[str]) -> dict[str, Optional[float]]:
    """Return current primary-window usage; expired snapshots become unknown."""
    names = list(dict.fromkeys(cli_names))
    if not names:
        return {}
    rows = (await db.execute(
        select(QuotaSnapshot)
        .where(QuotaSnapshot.cli.in_(names), QuotaSnapshot.window == "primary")
        .order_by(QuotaSnapshot.cli, desc(QuotaSnapshot.observed_at), desc(QuotaSnapshot.id))
    )).scalars().all()
    latest: dict[str, Optional[float]] = {name: None for name in names}
    seen: set[str] = set()
    now = datetime.now(UTC).replace(tzinfo=None)
    for row in rows:
        if row.cli in seen:
            continue
        seen.add(row.cli)
        reset = row.resets_at
        if reset is not None and reset.tzinfo is not None:
            reset = reset.astimezone(UTC).replace(tzinfo=None)
        if reset is not None and reset <= now:
            latest[row.cli] = None
        else:
            latest[row.cli] = max(0.0, min(100.0, float(row.used_percent)))
    return latest
