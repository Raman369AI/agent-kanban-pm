from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select

import tests_helper
from agent_kanban_pm.app import app
from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    AgentSession, AgentSessionStatus, ApprovalStatus, Entity, EntityType,
    Project, Role, UsageRecord,
)
from agent_kanban_pm.runtime.routing import RouteCandidate, choose_route
from agent_kanban_pm.runtime.usage_collectors import _collect_claude, _collect_codex


def test_ordered_routing_keeps_primary_when_quota_is_unknown():
    decision = choose_route(
        [RouteCandidate("claude"), RouteCandidate("codex")],
        available={"claude": True, "codex": True},
        used_percent={"claude": None, "codex": 10},
    )
    assert decision.agent == "claude"
    assert "quota unavailable" in decision.reason


def test_ordered_routing_uses_explicit_fallback_when_primary_is_low():
    decision = choose_route(
        [RouteCandidate("claude"), RouteCandidate("copilot", "gpt-5")],
        available={"claude": True, "copilot": True},
        used_percent={"claude": 96, "copilot": 25},
        min_headroom_percent=10,
    )
    assert decision.agent == "copilot"
    assert decision.model == "gpt-5"
    assert decision.rejected == ("claude: 4.0% headroom",)


def test_headroom_routing_selects_most_available_configured_agent():
    decision = choose_route(
        [RouteCandidate("claude"), RouteCandidate("codex"), RouteCandidate("copilot")],
        available={"claude": True, "codex": True, "copilot": True},
        used_percent={"claude": 70, "codex": 20, "copilot": 40},
        strategy="headroom",
    )
    assert decision.agent == "codex"


def test_codex_collector_extracts_only_numeric_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = str(tmp_path / "worktree")
    log_dir = tmp_path / ".codex" / "sessions" / "2026" / "09" / "20"
    log_dir.mkdir(parents=True)
    events = [
        {"type": "session_meta", "payload": {"id": "codex-run", "cwd": workspace, "private": "never store"}},
        {"type": "turn_context", "payload": {"model": "gpt-test", "prompt": "secret"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 120, "cached_input_tokens": 30, "output_tokens": 25, "reasoning_output_tokens": 5}
        }, "rate_limits": {"primary": {"used_percent": 76, "window_minutes": 300, "resets_at": 1_800_000_000}}}},
    ]
    (log_dir / "rollout-test.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    snapshot = _collect_codex(workspace, now, now)
    assert snapshot.external_session_id == "codex-run"
    assert snapshot.model == "gpt-test"
    assert snapshot.input_tokens == 120
    assert snapshot.cache_read_tokens == 30
    assert snapshot.quotas[0]["used_percent"] == 76
    assert not hasattr(snapshot, "prompt")


def test_claude_collector_sums_assistant_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = str(tmp_path / "worktree")
    log_dir = tmp_path / ".claude" / "projects" / "demo"
    log_dir.mkdir(parents=True)
    events = [
        {"type": "user", "cwd": workspace, "sessionId": "claude-run", "message": "secret"},
        {"type": "assistant", "cwd": workspace, "sessionId": "claude-run", "message": {
            "model": "claude-test", "content": "private", "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 2}
        }},
        {"type": "assistant", "cwd": workspace, "sessionId": "claude-run", "message": {
            "model": "claude-test", "usage": {"input_tokens": 7, "output_tokens": 3, "cache_creation_input_tokens": 5}
        }},
    ]
    (log_dir / "session.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8"
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    snapshot = _collect_claude(workspace, now, now)
    assert snapshot.external_session_id == "claude-run"
    assert snapshot.input_tokens == 17
    assert snapshot.output_tokens == 7
    assert snapshot.cache_read_tokens == 2
    assert snapshot.cache_write_tokens == 5


def test_usage_reports_are_authenticated_and_idempotent(tmp_path):
    with TestClient(app) as client:
        owner, headers = tests_helper.local_owner_headers(client)

        async def seed():
            async with async_session_maker() as db:
                agent = Entity(
                    name="usage-agent", entity_type=EntityType.AGENT,
                    role=Role.WORKER, is_active=True,
                )
                project = Project(
                    name="Usage project", path=str(tmp_path),
                    creator_id=owner["id"], approval_status=ApprovalStatus.APPROVED,
                )
                db.add_all([agent, project])
                await db.flush()
                run = AgentSession(
                    agent_id=agent.id, project_id=project.id, workspace_path=str(tmp_path),
                    assigned_role="worker", status=AgentSessionStatus.DONE,
                    ended_at=datetime.now(UTC),
                )
                db.add(run)
                await db.commit()
                return project.id, run.id

        project_id, session_id = asyncio.run(seed())
        first = client.post(
            f"/agents/sessions/{session_id}/usage", headers=headers,
            json={"input_tokens": 100, "output_tokens": 20, "model": "model-a"},
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/agents/sessions/{session_id}/usage", headers=headers,
            json={"input_tokens": 150, "output_tokens": 30, "model": "model-a"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["id"] == first.json()["id"]

        quota = client.post(
            "/agents/quota", headers=headers,
            json={"cli": "usage-agent", "used_percent": 82, "window": "primary"},
        )
        assert quota.status_code == 201, quota.text

        rows = client.get(f"/agents/usage?project_id={project_id}", headers=headers).json()
        row = next(item for item in rows if item["cli"] == "usage-agent")
        assert row["input_tokens"] == 150
        assert row["output_tokens"] == 30
        assert row["total_tokens"] == 180
        assert row["quota"]["remaining_percent"] == 18

        async def count_records():
            async with async_session_maker() as db:
                return len((await db.execute(
                    select(UsageRecord).where(UsageRecord.session_id == session_id)
                )).scalars().all())

        assert asyncio.run(count_records()) == 1
