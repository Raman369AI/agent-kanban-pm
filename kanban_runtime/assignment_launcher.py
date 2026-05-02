"""
Assignment launcher

Local runtime bridge that turns an existing assignment into an executable
CLI-agent session. The server still does not choose who gets the task; it only
starts the already-assigned local adapter so the assignment is not inert.
"""

from __future__ import annotations

import logging
import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select, and_, desc
from sqlalchemy.orm import selectinload

from database import async_session_maker
from event_bus import EventType, event_bus
from models import (
    ActivityType,
    AgentActivity,
    AgentCheckpoint,
    AgentHeartbeat,
    AgentSession,
    AgentSessionStatus,
    AgentStatusType,
    Comment,
    Entity,
    EntityType,
    LeaseStatus,
    Project,
    ApprovalStatus,
    Stage,
    Task,
    TaskLease,
    TaskLog,
    TaskStatus,
)
from kanban_runtime.adapter_loader import AdapterSpec, load_all_adapters, standalone_assignment_to_adapter
from kanban_runtime.handoff_protocol import (
    build_handoff_instructions,
    ensure_instruction_aliases,
    initialize_status_file,
    read_status_file,
)
from kanban_runtime.process_launcher import (
    shell_command,
    start_tmux_session,
    tmux_available,
    tmux_has_session,
    tmux_kill_session,
)
from kanban_runtime.preferences import load_preferences
from kanban_runtime.instance import get_tmux_prefix

logger = logging.getLogger(__name__)


def _safe_session_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:40] or "agent"


def _tmux_session_name(agent_name: str, task_id: int) -> str:
    return f"{get_tmux_prefix()}-task-{task_id}-{_safe_session_part(agent_name)}"


def _tmux_available() -> bool:
    return tmux_available()


def _git_worktree_path(project: Project, task: Task, agent: Entity) -> Path:
    root = Path.home() / ".kanban" / "worktrees" / f"project-{project.id}"
    return root / f"task-{task.id}-{_safe_session_part(agent.name)}"


