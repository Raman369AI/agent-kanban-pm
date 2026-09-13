"""Interactive CLI menus must become actionable Kanban approvals."""

import pytest
from sqlalchemy import select

from agent_kanban_pm.db import async_session_maker, init_db
from agent_kanban_pm.models import (
    AgentApproval,
    AgentApprovalStatus,
    AgentSession,
    AgentSessionStatus,
    ApprovalStatus,
    ApprovalType,
    Entity,
    EntityType,
    Project,
    Role,
    Task,
    TaskStatus,
)
from agent_kanban_pm.runtime import process_launcher, role_supervisor, session_streamer
from agent_kanban_pm.runtime.prompt_patterns import (
    approval_message, detect_prompt, prompt_identity,
)


CHROME_PANE = """● Skill(claude-in-chrome)
  ⎿  Initializing…
────────────────────────────────────────────────────────────────────────────────
 Claude wants to use your browser

  This task could use your Chrome browser. The Claude in Chrome extension lets
  Claude navigate sites, click buttons, and fill forms in your existing
  session.

    Install extension  Opens the install page in Chrome
  ❯ Not now            Continue without browser tools
    Don't ask again    Revisit anytime with /chrome
"""


def test_browser_menu_is_detected_by_both_supervisors():
    expected = ("external_access", "keys:Up,Enter", "keys:Enter")
    for detector in (detect_prompt, role_supervisor.detect_prompt):
        detection = detector(CHROME_PANE)
        assert detection is not None
        assert detection[1:] == expected
        assert "Claude wants to use your browser" in detection[0]
    selected_install = CHROME_PANE.replace(
        "    Install extension", "  ❯ Install extension"
    ).replace("  ❯ Not now", "    Not now")
    assert detect_prompt(selected_install)[1:] == (
        "external_access", "keys:Enter", "keys:Down,Enter"
    )
    assert prompt_identity(detect_prompt(selected_install)[0]) == prompt_identity(detection[0])
    assert detect_prompt("● Running tests\n❯\n  auto mode on") is None
    unfamiliar = "Choose how to proceed\n    Try again\n  ❯ Wait here\n    Cancel\n"
    assert detect_prompt(unfamiliar)[1:] == ("other", "keys:Enter", "keys:Escape")
    assert "Approve selects the highlighted choice" in approval_message(unfamiliar, "other")


def test_menu_reply_uses_keys_not_literal_text(monkeypatch):
    sent = []
    monkeypatch.setattr(process_launcher, "tmux_available", lambda: True)
    monkeypatch.setattr(
        process_launcher.subprocess,
        "run",
        lambda args, **kwargs: sent.append(args),
    )
    process_launcher.send_prompt_reply("worker-pane", "keys:Up,Enter")
    process_launcher.send_prompt_reply("worker-pane", "keys:Enter")
    assert sent == [
        ["tmux", "send-keys", "-t", "worker-pane", "Up", "Enter"],
        ["tmux", "send-keys", "-t", "worker-pane", "Enter"],
    ]
    with pytest.raises(ValueError):
        process_launcher.send_prompt_reply("worker-pane", "keys:run-shell-command")


@pytest.mark.parametrize(
    ("decision", "expected_reply"),
    [
        (AgentApprovalStatus.APPROVED, "keys:Up,Enter"),
        (AgentApprovalStatus.REJECTED, "keys:Enter"),
    ],
)
@pytest.mark.asyncio
async def test_browser_menu_is_queued_once_and_decision_resumes_cli(
    tmp_path, monkeypatch, decision, expected_reply
):
    await init_db()
    session_streamer.reset_streamer()
    sent = []
    pane_text = [CHROME_PANE]

    async def no_publish(*args, **kwargs):
        return None

    async def no_completion(*args, **kwargs):
        return None

    monkeypatch.setattr(session_streamer.event_bus, "publish", no_publish)
    monkeypatch.setattr(session_streamer, "_tmux_has_session", lambda _: True)
    monkeypatch.setattr(session_streamer, "capture_pane", lambda *args, **kwargs: pane_text[0])
    monkeypatch.setattr(session_streamer, "_completion_for_session", no_completion)
    monkeypatch.setattr(
        session_streamer, "send_prompt_reply",
        lambda pane, reply: sent.append((pane, reply)),
    )

    async with async_session_maker() as db:
        owner = Entity(name="browser-prompt-owner", entity_type=EntityType.HUMAN, role=Role.OWNER)
        agent = Entity(name="browser-prompt-agent", entity_type=EntityType.AGENT, role=Role.WORKER)
        db.add_all([owner, agent])
        await db.flush()
        project = Project(
            name="browser prompt project", creator_id=owner.id,
            approval_status=ApprovalStatus.APPROVED, path=str(tmp_path),
        )
        db.add(project)
        await db.flush()
        task = Task(
            title="Review in browser", project_id=project.id,
            status=TaskStatus.IN_PROGRESS, created_by=owner.id,
        )
        db.add(task)
        await db.flush()
        session = AgentSession(
            agent_id=agent.id, project_id=project.id, task_id=task.id,
            workspace_path=str(tmp_path), status=AgentSessionStatus.ACTIVE,
            command="claude",
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    async with async_session_maker() as db:
        session = await db.get(AgentSession, session_id)
    await session_streamer._stream_one_session(session, "browser-prompt-agent")
    await session_streamer._stream_one_session(session, "browser-prompt-agent")

    async with async_session_maker() as db:
        rows = (await db.execute(select(AgentApproval).where(
            AgentApproval.session_id == session_id
        ))).scalars().all()
        assert len(rows) == 1
        approval = rows[0]
        assert approval.approval_type == ApprovalType.EXTERNAL_ACCESS
        assert approval.status == AgentApprovalStatus.PENDING
        assert "Approve opens the browser extension" in approval.message
        assert (await db.get(AgentSession, session_id)).status == AgentSessionStatus.BLOCKED
        approval.status = decision
        approval.response_message = "Human note must not be typed into the menu"
        await db.commit()

    await session_streamer._stream_one_session(session, "browser-prompt-agent")
    assert sent == [
        (session_streamer._tmux_session_name("browser-prompt-agent", task.id), expected_reply)
    ]
    await session_streamer._stream_one_session(session, "browser-prompt-agent")
    assert len(sent) == 1  # A resolved decision is not replayed every poll.

    pane_text[0] = "● Running tests\n❯\n  auto mode on"
    await session_streamer._stream_one_session(session, "browser-prompt-agent")
    pane_text[0] = CHROME_PANE
    await session_streamer._stream_one_session(session, "browser-prompt-agent")
    async with async_session_maker() as db:
        approvals = (await db.execute(select(AgentApproval).where(
            AgentApproval.session_id == session_id
        ))).scalars().all()
        assert len(approvals) == 2
        assert approvals[-1].status == AgentApprovalStatus.PENDING
        approvals[-1].status = AgentApprovalStatus.APPROVED
        await db.commit()

    # After a server restart, a resolved decision must not authorize the menu
    # again if it is still on screen.
    session_streamer.reset_streamer()
    await session_streamer._stream_one_session(session, "browser-prompt-agent")
    async with async_session_maker() as db:
        approvals = (await db.execute(select(AgentApproval).where(
            AgentApproval.session_id == session_id
        ))).scalars().all()
        assert len(approvals) == 3
        assert approvals[-1].status == AgentApprovalStatus.PENDING
    assert len(sent) == 1
    session_streamer.reset_streamer()
