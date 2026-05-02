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

from sqlalchemy import select, desc

from database import async_session_maker
from event_bus import event_bus, EventType
from models import (
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
    Task,
    TaskLog,
)
from kanban_runtime.assignment_launcher import _tmux_session_name
from kanban_runtime.handoff_protocol import read_status_file
from kanban_runtime.prompt_patterns import detect_prompt
from kanban_runtime.role_supervisor import (
    tmux_capture_pane,
    tmux_send_text,
)

logger = logging.getLogger(__name__)

# In-process cursor: per-session, the trailing-N-char signature of the pane the
# last time we polled. Anything new (i.e. text appearing after that signature)
# is what we ship to AgentActivity.
_pane_cursor: Dict[int, str] = {}
# Per-session pending approval id, so we don't re-file the same prompt.
_pending_approvals: Dict[int, int] = {}
_checkpoint_cursor: Dict[int, str] = {}


def reset_streamer():
    """Clear module-level state for test isolation."""
    _pane_cursor.clear()
    _pending_approvals.clear()
    _checkpoint_cursor.clear()


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _tmux_has_session(session_name: str) -> bool:
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.warning("tmux has-session check failed for %s: %s", session_name, exc)
        return False


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


def _pane_is_ready_for_input(pane: str) -> bool:
    tail = pane[-1200:]
    if "Type your message" in tail:
        return True
    return bool(tail.rstrip().endswith("$") or tail.rstrip().endswith("#"))


def _terminal_completion_summary(pane: str) -> Optional[str]:
    """Legacy fallback: detect completion from terminal text heuristics.

    DEPRECATED: Prefer STATUS.md handoff_ready as the canonical completion
    signal. This fallback fires only when STATUS.md is absent or does not
    indicate handoff_ready, and logs a warning so operators know to update
    the agent's instructions.
    """
    lower = pane.lower()
    markers = [
        "i have completed",
        "i've revamped",
        "changes are ready for review",
        "ready to hand off",
        "here are the **",
        "here are the files",
        "here are the 28 files",
        "now i have a complete picture",
    ]
    has_completed_todos = "# todos" in lower and "[ ]" not in lower and "[x]" in lower
    if not any(marker in lower for marker in markers) and not has_completed_todos:
        return None
    if not _pane_is_ready_for_input(pane):
        return None
    start = max(
        pane.lower().rfind("i have completed"),
        pane.lower().rfind("i've revamped"),
        pane.lower().rfind("here are the **"),
        pane.lower().rfind("here are the files"),
        pane.lower().rfind("here are the 28 files"),
        pane.lower().rfind("now i have a complete picture"),
    )
    if start < 0:
        start = max(0, len(pane) - 3000)
    summary = pane[start:].strip()
    for marker in ("? for shortcuts", "\n >   Type your message", "\nkronos@"):
        idx = summary.find(marker)
        if idx > 0:
            summary = summary[:idx].strip()
    return summary[-4000:] or None


def _check_completion(pane: str, workspace_path: Optional[str]) -> Optional[str]:
    """Check whether the session is complete.

    Two-tier detection:
      1. Primary (structural): Read STATUS.md from the workspace. If
         handoff_ready is true and state is done/completed/review, use the
         frontmatter summary as the completion summary.
      2. Fallback (heuristic): Legacy terminal text marker matching. Fires
         only when STATUS.md is absent or has no handoff signal, and logs a
         deprecation warning so operators can update agent instructions.
    """
    # --- Tier 1: STATUS.md (canonical signal) ---
    if workspace_path:
        try:
            status_data = read_status_file(workspace_path)
            if status_data.get("handoff_ready"):
                state = (status_data.get("state") or "").lower()
                if state in ("done", "completed", "review"):
                    validated = status_data.get("validated")
                    summary = (
                        validated.summary if validated and validated.summary
                        else status_data.get("frontmatter", {}).get("summary", "")
                    )
                    if not summary:
                        summary = f"Agent marked handoff_ready=true, state={state} in STATUS.md"
                    return summary[-4000:]
        except Exception as exc:
            logger.debug("Could not read STATUS.md at %s: %s", workspace_path, exc)

    # --- Tier 2: Terminal text heuristic (deprecated fallback) ---
    fallback = _terminal_completion_summary(pane)
    if fallback:
        logger.warning(
            "Completion detected via terminal text heuristic (DEPRECATED). "
            "Agents should set handoff_ready: true in STATUS.md instead. "
            "workspace=%s",
            workspace_path,
        )
    return fallback