def _create_git_worktree(project_path: str, worktree_path: Path) -> Optional[str]:
    """Create or reuse a detached git worktree for one task session."""
    git = shutil.which("git")
    if not git:
        logger.warning("Cannot create isolated task workspace: git is not installed")
        return None

    try:
        inside = subprocess.run(
            [git, "-C", project_path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            logger.warning("Cannot create isolated task workspace: %s is not a git worktree", project_path)
            return None

        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        if worktree_path.exists():
            registered = subprocess.run(
                [git, "-C", project_path, "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            marker = f"worktree {worktree_path}"
            if registered.returncode == 0 and marker in registered.stdout:
                return str(worktree_path)
            logger.warning("Cannot reuse task workspace because it exists but is not a registered git worktree: %s", worktree_path)
            return None

        result = subprocess.run(
            [git, "-C", project_path, "worktree", "add", "--detach", str(worktree_path), "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git worktree add failed for %s: %s", worktree_path, result.stderr.strip())
            return None
        return str(worktree_path)
    except Exception as exc:
        logger.warning("Cannot create isolated task workspace: %s", exc)
        return None


def _tmux_has_session(session_name: str) -> bool:
    return tmux_has_session(session_name)


def _handoff_context(task: Task) -> str:
    notes: list[str] = []
    for log in sorted(task.logs or [], key=lambda item: item.created_at or datetime.min)[-8:]:
        notes.append(f"- {log.created_at}: {log.message}")
    for comment in sorted(task.comments or [], key=lambda item: item.created_at or datetime.min)[-8:]:
        author = comment.author.name if comment.author else f"entity #{comment.author_id}"
        notes.append(f"- {comment.created_at} {author}: {comment.content}")
    if not notes:
        return "No handoff notes recorded yet."
    return "\n".join(notes[-12:])


def _checkpoint_context(checkpoint: Optional[AgentCheckpoint]) -> str:
    if not checkpoint:
        return "No prior checkpoint recorded for this task and agent."
    parts = [
        f"Checkpoint #{checkpoint.id} from {checkpoint.updated_at}:",
        checkpoint.summary,
    ]
    if checkpoint.workspace_path:
        parts.append(f"Previous workspace: {checkpoint.workspace_path}")
    if checkpoint.terminal_tail:
        parts.append("Recent terminal tail:\n" + checkpoint.terminal_tail[-2000:])
    return "\n".join(parts)


def _build_prompt(
    task: Task,
    project: Project,
    agent: Entity,
    workspace_path: str,
    checkpoint: Optional[AgentCheckpoint] = None,
    isolated_workspace: bool = True,
) -> str:
    workspace_guidance = (
        "Use only the isolated workspace above. Do not read or write the primary "
        "project path unless a human approval explicitly grants it."
        if isolated_workspace
        else "This project is not a Git worktree, so the task is running in the "
        "configured project workspace. Keep changes focused to this task and ask "
        "for approval before broad or destructive edits."
    )
    return (
        f"You are the {agent.name} worker for Agent Kanban PM. "
        f"Work on Kanban task #{task.id} in project #{project.id}.\n\n"
        f"Workspace: {workspace_path}\n"
        f"{workspace_guidance}\n"
        f"Task title: {task.title}\n"
        f"Task description: {task.description or '(none)'}\n\n"
        f"Handoff notes and movement summaries:\n{_handoff_context(task)}\n\n"
        f"Durable restart checkpoint:\n{_checkpoint_context(checkpoint)}\n\n"
        f"{build_handoff_instructions(agent.name, workspace_path)}\n\n"
        "Make focused changes for this task only. "
        "Keep the Kanban server running. If you need permission for file, shell, "
        "network, git, or PR actions, ask in the terminal so the approval queue "
        "can capture it. Report progress back to Kanban when practical."
    )


def _build_agent_command(adapter: AdapterSpec, workspace_path: str, prompt: str) -> list[str]:
    cmd_path = shutil.which(adapter.invoke.command)
    if not cmd_path:
        raise FileNotFoundError(f"CLI not found: {adapter.invoke.command}")

    if adapter.task_command.prompt_file:
        prompt_path = Path(workspace_path) / adapter.task_command.prompt_file
        prompt_path.write_text(prompt, encoding="utf-8")

    rendered_args = [
        arg.replace("{workspace}", workspace_path).replace("{prompt}", prompt)
        for arg in adapter.task_command.args
    ]
    return [cmd_path, *rendered_args]


def _select_role_for_task(task: Task) -> str:
    """DEPRECATED: server must not choose roles from task text.
    
    Kept only as a fallback for orphaned tasks with no role hint. Returns
    'worker' unconditionally; the orchestrator should assign the correct role
    via MCP after seeing the orphaned-task notification.
    """
    return "worker"


class AssignmentLauncher:
    def __init__(self, api_base: str = ""):
        if api_base:
            self.api_base = api_base
        else:
            from kanban_runtime.instance import get_api_base
            self.api_base = get_api_base()

    def reset(self):
        """Reset for test isolation. AssignmentLauncher is stateless."""
        pass

    async def handle_event(self, payload: dict) -> None:
        data = payload.get("data") or {}
        task_id = data.get("task_id")
        entity_id = data.get("entity_id")
        if not task_id or not entity_id:
            return
        await self.launch_for_assignment(int(task_id), int(entity_id), assigned_role=data.get("role"))

    async def launch_for_assignment(self, task_id: int, entity_id: int, assigned_role: Optional[str] = None) -> Optional[int]:
        if not _tmux_available():
            logger.warning("Cannot auto-start assigned agent: tmux is not installed")
            return None

        adapters = {a.name: a for a in load_all_adapters()}
        prefs = load_preferences()
        role_assignments = prefs.get_role_assignments() if prefs else {}

        async with async_session_maker() as db:
            task_result = await db.execute(
                select(Task)
                .filter(Task.id == task_id)
                .options(
                    selectinload(Task.assignees),
                    selectinload(Task.project),
                    selectinload(Task.stage),
                    selectinload(Task.logs),
                    selectinload(Task.comments).selectinload(Comment.author),
                )
            )
            task = task_result.scalar_one_or_none()
            if not task or task.status == TaskStatus.COMPLETED:
                return None
            runnable_stages = {"to do", "todo", "in progress", "in_progress"}
            if not task.stage or task.stage.name.strip().lower() not in runnable_stages:
                logger.info("Task %s is assigned but not in a runnable stage; waiting for manual board movement", task.id)
                return None

            agent_result = await db.execute(
                select(Entity).filter(Entity.id == entity_id, Entity.entity_type == EntityType.AGENT)
            )
            agent = agent_result.scalar_one_or_none()
            if not agent or not agent.is_active:
                return None

            project = task.project
            if not project:
                project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
                project = project_result.scalar_one_or_none()
            if not project or not project.path:
                logger.warning("Cannot auto-start %s for task %s: project has no path", agent.name, task.id)
                return None
            workspace_path = _create_git_worktree(project.path, _git_worktree_path(project, task, agent))
            isolated_workspace = True
            if not workspace_path:
                if not Path(project.path).is_dir():
                    db.add(AgentActivity(
                        agent_id=agent.id,
                        project_id=project.id,
                        task_id=task.id,
                        activity_type=ActivityType.ERROR,
                        source="assignment_launcher",
                        message="Assignment was not started because the project workspace path does not exist",
                        workspace_path=project.path,
                    ))
                    await db.commit()
                    return None
                workspace_path = project.path
                isolated_workspace = False

            matching_role_name = assigned_role or next(
                (role_name for role_name, assignment in role_assignments.items() if assignment.agent == agent.name),
                "worker",
            )
            alias_results = ensure_instruction_aliases(workspace_path)
            status_path = initialize_status_file(
                workspace_path,
                task_id=task.id,
                project_id=project.id,
                current_agent=agent.name,
                assigned_role=matching_role_name,
                task_title=task.title,
            )

            adapter = adapters.get(agent.name)
            if not adapter:
                matching_role = next(
                    ((role_name, assignment) for role_name, assignment in role_assignments.items() if assignment.agent == agent.name),
                    None,
                )
                if not matching_role:
                    logger.warning("Cannot auto-start task %s: no role adapter for agent %s", task.id, agent.name)
                    return None
                adapter = standalone_assignment_to_adapter(matching_role[0], matching_role[1])

            existing_result = await db.execute(
                select(AgentSession).filter(
                    AgentSession.agent_id == agent.id,
                    AgentSession.task_id == task.id,
                    AgentSession.ended_at.is_(None),
                ).order_by(AgentSession.started_at.desc())
            )
            existing_sessions = existing_result.scalars().all()
            session_name = _tmux_session_name(agent.name, task.id)
            if existing_sessions and _tmux_has_session(session_name):
                existing_session = existing_sessions[0]
                now = datetime.now(UTC)
                for stale_session in existing_sessions[1:]:
                    stale_session.status = AgentSessionStatus.DONE
                    stale_session.ended_at = now
                    stale_session.last_seen_at = now
                task_started_event = await self._mark_task_started(db, task, agent)
                await db.commit()
                await self._publish_task_started(task_started_event)
                return existing_session.id
            if existing_sessions:
                now = datetime.now(UTC)
                for stale_session in existing_sessions:
                    stale_session.status = AgentSessionStatus.ERROR
                    stale_session.ended_at = now
                    stale_session.last_seen_at = now

            checkpoint_result = await db.execute(
                select(AgentCheckpoint)
                .filter(
                    AgentCheckpoint.task_id == task.id,
                    AgentCheckpoint.agent_id == agent.id,
                )
                .order_by(desc(AgentCheckpoint.updated_at))
                .limit(1)
            )
            checkpoint = checkpoint_result.scalar_one_or_none()
            prompt = _build_prompt(task, project, agent, workspace_path, checkpoint, isolated_workspace)
            args = _build_agent_command(adapter, workspace_path, prompt)
            command_text = shell_command(args)

            db_session = AgentSession(
                agent_id=agent.id,
                project_id=project.id,
                task_id=task.id,
                workspace_path=workspace_path,
                status=AgentSessionStatus.ACTIVE,
                command=command_text,
                model=adapter.models[0].id if adapter.models else None,
                mode="headless",
            )
            db.add(db_session)
            await db.flush()

            now = datetime.now(UTC)
            older_leases = await db.execute(
                select(TaskLease).filter(
                    TaskLease.task_id == task.id,
                    TaskLease.agent_id == agent.id,
                    TaskLease.status == LeaseStatus.ACTIVE,
                )
            )
            for lease in older_leases.scalars().all():
                lease.status = LeaseStatus.RELEASED
                lease.released_at = now

            db.add(TaskLease(
                task_id=task.id,
                agent_id=agent.id,
                session_id=db_session.id,
                status=LeaseStatus.ACTIVE,
                expires_at=now + timedelta(hours=1),
            ))

            heartbeat_result = await db.execute(
                select(AgentHeartbeat).filter(AgentHeartbeat.agent_id == agent.id)
            )
            heartbeat = heartbeat_result.scalar_one_or_none()
            if heartbeat:
                heartbeat.task_id = task.id
                heartbeat.status_type = AgentStatusType.WORKING
                heartbeat.message = f"Starting task #{task.id}: {task.title}"
                heartbeat.updated_at = now
            else:
                db.add(AgentHeartbeat(
                    agent_id=agent.id,
                    task_id=task.id,
                    status_type=AgentStatusType.WORKING,
                    message=f"Starting task #{task.id}: {task.title}",
                    updated_at=now,
                ))

            db.add(AgentActivity(
                agent_id=agent.id,
                session_id=db_session.id,
                project_id=project.id,
                task_id=task.id,
                activity_type=ActivityType.ACTION,
                source="assignment_launcher",
                message=(
                    f"Auto-starting {agent.name} for assigned task #{task.id} in "
                    f"{'isolated git worktree' if isolated_workspace else 'project workspace'}; "
                    f"handoff status is {status_path}"
                ),
                workspace_path=workspace_path,
                command=command_text,
                payload_json=json.dumps({"instruction_aliases": alias_results}),
            ))
            task_started_event = await self._mark_task_started(db, task, agent)

            await db.commit()
            session_id = db_session.id
            await self._publish_task_started(task_started_event)

        env = os.environ.copy()
        env["KANBAN_AGENT_NAME"] = agent.name
        env["KANBAN_AGENT_ROLE"] = matching_role_name
        env["KANBAN_API_BASE"] = self.api_base
        if _tmux_has_session(session_name):
            tmux_kill_session(session_name)
        start_tmux_session(
            session_name=session_name,
            cwd=workspace_path,
            args=args,
            env=env,
            kill_existing=False,
        )
        logger.info("Auto-started %s for task #%s in tmux session %s", agent.name, task_id, session_name)
        return session_id

    async def _mark_task_started(self, db, task: Task, agent: Entity) -> Optional[dict]:
        """Record that execution has started and reflect it on the board."""
        current_stage_name = task.stage.name if task.stage else str(task.stage_id)
        current_stage_key = current_stage_name.strip().lower() if current_stage_name else ""
        already_in_progress = (
            task.status == TaskStatus.IN_PROGRESS
            and current_stage_key in {"in progress", "in_progress"}
        )
        if already_in_progress:
            return None

        old_stage_id = task.stage_id
        old_stage_name = current_stage_name
        in_progress_stage_id = task.stage_id
        in_progress_stage_name = current_stage_name

        if current_stage_key not in {"in progress", "in_progress"}:
            stages_result = await db.execute(
                select(Stage)
                .filter(Stage.project_id == task.project_id)
                .order_by(Stage.order)
            )
            in_progress_stage = next(
                (
                    stage for stage in stages_result.scalars().all()
                    if stage.name.strip().lower() in {"in progress", "in_progress"}
                ),
                None,
            )
            if in_progress_stage:
                task.stage_id = in_progress_stage.id
                task.stage = in_progress_stage
                in_progress_stage_id = in_progress_stage.id
                in_progress_stage_name = in_progress_stage.name

        task.status = TaskStatus.IN_PROGRESS
        task.version += 1
        task.updated_at = datetime.now(UTC)
        current_stage = task.stage.name if task.stage else str(task.stage_id)
        db.add(TaskLog(
            task_id=task.id,
            message=(
                f"Execution started by {agent.name}; moved from "
                f"'{old_stage_name}' to '{current_stage}' and marked in progress."
            ),
            log_type="action",
        ))
        return {
            "task_id": task.id,
            "title": task.title,
            "project_id": task.project_id,
            "entity_id": agent.id,
            "from_stage_id": old_stage_id,
            "to_stage_id": in_progress_stage_id,
            "from_stage_name": old_stage_name,
            "to_stage_name": in_progress_stage_name,
            "status": TaskStatus.IN_PROGRESS.value,
            "stage_changed": old_stage_id != in_progress_stage_id,
        }

    async def _publish_task_started(self, task_started_event: Optional[dict]) -> None:
        if not task_started_event:
            return
        project_id = task_started_event["project_id"]
        entity_id = task_started_event["entity_id"]
        if task_started_event["stage_changed"]:
            await event_bus.publish(
                EventType.TASK_MOVED.value,
                {
                    "task_id": task_started_event["task_id"],
                    "title": task_started_event["title"],
                    "from_stage_id": task_started_event["from_stage_id"],
                    "to_stage_id": task_started_event["to_stage_id"],
                    "status": task_started_event["status"],
                    "summary": "Execution started by assigned agent",
                },
                project_id=project_id,
                entity_id=entity_id,
            )
        await event_bus.publish(
            EventType.TASK_UPDATED.value,
            {
                "task_id": task_started_event["task_id"],
                "title": task_started_event["title"],
                "status": task_started_event["status"],
                "stage_id": task_started_event["to_stage_id"],
            },
            project_id=project_id,
            entity_id=entity_id,
        )

    async def resume_runnable_assignments(self, workspace_path: Optional[str] = None) -> int:
        """Replay already-assigned runnable tasks after a server/runtime restart."""
        if not _tmux_available():
            logger.warning("Cannot resume assigned agents: tmux is not installed")
            return 0

        await self.assign_orphaned_tasks(workspace_path=workspace_path)

        async with async_session_maker() as db:
            query = (
                select(Task)
                .options(selectinload(Task.assignees), selectinload(Task.stage))
                .join(Project, Task.project_id == Project.id)
                .filter(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]))
                .filter(Project.approval_status == ApprovalStatus.APPROVED)
                .filter(Project.path.is_not(None))
                .filter(Project.path != "")
            )
            if workspace_path:
                query = query.filter(Project.path == workspace_path)
            result = await db.execute(query)
            tasks = [
                task for task in result.scalars().all()
                if task.assignees
                and task.stage
                and task.stage.name.strip().lower() in {"to do", "todo", "in progress", "in_progress"}
            ]

        resumed = 0
        for task in tasks:
            for assignee in task.assignees:
                session_id = await self.launch_for_assignment(task.id, assignee.id)
                if session_id:
                    resumed += 1
        if resumed:
            logger.info("Resumed %s assigned task session(s)", resumed)

        await self.scan_and_advance_completed_tasks()
        return resumed

    async def scan_and_advance_completed_tasks(self) -> int:
        """Read STATUS.md for active task sessions; emit activity suggestions only.

        Does NOT move cards. The orchestrator or human must make explicit
        transition decisions. Cards stay where they are until moved by intent.
        """
        reported = 0
        async with async_session_maker() as db:
            sessions_result = await db.execute(
                select(AgentSession)
                .filter(AgentSession.status == AgentSessionStatus.ACTIVE)
                .filter(AgentSession.workspace_path.is_not(None))
                .filter(AgentSession.task_id.is_not(None))
                .options(selectinload(AgentSession.task))
            )
            sessions = sessions_result.scalars().all()

            for session in sessions:
                task = session.task
                if not task or task.status == TaskStatus.COMPLETED:
                    continue
                try:
                    status_data = read_status_file(session.workspace_path)
                except Exception as exc:
                    logger.warning("Failed to read STATUS.md at %s: %s", session.workspace_path, exc)
                    continue
                if not status_data.get("handoff_ready"):
                    continue
                state = (status_data.get("state") or "").lower()
                if state not in ("done", "completed", "review"):
                    continue

                db.add(AgentActivity(
                    agent_id=session.agent_id,
                    session_id=session.id,
                    project_id=session.project_id,
                    task_id=session.task_id,
                    activity_type=ActivityType.HANDOFF,
                    source="session_streamer",
                    message=(
                        f"Task #{task.id} appears ready for review: STATUS.md handoff_ready=true "
                        f"(state={state}, agent={status_data.get('current_agent', '?')}). "
                        "Orchestrator or human should move the card to Review."
                    ),
                    workspace_path=session.workspace_path,
                ))
                reported += 1

            if reported:
                await db.commit()

        if reported:
            logger.info("Reported %s task(s) with handoff_ready=true (no auto-move)", reported)
        return reported

    async def assign_orphaned_tasks(self, workspace_path: Optional[str] = None) -> int:
        """Report orphaned active tasks that have no assignees.

        Does NOT auto-assign roles or move cards. The orchestrator agent
        decides who picks up orphaned work. This only logs a summary so
        the orchestrator can see which tasks need attention.
        """
        async with async_session_maker() as db:
            query = (
                select(Task)
                .options(selectinload(Task.assignees), selectinload(Task.stage), selectinload(Task.project))
                .join(Project, Task.project_id == Project.id)
                .filter(Task.status.in_([TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]))
                .filter(Project.approval_status == ApprovalStatus.APPROVED)
                .filter(Project.path.is_not(None))
                .filter(Project.path != "")
            )
            if workspace_path:
                query = query.filter(Project.path == workspace_path)
            result = await db.execute(query)
            orphaned = [task for task in result.scalars().all() if not task.assignees and task.project_id]

            for task in orphaned:
                db.add(TaskLog(
                    task_id=task.id,
                    message=(
                        f"Orphaned task detected: no assignee, status={task.status.value}, "
                        f"stage={task.stage.name if task.stage else '?'}. "
                        "Orchestrator or human should assign this task."
                    ),
                    log_type="info",
                ))

            if orphaned:
                await db.commit()

        if orphaned:
            logger.info("Reported %s orphaned task(s) (no auto-assignment)", len(orphaned))
        return len(orphaned)


assignment_launcher = AssignmentLauncher()
