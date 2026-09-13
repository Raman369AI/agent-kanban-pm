"""
Per-task tmux session streamer.

The assignment launcher spawns a tmux session for each (task, agent) pair and
runs the CLI inside it. Without a streamer, the only way to see what the CLI
is doing is to `tmux attach` — the kanban board's Terminal workbench tab stays
empty because the CLI itself does not call back through MCP / REST with
session_id-tagged activity.

This module periodically:
  1. Loads every active AgentSession (status != DONE/ERROR, ended_at NULL,
     task_id set, command set).
  2. Reconstructs the expected tmux session name (matches assignment_launcher).
  3. Captures the pane scrollback, diffs against what was last persisted, and
     writes new lines into AgentActivity rows tagged with session_id +
     project_id so the UI's Terminal feed reflects live progress.
  4. Detects known approval-style prompts (same regex set as the role
     supervisor) and files an AgentApproval, marking the session BLOCKED.
  5. Marks the session DONE when the tmux session has gone away.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Dict, Optional

from sqlalchemy import select, desc, update as sa_update, func

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.models import (
    AgentActivity,
    AgentApproval,
    AgentApprovalStatus,
    AgentCheckpoint,
    AgentHeartbeat,
    AgentSession,
    AgentSessionStatus,
    AgentStatusType,
    ActivitySummary,
    ActivityType,
    ApprovalType,
    Stage,
    Task,
    TaskLog,
    TaskStatus,
    task_assignments,
)
from agent_kanban_pm.runtime.assignment_launcher import _tmux_session_name
from agent_kanban_pm.runtime.handoff_protocol import read_status_file, status_matches_session
from agent_kanban_pm.runtime.prompt_patterns import (
    approval_label, approval_message, detect_prompt, prompt_identity,
)
from agent_kanban_pm.runtime.process_launcher import (
    runner_available,
    has_session,
    kill_session,
    capture_pane,
    send_prompt_reply,
)

logger = logging.getLogger(__name__)

# In-process cursor: per-session, the trailing-N-char signature of the pane the
# last time we polled. Anything new (i.e. text appearing after that signature)
# is what we ship to AgentActivity.
_pane_cursor: Dict[int, str] = {}
# Per-session pending approval id, so we don't re-file the same prompt.
_pending_approvals: Dict[int, int] = {}
_last_prompt: Dict[int, str] = {}
_delivered_approvals: Dict[int, int] = {}
_checkpoint_cursor: Dict[int, str] = {}


def reset_streamer():
    """Clear module-level state for test isolation."""
    _pane_cursor.clear()
    _pending_approvals.clear()
    _last_prompt.clear()
    _delivered_approvals.clear()
    _checkpoint_cursor.clear()


def _tmux_available() -> bool:
    return runner_available()


def _tmux_has_session(session_name: str) -> bool:
    return has_session(session_name)


def _new_text_since_cursor(pane: str, cursor: Optional[str]) -> str:
    """Return text that appeared in `pane` after the last seen `cursor`."""
    if not pane:
        return ""
    if not cursor:
        return pane
    idx = pane.rfind(cursor)
    if idx < 0:
        # Cursor not found (probably scrolled out of capture window) — return
        # the whole pane so we don't miss anything.
        return pane
    return pane[idx + len(cursor):]


def _checkpoint_summary(pane: str) -> str:
    lines = [line.strip() for line in pane.splitlines() if line.strip()]
    useful = [
        line for line in lines
        if not line.startswith(("▀", "▄", "─"))
        and "Type your message" not in line
        and "? for shortcuts" not in line
    ]
    tail = useful[-12:]
    return "\n".join(tail)[-2000:] or "Session is active; no terminal output captured yet."


async def _upsert_checkpoint(session: AgentSession, pane: str, status_type: AgentStatusType) -> None:
    if not session.task_id:
        return
    terminal_tail = pane[-5000:] if pane else ""
    signature = f"{status_type.value}:{terminal_tail[-500:]}"
    if _checkpoint_cursor.get(session.id) == signature:
        return
    _checkpoint_cursor[session.id] = signature
    now = datetime.now(UTC)
    async with async_session_maker() as db:
        result = await db.execute(
            select(AgentCheckpoint)
            .filter(
                AgentCheckpoint.agent_id == session.agent_id,
                AgentCheckpoint.task_id == session.task_id,
                AgentCheckpoint.session_id == session.id,
            )
            .order_by(desc(AgentCheckpoint.updated_at))
            .limit(1)
        )
        checkpoint = result.scalar_one_or_none()
        payload = json.dumps({
            "status": status_type.value,
            "last_seen_at": now.isoformat(),
        })
        if checkpoint:
            checkpoint.workspace_path = session.workspace_path
            checkpoint.summary = _checkpoint_summary(pane)
            checkpoint.terminal_tail = terminal_tail
            checkpoint.payload_json = payload
            checkpoint.updated_at = now
        else:
            db.add(AgentCheckpoint(
                agent_id=session.agent_id,
                project_id=session.project_id,
                task_id=session.task_id,
                session_id=session.id,
                workspace_path=session.workspace_path,
                summary=_checkpoint_summary(pane),
                terminal_tail=terminal_tail,
                payload_json=payload,
                created_at=now,
                updated_at=now,
            ))
        await db.commit()


async def _completion_for_session(session: AgentSession) -> Optional[str]:
    """Import a verified file handoff into durable session state before acting on it."""
    async with async_session_maker() as db:
        row = await db.get(AgentSession, session.id)
        if not row or row.ended_at is not None:
            return None
        if row.handoff_state in {"done", "completed", "review"}:
            return row.handoff_summary or "Agent submitted a handoff"

    try:
        status_data = read_status_file(session.workspace_path)
    except (OSError, ValueError) as exc:
        logger.warning("Could not read handoff for session #%s: %s", session.id, exc)
        return None
    if not status_matches_session(status_data, session):
        return None
    summary = (status_data["validated"].summary or "Agent submitted a handoff")[-4000:]
    state = status_data["state"]

    async with async_session_maker() as db:
        result = await db.execute(
            sa_update(AgentSession)
            .where(
                AgentSession.id == session.id,
                AgentSession.ended_at.is_(None),
                AgentSession.run_token == session.run_token,
                AgentSession.handoff_received_at.is_(None),
            )
            .values(
                handoff_state=state,
                handoff_summary=summary,
                handoff_received_at=datetime.now(UTC),
            )
        )
        if result.rowcount:
            await db.commit()
            return summary
        row = await db.get(AgentSession, session.id)
        return row.handoff_summary if row and row.ended_at is None else None


async def _stage_for_key(db, project_id: int, keys: set[str]) -> Optional[Stage]:

    result = await db.execute(
        select(Stage)
        .filter(Stage.project_id == project_id)
        .order_by(Stage.order)
    )
    for stage in result.scalars().all():
        if stage.key in keys:
            return stage
    return None


async def _assigned_role_for_session(session: AgentSession) -> str:
    return (session.assigned_role or "worker").strip().lower()


async def _assign_stage_entry_roles(db, task: Task, stage: Stage, *, skip_agent_id: Optional[int] = None) -> list[dict]:
    from agent_kanban_pm.runtime.preferences import load_preferences
    from agent_kanban_pm.runtime.stage_policy import get_stage_policy_for_stage, policy_roles
    from agent_kanban_pm.models import Entity, EntityType

    policy = await get_stage_policy_for_stage(db, task.project_id, stage.id)
    roles = policy_roles(policy) if policy else []
    if not roles:
        return []

    prefs = load_preferences()
    assignments = prefs.get_role_assignments() if prefs else {}
    assigned: list[dict] = []
    for role_name in roles:
        assignment = assignments.get(role_name)
        if not assignment or not assignment.agent:
            continue
        entity = (await db.execute(
            select(Entity).filter(
                Entity.name == assignment.agent,
                Entity.entity_type == EntityType.AGENT,
                Entity.is_active == True,
            )
        )).scalar_one_or_none()
        if not entity or entity.id == skip_agent_id:
            continue

        exists = await db.execute(
            select(task_assignments.c.task_id).where(
                task_assignments.c.task_id == task.id,
                task_assignments.c.entity_id == entity.id,
            )
        )
        if exists.first():
            assigned.append({"role": role_name, "entity_id": entity.id, "agent": entity.name, "already_assigned": True})
            continue
        await db.execute(task_assignments.insert().values(task_id=task.id, entity_id=entity.id))
        assigned.append({"role": role_name, "entity_id": entity.id, "agent": entity.name, "already_assigned": False})
    return assigned


async def _advance_task_after_session(db, session: AgentSession, task: Task) -> tuple[Optional[dict], list[dict]]:

    current_stage = None
    if task.stage_id:
        current_stage = (await db.execute(select(Stage).filter(Stage.id == task.stage_id))).scalar_one_or_none()
    current_key = current_stage.key if current_stage else ""
    assigned_role = await _assigned_role_for_session(session)

    target_stage = None
    target_status = None
    if current_key in {"to_do", "in_progress"}:
        target_stage = await _stage_for_key(db, task.project_id, {"review"})
        target_status = TaskStatus.IN_REVIEW
    elif current_key == "review" and assigned_role in {"test", "diff_review"}:
        target_stage = await _stage_for_key(db, task.project_id, {"done"})
        target_status = TaskStatus.COMPLETED

    if not target_stage or target_stage.id == task.stage_id:
        return None, []

    old_stage_id = task.stage_id
    old_stage_name = current_stage.name if current_stage else None
    task.stage_id = target_stage.id
    task.stage = target_stage
    task.status = target_status
    task.version += 1
    task.updated_at = datetime.now(UTC)
    if target_status == TaskStatus.COMPLETED and task.completed_at is None:
        task.completed_at = datetime.now(UTC)

    assigned = await _assign_stage_entry_roles(db, task, target_stage, skip_agent_id=session.agent_id)
    return {
        "task_id": task.id,
        "title": task.title,
        "project_id": task.project_id,
        "from_stage_id": old_stage_id,
        "to_stage_id": target_stage.id,
        "from_stage_name": old_stage_name,
        "to_stage_name": target_stage.name,
        "status": target_status.value,
        "assigned_role": assigned_role,
    }, assigned


async def _finalize_completed_session(session: AgentSession, pane: str, summary: str) -> bool:
    """Mark the session DONE, record handoff, and advance the card to the next collaboration stage."""
    now = datetime.now(UTC)
    async with async_session_maker() as db:
        claimed = await db.execute(
            sa_update(AgentSession)
            .where(
                AgentSession.id == session.id,
                AgentSession.ended_at.is_(None),
                AgentSession.status.in_([
                    AgentSessionStatus.STARTING,
                    AgentSessionStatus.ACTIVE,
                    AgentSessionStatus.BLOCKED,
                ]),
            )
            .values(
                status=AgentSessionStatus.DONE,
                ended_at=now,
                last_seen_at=now,
                handoff_summary=summary,
                handoff_state=func.coalesce(AgentSession.handoff_state, "done"),
                handoff_received_at=func.coalesce(AgentSession.handoff_received_at, now),
            )
        )
        if claimed.rowcount != 1:
            return False
        row = await db.get(AgentSession, session.id)
        task = await db.get(Task, row.task_id) if row and row.task_id else None
        if not task or task.project_id != row.project_id:
            await db.rollback()
            return False

        transition_event, assigned_stage_roles = await _advance_task_after_session(db, row, task)

        pending = (await db.execute(
            select(AgentApproval).filter(
                AgentApproval.session_id == session.id,
                AgentApproval.status == AgentApprovalStatus.PENDING,
            )
        )).scalars().all()
        for approval in pending:
            # Use atomic CAS to avoid overwriting a concurrent resolution
            cas_result = await db.execute(
                sa_update(AgentApproval)
                .where(
                    AgentApproval.id == approval.id,
                    AgentApproval.update_version == approval.update_version,
                    AgentApproval.status == AgentApprovalStatus.PENDING,
                )
                .values(
                    status=AgentApprovalStatus.CANCELLED,
                    resolved_at=now,
                    response_message="Session completed before this approval was resolved.",
                    update_version=approval.update_version + 1,
                )
            )
            if cas_result.rowcount == 0:
                logger.debug(
                    "Approval #%d was already resolved by another path; skipping cancellation",
                    approval.id,
                )

        db.add(TaskLog(
            task_id=task.id,
            message=f"Agent session #{session.id} completed. Session may be ready for review. Summary:\n{summary}",
            log_type="handoff",
            created_at=now,
        ))
        db.add(ActivitySummary(
            project_id=task.project_id,
            task_id=task.id,
            agent_id=session.agent_id,
            summary=summary,
            created_at=now,
        ))
        db.add(AgentActivity(
            agent_id=session.agent_id,
            session_id=session.id,
            project_id=session.project_id,
            task_id=session.task_id,
            activity_type=ActivityType.HANDOFF,
            source="session_streamer",
            message=(
                f"Session #{session.id} appears complete. "
                + (
                    f"Moved task to {transition_event['to_stage_name']}. "
                    if transition_event else
                    "No automatic card movement was available. "
                )
                + f"Summary: {summary[:500]}"
            ),
            workspace_path=session.workspace_path,
            created_at=now,
        ))
        await db.commit()

    if transition_event:
        await event_bus.publish(
            EventType.TASK_MOVED.value,
            {
                "task_id": transition_event["task_id"],
                "title": transition_event["title"],
                "from_stage_id": transition_event["from_stage_id"],
                "to_stage_id": transition_event["to_stage_id"],
                "status": transition_event["status"],
                "summary": "Agent session completed and handed off",
            },
            project_id=transition_event["project_id"],
            entity_id=session.agent_id,
        )
        await event_bus.publish(
            EventType.TASK_UPDATED.value,
            {
                "task_id": transition_event["task_id"],
                "title": transition_event["title"],
                "status": transition_event["status"],
                "stage_id": transition_event["to_stage_id"],
            },
            project_id=transition_event["project_id"],
            entity_id=session.agent_id,
        )
        for assignment in assigned_stage_roles:
            await event_bus.publish(
                EventType.TASK_ASSIGNED.value,
                {
                    "task_id": transition_event["task_id"],
                    "entity_id": assignment["entity_id"],
                    "role": assignment["role"],
                    "trigger": "stage_entry",
                },
                project_id=transition_event["project_id"],
                entity_id=session.agent_id,
            )

    await event_bus.publish(
        EventType.AGENT_STATUS_UPDATED.value,
        {
            "agent_id": session.agent_id,
            "session_id": session.id,
            "project_id": session.project_id,
            "task_id": session.task_id,
            "status_type": "done",
            "message": (
                f"Session completed; task #{session.task_id} advanced to {transition_event['to_stage_name']}"
                if transition_event else
                f"Session completed; task #{session.task_id} awaiting review decision"
            ),
        },
        project_id=session.project_id,
        entity_id=session.agent_id,
    )
    return True


async def _resolve_session_id_for_approval(approval_id: int) -> Optional[str]:
    async with async_session_maker() as db:
        result = await db.execute(
            select(AgentApproval).filter(AgentApproval.id == approval_id)
        )
        approval = result.scalar_one_or_none()
        if not approval or approval.status == AgentApprovalStatus.PENDING:
            return None
        return approval.status.value, approval.response_message or ""


async def _get_latest_session_approval(session_id: int) -> Optional[AgentApproval]:
    async with async_session_maker() as db:
        result = await db.execute(
            select(AgentApproval)
            .filter(AgentApproval.session_id == session_id)
            .order_by(desc(AgentApproval.requested_at))
            .limit(1)
        )
        return result.scalar_one_or_none()


async def gc_leak_worktrees() -> None:
    """Prune only worktrees never referenced by a session, and only if clean."""
    try:
        from agent_kanban_pm.runtime.assignment_launcher import prune_git_worktree
        from agent_kanban_pm.models import Project
        from pathlib import Path
        import shutil
        import subprocess

        git = shutil.which("git")
        if not git:
            return

        async with async_session_maker() as db:
            projects_result = await db.execute(select(Project))
            projects = projects_result.scalars().all()

            sessions_result = await db.execute(
                select(AgentSession.workspace_path).filter(
                    AgentSession.workspace_path.is_not(None)
                )
            )
            # A finished session's files still need review and Git integration.
            # Retain every recorded task workspace until explicit cleanup.
            recorded_worktree_paths = {
                Path(path).resolve() for path in sessions_result.scalars().all()
            }

        for project in projects:
            if not project.path or not Path(project.path).exists():
                continue

            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        [git, "-C", project.path, "worktree", "list", "--porcelain"],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                )
                if result.returncode != 0:
                    continue

                for line in result.stdout.splitlines():
                    if line.startswith("worktree "):
                        wt_path_str = line.split("worktree ", 1)[1].strip()
                        wt_path = Path(wt_path_str).resolve()
                        project_path_resolved = Path(project.path).resolve()

                        if wt_path != project_path_resolved and wt_path not in recorded_worktree_paths:
                            logger.info("GC pruning orphaned worktree: %s", wt_path_str)
                            await loop.run_in_executor(None, prune_git_worktree, project.path, wt_path_str)
            except Exception as e:
                logger.warning("Error GC scanning worktrees for project %s: %s", project.path, e)
    except Exception as exc:
        logger.warning("Error running gc_leak_worktrees: %s", exc)


async def _stream_one_session(session: AgentSession, agent_name: str) -> None:
    tmux_session = _tmux_session_name(agent_name, session.task_id)

    # Agents may submit the handoff and exit between polls. Persist and
    # finalize that handoff before interpreting the missing runner as failure.
    early_summary = await _completion_for_session(session)
    if early_summary and await _finalize_completed_session(session, "", early_summary):
        if _tmux_has_session(tmux_session):
            kill_session(tmux_session)
        _pane_cursor.pop(session.id, None)
        _pending_approvals.pop(session.id, None)
        return

    if not _tmux_has_session(tmux_session):
        # tmux session is gone → wrap up the AgentSession so the UI stops
        # showing it as Active forever.
        async with async_session_maker() as db:
            row = (await db.execute(
                select(AgentSession).filter(AgentSession.id == session.id)
            )).scalar_one_or_none()
            if row and row.ended_at is None:
                row.status = AgentSessionStatus.DONE
                row.ended_at = datetime.now(UTC)
                row.last_seen_at = datetime.now(UTC)
                await db.commit()
                await event_bus.publish(
                    EventType.AGENT_STATUS_UPDATED.value,
                    {
                        "agent_id": row.agent_id,
                        "session_id": row.id,
                        "project_id": row.project_id,
                        "task_id": row.task_id,
                        "status_type": "done",
                        "message": "tmux session ended",
                    },
                    project_id=row.project_id,
                    entity_id=row.agent_id,
                )
        _pane_cursor.pop(session.id, None)
        _pending_approvals.pop(session.id, None)
        return

    async with async_session_maker() as db:
        row = (await db.execute(
            select(AgentSession).filter(AgentSession.id == session.id)
        )).scalar_one_or_none()
        now = datetime.now(UTC)
        status_type = AgentStatusType.WAITING if row and row.status == AgentSessionStatus.BLOCKED else AgentStatusType.WORKING
        if row:
            row.last_seen_at = now
        heartbeat = (await db.execute(
            select(AgentHeartbeat).filter(AgentHeartbeat.agent_id == session.agent_id)
        )).scalar_one_or_none()
        message = f"Active task session #{session.id} for task #{session.task_id}"
        if heartbeat:
            heartbeat.task_id = session.task_id
            heartbeat.status_type = status_type
            heartbeat.message = message
            heartbeat.updated_at = now
        else:
            db.add(AgentHeartbeat(
                agent_id=session.agent_id,
                task_id=session.task_id,
                status_type=status_type,
                message=message,
                updated_at=now,
            ))
        await db.commit()
    await event_bus.publish(
        EventType.AGENT_STATUS_UPDATED.value,
        {
            "agent_id": session.agent_id,
            "session_id": session.id,
            "project_id": session.project_id,
            "task_id": session.task_id,
            "status_type": status_type.value,
            "message": message,
            "workspace_path": session.workspace_path,
        },
        project_id=session.project_id,
        entity_id=session.agent_id,
    )

    pane = capture_pane(tmux_session, lines=200)
    await _upsert_checkpoint(session, pane, status_type)
    summary = await _completion_for_session(session)
    if summary and await _finalize_completed_session(session, pane, summary):
        kill_session(tmux_session)
        _pane_cursor.pop(session.id, None)
        _pending_approvals.pop(session.id, None)
        return
    cursor = _pane_cursor.get(session.id)
    new_text = _new_text_since_cursor(pane, cursor)

    if new_text.strip():
        truncated = new_text[-4000:]  # keep activity rows bounded
        async with async_session_maker() as db:
            db.add(AgentActivity(
                agent_id=session.agent_id,
                session_id=session.id,
                project_id=session.project_id,
                task_id=session.task_id,
                activity_type=ActivityType.OBSERVATION,
                source="tmux_pane",
                message=truncated,
                workspace_path=session.workspace_path,
            ))
            row = (await db.execute(
                select(AgentSession).filter(AgentSession.id == session.id)
            )).scalar_one_or_none()
            if row:
                row.last_seen_at = datetime.now(UTC)
            await db.commit()
        await event_bus.publish(
            EventType.AGENT_ACTIVITY_LOGGED.value,
            {
                "agent_id": session.agent_id,
                "session_id": session.id,
                "project_id": session.project_id,
                "task_id": session.task_id,
                "activity_type": ActivityType.OBSERVATION.value,
                "source": "tmux_pane",
                "message": truncated[-400:],
            },
            project_id=session.project_id,
            entity_id=session.agent_id,
        )

    if pane:
        _pane_cursor[session.id] = pane[-400:]

    # --- Approval queue capture (per-task tmux) -----------------------------
    detection = detect_prompt(pane)
    if not detection:
        _last_prompt.pop(session.id, None)
        return
    prompt_line, approval_type, yes_reply, no_reply = detection
    identity = prompt_identity(prompt_line)
    continuous_prompt = _last_prompt.get(session.id) == identity
    _last_prompt[session.id] = identity

    latest_approval = await _get_latest_session_approval(session.id)
    pending_id = _pending_approvals.get(session.id)
    same_prompt = bool(
        latest_approval and prompt_identity(latest_approval.command or "") == identity
    )
    if same_prompt and latest_approval.status == AgentApprovalStatus.PENDING:
        _pending_approvals[session.id] = latest_approval.id
        return
    if same_prompt and latest_approval.status != AgentApprovalStatus.PENDING:
        if _delivered_approvals.get(session.id) == latest_approval.id and continuous_prompt:
            return  # Do not replay the same resolved decision every poll.
        if pending_id == latest_approval.id:
            resolved = await _resolve_session_id_for_approval(pending_id)
            if resolved is not None:
                decision, response_message = resolved
                # An approval note is for the audit trail, not a keystroke in a
                # menu. Menu replies are fixed so the requested choice is honored.
                menu_reply = yes_reply.startswith("keys:") or no_reply.startswith("keys:")
                if decision == "approved":
                    reply = yes_reply if menu_reply else response_message.strip() or yes_reply
                elif decision == "rejected":
                    reply = no_reply if menu_reply else response_message.strip() or no_reply
                else:
                    reply = no_reply
                send_prompt_reply(tmux_session, reply)
                _delivered_approvals[session.id] = pending_id
                _pending_approvals.pop(session.id, None)
            return
        # A previous approval was already consumed, or the process restarted
        # after resolution. Require a fresh decision instead of reusing it.

    async with async_session_maker() as db:
        approval = AgentApproval(
            project_id=session.project_id,
            task_id=session.task_id,
            session_id=session.id,
            agent_id=session.agent_id,
            approval_type=ApprovalType(approval_type),
            title=f"task #{session.task_id}: {approval_label(prompt_line, approval_type)}",
            message=approval_message(prompt_line, approval_type),
            command=prompt_line,
            status=AgentApprovalStatus.PENDING,
        )
        db.add(approval)
        row = (await db.execute(
            select(AgentSession).filter(AgentSession.id == session.id)
        )).scalar_one_or_none()
        if row and row.status != AgentSessionStatus.BLOCKED:
            row.status = AgentSessionStatus.BLOCKED
            row.last_seen_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(approval)
        approval_id = approval.id
        await event_bus.publish(
            EventType.AGENT_APPROVAL_REQUESTED.value,
            {
                "approval_id": approval.id,
                "project_id": session.project_id,
                "task_id": session.task_id,
                "session_id": session.id,
                "agent_id": session.agent_id,
                "approval_type": approval_type,
                "title": approval.title,
                "message": approval.message,
                "command": approval.command,
            },
            project_id=session.project_id,
            entity_id=session.agent_id,
        )
    _pending_approvals[session.id] = approval_id
    logger.info(
        "Filed approval #%s for task session #%s (type=%s): %r",
        approval_id, session.id, approval_type, prompt_line,
    )


async def session_streamer_loop(poll_seconds: int = 5):
    """Background task: stream pane output of active per-task tmux sessions."""
    if not _tmux_available():
        logger.info("no process runner available — session streamer disabled")
        return

    import time
    last_gc_time = 0.0

    while True:
        try:
            await asyncio.sleep(poll_seconds)

            now_time = time.time()
            if now_time - last_gc_time > 60.0:
                last_gc_time = now_time
                await gc_leak_worktrees()

            async with async_session_maker() as db:
                result = await db.execute(
                    select(AgentSession)
                    .filter(
                        AgentSession.task_id.is_not(None),
                        AgentSession.command.is_not(None),
                        AgentSession.ended_at.is_(None),
                    )
                )
                sessions = list(result.scalars().all())
                # Resolve agent names in a single pass.
                from agent_kanban_pm.models import Entity
                agent_ids = {s.agent_id for s in sessions}
                names: Dict[int, str] = {}
                if agent_ids:
                    e_result = await db.execute(
                        select(Entity).filter(Entity.id.in_(agent_ids))
                    )
                    for e in e_result.scalars().all():
                        names[e.id] = e.name

            for s in sessions:
                agent_name = names.get(s.agent_id)
                if not agent_name:
                    continue
                try:
                    await _stream_one_session(s, agent_name)
                except Exception as exc:
                    logger.warning("Session streamer error for #%s: %s", s.id, exc)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Session streamer loop error: %s", exc)
