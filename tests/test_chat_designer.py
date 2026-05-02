"""
Smoke tests for AGENTS.md §10 — Chat Designer.

Covers:
  * PlanV1 parsing happy path + malformed input.
  * `parse_plan_block` trims invalid `depends_on` indices.
  * `ChatDesigner.design()` retries once on parse failure.
  * `/ui/tasks/chat-plan` end-to-end with `items=[...]`.
  * `_render_acceptance` / `_render_dependencies` checklist rendering.
  * `kanban_cli.chat.drop_task` rewires depends_on indices.

Run with: pytest tests/test_chat_designer.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import tests_helper  # noqa: F401  — auto-clean throwaway entities/projects on exit

from fastapi.testclient import TestClient

from main import app
from kanban_cli.chat_designer import (
    ChatDesigner,
    DesignerError,
    DesignerInvocation,
    PlanV1,
    PlanTask,
    parse_plan_block,
)
from kanban_cli.chat import drop_task, render_plan
from kanban_runtime.preferences import RoleAssignment


SAMPLE_OUTPUT = """\
Some pre-amble the orchestrator might add.
<plan>{"version":1,"summary":"Add OAuth login.","questions":["Force migrate?"],"tasks":[
  {"title":"Design OAuth provider abstraction","description":"Spec the interface.","acceptance":["covers refresh"],"priority":9,"role_hint":"architecture","depends_on":[]},
  {"title":"Implement callback route","description":"Server-side flow.","acceptance":[],"priority":7,"role_hint":"worker","depends_on":[0]},
  {"title":"Wire frontend button","description":"UI piece.","priority":6,"role_hint":"ui","depends_on":[0,99]}
]}</plan>
trailing comment.
"""


def test_parse_happy_path():
    plan = parse_plan_block(SAMPLE_OUTPUT)
    assert isinstance(plan, PlanV1)
    assert len(plan.tasks) == 3
    assert plan.tasks[0].role_hint == "architecture"
    # depends_on=[0,99] gets trimmed to [0] (out-of-range index dropped)
    assert plan.tasks[2].depends_on == [0]
    print("PASS test_parse_happy_path")


def test_parse_no_block():
    try:
        parse_plan_block("hello, here is a plan: do thing 1, do thing 2")
    except DesignerError as exc:
        print(f"PASS test_parse_no_block: {exc}")
        return
    raise AssertionError("expected DesignerError when no <plan> block present")


def test_parse_invalid_role_hint():
    bad = '<plan>{"version":1,"summary":"","questions":[],"tasks":[{"title":"A","description":"d","priority":3,"role_hint":"unknown_role"}]}</plan>'
    try:
        parse_plan_block(bad)
    except DesignerError as exc:
        print(f"PASS test_parse_invalid_role_hint: {str(exc)[:80]}")
        return
    raise AssertionError("expected DesignerError on invalid role_hint")


def test_parse_priority_out_of_range():
    bad = '<plan>{"version":1,"summary":"","tasks":[{"title":"A","description":"d","priority":42,"role_hint":null,"depends_on":[]}]}</plan>'
    try:
        parse_plan_block(bad)
    except DesignerError as exc:
        print(f"PASS test_parse_priority_out_of_range: {str(exc)[:80]}")
        return
    raise AssertionError("expected DesignerError on priority>10")


def test_designer_retries_on_first_failure():
    """ChatDesigner.design() should retry once when parse fails on first try."""

    role = RoleAssignment(agent="fake", command="echo", mode="headless")
    designer = ChatDesigner.__new__(ChatDesigner)
    designer.role_assignment = role
    designer.adapter = None
    designer.invocation = DesignerInvocation(
        command="echo",
        args=[],
        use_stdin=False,
        env=os.environ.copy(),
        timeout_seconds=5,
        adapter_name="fake",
        display_name="fake",
    )

    calls = {"n": 0}
    valid = '<plan>{"version":1,"summary":"ok","tasks":[{"title":"x","description":"d","priority":5,"role_hint":null,"depends_on":[]}]}</plan>'

    def fake_run(invocation, prompt):
        calls["n"] += 1
        return "garbage with no plan block" if calls["n"] == 1 else valid

    import kanban_cli.chat_designer as cd
    original = cd.run_subprocess
    cd.run_subprocess = fake_run
    try:
        result = designer.design([], "make a plan")
    finally:
        cd.run_subprocess = original

    assert calls["n"] == 2, f"expected 2 calls (one retry), got {calls['n']}"
    assert result.plan.summary == "ok"
    print("PASS test_designer_retries_on_first_failure")


def test_designer_raises_after_retry_fails():
    role = RoleAssignment(agent="fake", command="echo", mode="headless")
    designer = ChatDesigner.__new__(ChatDesigner)
    designer.role_assignment = role
    designer.adapter = None
    designer.invocation = DesignerInvocation(
        command="echo",
        args=[],
        use_stdin=False,
        env=os.environ.copy(),
        timeout_seconds=5,
        adapter_name="fake",
        display_name="fake",
    )

    import kanban_cli.chat_designer as cd
    original = cd.run_subprocess
    cd.run_subprocess = lambda inv, prompt: "still no plan block at all"
    try:
        designer.design([], "make a plan")
    except DesignerError as exc:
        print(f"PASS test_designer_raises_after_retry_fails: {str(exc)[:80]}")
        return
    finally:
        cd.run_subprocess = original
    raise AssertionError("expected DesignerError after retry also failed")


def test_drop_task_rewires_depends_on():
    plan = PlanV1(
        version=1,
        summary="t",
        tasks=[
            PlanTask(title="a", description="", priority=5, role_hint=None, depends_on=[]),
            PlanTask(title="b", description="", priority=5, role_hint=None, depends_on=[0]),
            PlanTask(title="c", description="", priority=5, role_hint=None, depends_on=[0, 1]),
        ],
    )
    new = drop_task(plan, 0)
    assert len(new.tasks) == 2
    # Original task[1].depends_on=[0] → [0] is dropped (target removed)
    assert new.tasks[0].depends_on == []
    # Original task[2].depends_on=[0,1] → 0 is dropped, 1 → 0
    assert new.tasks[1].depends_on == [0]
    print("PASS test_drop_task_rewires_depends_on")


def _create_human_owner(name: str) -> int:
    """Insert a HUMAN OWNER directly in the DB (no public register endpoint)."""
    import asyncio
    from database import async_session_maker
    from models import Entity, EntityType, Role

    async def _run() -> int:
        async with async_session_maker() as db:
            entity = Entity(
                name=name,
                entity_type=EntityType.HUMAN,
                role=Role.OWNER,
                is_active=True,
            )
            db.add(entity)
            await db.commit()
            await db.refresh(entity)
            return entity.id

    return asyncio.run(_run())


def _ensure_human_and_project(client, name: str, path: str) -> tuple[int, int]:
    """Create a HUMAN owner + APPROVED project, return (human_id, project_id)."""
    human_id = _create_human_owner(name)
    headers = {"x-entity-id": str(human_id)}
    r = client.post(
        "/projects",
        json={"name": name, "description": "smoke test", "path": path},
        headers=headers,
    )
    assert r.status_code in (200, 201), f"create project: {r.status_code} {r.text[:200]}"
    project_id = r.json()["id"]
    client.patch(
        f"/projects/{project_id}",
        json={"approval_status": "APPROVED"},
        headers=headers,
    )
    return human_id, project_id


def test_endpoint_designer_path():
    """POST /ui/tasks/chat-plan with items[] should land cards in backlog and return them."""
    with TestClient(app) as client:
        human_id, project_id = _ensure_human_and_project(
            client, "ChatDesigner Smoke", "/tmp/chatdesigner_smoke"
        )
        headers = {"x-entity-id": str(human_id)}

        body = {
            "project_id": project_id,
            "message": "Add OAuth login (Google + GitHub)",
            "items": [
                {
                    "title": "Design OAuth abstraction",
                    "description": "Spec the interface.",
                    "priority": 9,
                    "role_hint": "architecture",
                    "acceptance": ["covers token refresh", "interface doc lands"],
                    "depends_on": [],
                },
                {
                    "title": "Implement callback route",
                    "description": "Server-side flow.",
                    "priority": 7,
                    "role_hint": "worker",
                    "acceptance": [],
                    "depends_on": [0],
                },
            ],
            "transcript": "USER: add OAuth\nASSISTANT: <plan>...</plan>",
        }
        r = client.post("/ui/tasks/chat-plan", json=body, headers=headers)
        assert r.status_code == 200, f"chat-plan: {r.status_code} {r.text[:300]}"
        payload = r.json()
        assert payload["from_designer"] is True
        assert len(payload["tasks"]) == 2
        ids = [t["id"] for t in payload["tasks"]]
        # Verify the second task's description was annotated with the dependency
        r = client.get(f"/tasks/{ids[1]}", headers=headers)
        if r.status_code == 200:
            desc = r.json().get("description", "")
            assert f"#{ids[0]}" in desc, f"expected dependency reference in description: {desc[:200]}"
            assert "Acceptance:" not in desc  # second task has no acceptance criteria
        print(f"PASS test_endpoint_designer_path (project={project_id}, tasks={ids})")


def test_endpoint_regex_fallback_still_works():
    """Posting without items[] must still hit the regex fallback (browser parity)."""
    with TestClient(app) as client:
        human_id, project_id = _ensure_human_and_project(
            client, "ChatRegex Smoke", "/tmp/chatregex_smoke"
        )
        headers = {"x-entity-id": str(human_id)}
        r = client.post(
            "/ui/tasks/chat-plan",
            json={"project_id": project_id, "message": "Refactor the auth router"},
            headers=headers,
        )
        assert r.status_code == 200, f"regex fallback: {r.status_code} {r.text[:300]}"
        payload = r.json()
        assert payload["from_designer"] is False
        assert len(payload["tasks"]) >= 1
        print(f"PASS test_endpoint_regex_fallback_still_works (created {len(payload['tasks'])} cards)")


def test_render_plan_renders_questions_and_tasks():
    plan = PlanV1(
        version=1,
        summary="Test summary",
        questions=["Q1?", "Q2?"],
        tasks=[
            PlanTask(title="Task A", description="desc A", priority=8, role_hint="worker", depends_on=[]),
        ],
    )
    out = render_plan(plan)
    assert "Test summary" in out
    assert "Q1?" in out and "Q2?" in out
    assert "Task A" in out and "[8]" in out
    print("PASS test_render_plan_renders_questions_and_tasks")


def main():
    tests = [
        test_parse_happy_path,
        test_parse_no_block,
        test_parse_invalid_role_hint,
        test_parse_priority_out_of_range,
        test_designer_retries_on_first_failure,
        test_designer_raises_after_retry_fails,
        test_drop_task_rewires_depends_on,
        test_render_plan_renders_questions_and_tasks,
        test_endpoint_designer_path,
        test_endpoint_regex_fallback_still_works,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:
            failed += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
