"""Audit-trail tests.

The launcher records every agent start as an append-only AgentActivity row
carrying the resolved command line, the workspace, the branch, and the autonomy
the session ran under. That record is the only durable answer to "what did this
agent actually run", so both ways of reading it — the HTTP feed and the
`kanban audit` command — need to keep working.

`kanban audit` reads the database directly rather than through the API, because
the moment someone needs the trail is often the moment the server is not up.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import tests_helper  # noqa: F401  — autouse cleanup listeners

from agent_kanban_pm.app import app
from agent_kanban_pm.models import ActivityType


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded(client):
    """A project, a task, and two activity rows: one auto launch, one command."""
    owner = client.get("/entities/me").json()
    headers = {"X-Entity-ID": str(owner["id"])}

    project = client.post(
        "/projects", json={"name": "Audit Test", "description": "d"}, headers=headers
    ).json()
    task = client.post(
        "/tasks",
        json={"title": "Audit task", "description": "d", "project_id": project["id"]},
        headers=headers,
    ).json()

    # Adapter agents are registered as entities at startup; reuse one rather
    # than inventing a second creation path.
    agents = client.get("/entities?entity_type=agent", headers=headers).json()
    assert agents, "no agent entities registered"
    agent = agents[0]

    launch = client.post(
        f"/agents/{agent['id']}/activity",
        json={
            "project_id": project["id"],
            "task_id": task["id"],
            "activity_type": "action",
            "source": "assignment_launcher",
            "message": "Auto-starting audit-agent for assigned task",
            "workspace_path": "/srv/demo/.worktrees/task-1",
            "command": "/usr/bin/claude --print --dangerously-skip-permissions 'go'",
            "payload_json": json.dumps({"autonomy": "auto", "branch": "kanban/task-1"}),
        },
        headers=headers,
    )
    assert launch.status_code in (200, 201), launch.text

    note = client.post(
        f"/agents/{agent['id']}/activity",
        json={
            "project_id": project["id"],
            "task_id": task["id"],
            "activity_type": "thought",
            "source": "agent",
            "message": "Considering the approach",
            "payload_json": json.dumps({"autonomy": "supervised"}),
        },
        headers=headers,
    )
    assert note.status_code in (200, 201), note.text

    return {
        "headers": headers,
        "project_id": project["id"],
        "task_id": task["id"],
        "agent_id": agent["id"],
    }


# ---------------------------------------------------------------------------
# The recorded row
# ---------------------------------------------------------------------------


def test_launch_record_keeps_the_command_line(client, seeded):
    feed = client.get(
        f"/agents/activity?task_id={seeded['task_id']}", headers=seeded["headers"]
    ).json()
    launches = [row for row in feed if row["source"] == "assignment_launcher"]
    assert launches, f"no launch row in {feed}"
    assert "--dangerously-skip-permissions" in launches[0]["command"]
    assert launches[0]["workspace_path"]


def test_launch_record_keeps_the_autonomy_it_ran_under(client, seeded):
    feed = client.get(
        f"/agents/activity?task_id={seeded['task_id']}", headers=seeded["headers"]
    ).json()
    launches = [row for row in feed if row["source"] == "assignment_launcher"]
    payload = json.loads(launches[0]["payload_json"])
    assert payload["autonomy"] == "auto"
    assert payload["branch"] == "kanban/task-1"


# ---------------------------------------------------------------------------
# Feed filters
# ---------------------------------------------------------------------------


def test_feed_filters_by_task(client, seeded):
    feed = client.get(
        f"/agents/activity?task_id={seeded['task_id']}", headers=seeded["headers"]
    ).json()
    assert len(feed) >= 2
    assert all(row["task_id"] == seeded["task_id"] for row in feed)


def test_feed_filters_by_activity_type(client, seeded):
    feed = client.get(
        f"/agents/activity?task_id={seeded['task_id']}&activity_type=action",
        headers=seeded["headers"],
    ).json()
    assert feed, "action filter returned nothing"
    assert all(row["activity_type"] == ActivityType.ACTION.value for row in feed)


def test_feed_can_return_only_rows_that_recorded_a_command(client, seeded):
    """The audit question is 'what ran', not 'what was narrated'."""
    feed = client.get(
        f"/agents/activity?task_id={seeded['task_id']}&has_command=true",
        headers=seeded["headers"],
    ).json()
    assert feed, "has_command filter returned nothing"
    assert all(row["command"] for row in feed)
    # The thought row has no command and must be excluded.
    assert all(row["activity_type"] != ActivityType.THOUGHT.value for row in feed)


def test_feed_rejects_an_unknown_activity_type(client, seeded):
    response = client.get(
        "/agents/activity?activity_type=not-a-real-type", headers=seeded["headers"]
    )
    assert response.status_code == 422


def test_activity_feed_requires_authentication():
    """The trail names workspaces and command lines; it is not public."""
    import os

    from agent_kanban_pm.app import app as fresh_app

    testing = os.environ.pop("KANBAN_TESTING", None)
    try:
        with TestClient(fresh_app) as unauth:
            assert unauth.get("/agents/activity").status_code == 401
    finally:
        if testing is not None:
            os.environ["KANBAN_TESTING"] = testing


# ---------------------------------------------------------------------------
# `kanban audit`
# ---------------------------------------------------------------------------


def test_audit_command_is_registered_with_its_filters():
    """The CLI is the path a human uses when the server is down."""
    from agent_kanban_pm.cli import cmd_audit, main  # noqa: F401
    import agent_kanban_pm.cli as cli_module
    import argparse
    import inspect

    source = inspect.getsource(cli_module.main)
    assert '"audit"' in source, "audit subcommand not registered"
    for flag in ("--auto", "--commands", "--project", "--task", "--agent",
                 "--since", "--limit", "--json"):
        assert flag in source, f"audit is missing {flag}"
    assert isinstance(argparse.ArgumentParser(), argparse.ArgumentParser)


def test_audit_reads_the_database_not_the_http_api():
    """Auditing must not depend on a running server."""
    import inspect

    from agent_kanban_pm.cli import cmd_audit

    source = inspect.getsource(cmd_audit)
    assert "async_session_maker" in source
    for networked in ("httpx", "requests", "urlopen", "api_base"):
        assert networked not in source, (
            f"cmd_audit reaches for {networked}; the trail must be readable "
            f"with the server down"
        )