async def _finalize_completed_session(session: AgentSession, pane: str, summary: str) -> bool:
    """Mark the session as DONE and record activity, but do NOT move the task card.

    Card movement must be an explicit decision by the orchestrator or human.
    This only records the activity and session termination.
    """
    now = datetime.now(UTC)
    async with async_session_maker() as db:
        row = (await db.execute(
            select(AgentSession).filter(AgentSession.id == session.id)
        )).scalar_one_or_none()
        task = (await db.execute(
            select(Task).filter(Task.id == session.task_id)
        )).scalar_one_or_none()
        if not row or not task:
            return False

        row.status = AgentSessionStatus.DONE
        row.ended_at = now
        row.last_seen_at = now

        pending = (await db.execute(
            select(AgentApproval).filter(
                AgentApproval.session_id == session.id,
                AgentApproval.status == AgentApprovalStatus.PENDING,
            )
        )).scalars().all()
        from sqlalchemy import update as sa_update
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
                f"Orchestrator or human should decide whether to move the card. "
                f"Summary: {summary[:500]}"
            ),
            workspace_path=session.workspace_path,
            created_at=now,
        ))
        await db.commit()

    await event_bus.publish(
        EventType.AGENT_STATUS_UPDATED.value,
        {
            "agent_id": session.agent_id,
            "session_id": session.id,
            "project_id": session.project_id,
            "task_id": session.task_id,
            "status_type": "done",
            "message": f"Session completed; task #{session.task_id} awaiting orchestrator review",
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


async def _stream_one_session(session: AgentSession, agent_name: str) -> None:
    tmux_session = _tmux_session_name(agent_name, session.task_id)

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

    pane = tmux_capture_pane(tmux_session, lines=200)
    await _upsert_checkpoint(session, pane, status_type)
    summary = _check_completion(pane, session.workspace_path)
    if summary and await _finalize_completed_session(session, pane, summary):
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
        return
    prompt_line, approval_type, yes_reply, no_reply = detection

    latest_approval = await _get_latest_session_approval(session.id)
    pending_id = _pending_approvals.get(session.id)
    same_prompt = bool(latest_approval and latest_approval.message == prompt_line)
    if same_prompt and latest_approval.status == AgentApprovalStatus.PENDING:
        _pending_approvals[session.id] = latest_approval.id
        return
    if same_prompt and (pending_id is not None or latest_approval):
        approval_id = pending_id or latest_approval.id
        resolved = await _resolve_session_id_for_approval(approval_id)
        if resolved is not None:
            decision, response_message = resolved
            if decision == "approved":
                reply = response_message.strip() or yes_reply
            elif decision == "rejected":
                reply = response_message.strip() or no_reply
            else:
                reply = no_reply
            tmux_send_text(tmux_session, reply)
            _pending_approvals.pop(session.id, None)
        return

    async with async_session_maker() as db:
        approval = AgentApproval(
            project_id=session.project_id,
            task_id=session.task_id,
            session_id=session.id,
            agent_id=session.agent_id,
            approval_type=ApprovalType(approval_type),
            title=f"task #{session.task_id}: {approval_type.replace('_', ' ')}",
            message=prompt_line,
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
        logger.info("tmux not available — session streamer disabled")
        return

    while True:
        try:
            await asyncio.sleep(poll_seconds)
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
                from models import Entity
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
