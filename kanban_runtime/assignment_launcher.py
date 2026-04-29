"""
Assignment launcher

Local runtime bridge that turns an existing assignment into an executable
CLI-agent session. The server still does not choose who gets the task; it only
starts the already-assigned local adapter so the assignment is not inert.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import select, and_, desc
from sqlalchemy.orm import selectinload

from database import async_session_maker
from models import (
    ActivityType,
    AgentActivity,
    AgentCheckpoint,
    AgentHeartbeat,
    AgentSession,
    AgentSessionStatus,
    AgentStatusType,
    Comment,
    DecisionType,
    Entity,
    EntityType,
    LeaseStatus,
    Project,
    ApprovalStatus,
    Role,
    Stage,
    Task,
    TaskLease,
    TaskLog,
    TaskStatus,
    OrchestrationDecision,
)
from kanban_runtime.adapter_loader import AdapterSpec, load_all_adapters, standalone_assignment_to_adapter
from kanban_runtime.preferences import load_preferences

logger = logging.getLogger(__name__)


def _safe_session_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-")[:40] or "agent"


def _tmux_session_name(agent_name: str, task_id: int) -> str:
    return f"kanban-task-{task_id}-{_safe_session_part(agent_name)}"


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


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
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


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
) -> str:
    return (
        f"You are the {agent.name} worker for Agent Kanban PM. "
        f"Work on Kanban task #{task.id} in project #{project.id}.\n\n"
        f"Workspace: {workspace_path}\n"
        "Use only the isolated workspace above. Do not read or write the primary "
        "project path unless a human approval explicitly grants it.\n"
        f"Task title: {task.title}\n"
        f"Task description: {task.description or '(none)'}\n\n"
        f"Handoff notes and movement summaries:\n{_handoff_context(task)}\n\n"
        f"Durable restart checkpoint:\n{_checkpoint_context(checkpoint)}\n\n"
        "Read AGENTS.md first. Make focused changes for this task only. "
        "Keep the Kanban server running. If you need permission for file, shell, "
        "network, git, or PR actions, ask in the terminal so the approval queue "
        "can capture it. Report progress back to Kanban when practical."
    )


def _build_agent_command(adapter: AdapterSpec, workspace_path: str, prompt: str) -> list[str]:
    cmd_path = shutil.which(adapter.invoke.command)
    if not cmd_path:
        raise FileNotFoundError(f"CLI not found: {adapter.invoke.command}")

    # Adapter-specific prompt entrypoints. These are launch mechanics only;
    # routing still comes from user/manager assignment. Each branch must use
    # the CLI's NON-INTERACTIVE / one-shot flag so the prompt actually runs
    # instead of dropping the agent into an interactive REPL.
    if adapter.name == "gemini":
        return [cmd_path, "--approval-mode", "default", "-i", prompt]
    if adapter.name == "codex":
        return [cmd_path, "--ask-for-approval", "on-request", "-C", workspace_path, prompt]
    if adapter.name == "claude":
        # `--print` (or `-p`) runs the prompt headlessly and exits.
        return [cmd_path, "--print", "--permission-mode", "default", "--add-dir", workspace_path, prompt]
    if adapter.name == "opencode":
        return [cmd_path, "run", prompt]
    if adapter.name == "aider":
        return [cmd_path, "--message", prompt]
    return [cmd_path, prompt]


def _select_role_for_task(task: Task) -> str:
    text = " ".join([
        task.title or "",
        task.description or "",
        task.required_skills or "",
    ]).lower()
    role_markers = [
        ("git_pr", ("pull request", "github", "git ", "branch", "commit", "push", "pr sync")),
        ("diff_review", ("diff", "review", "security", "auth", "migration")),
        ("test", ("test", "pytest", "regression", "smoke", "verify", "checker")),
        ("architecture", ("architecture", "design", "schema", "model", "refactor", "cross-cutting")),
        ("ui", ("ui", "frontend", "css", "html", "template", "mobile", "desktop", "glass", "ux")),
    ]
    for role_name, markers in role_markers:
        if any(marker in text for marker in markers):
            return role_name
    return "worker"


class AssignmentLauncher:
    def __init__(self, api_base: str = "http://127.0.0.1:8000"):
        self.api_base = api_base

    async def handle_event(self, payload: dict) -> None:
        data = payload.get("data") or {}
        task_id = data.get("task_id")
        entity_id = data.get("entity_id")
        if not task_id or not entity_id:
            return
        await self.launch_for_assignment(int(task_id), int(entity_id))

    async def launch_for_assignment(self, task_id: int, entity_id: int) -> Optional[int]:
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
            if not workspace_path:
                db.add(AgentActivity(
                    agent_id=agent.id,
                    project_id=project.id,
                    task_id=task.id,
                    activity_type=ActivityType.ERROR,
                    source="assignment_launcher",
                    message="Assignment was not started because an isolated git worktree could not be created",
                    workspace_path=project.path,
                ))
                await db.commit()
                return None

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
                now = datetime.utcnow()
                for stale_session in existing_sessions[1:]:
                    stale_session.status = AgentSessionStatus.DONE
                    stale_session.ended_at = now
                    stale_session.last_seen_at = now
                await self._mark_task_started(db, task, agent)
                await db.commit()
                return existing_session.id
            if existing_sessions:
                now = datetime.utcnow()
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
            prompt = _build_prompt(task, project, agent, workspace_path, checkpoint)
            args = _build_agent_command(adapter, workspace_path, prompt)
            shell_command = shlex.join(args)

            db_session = AgentSession(
                agent_id=agent.id,
                project_id=project.id,
                task_id=task.id,
                workspace_path=workspace_path,
                status=AgentSessionStatus.ACTIVE,
                command=shell_command,
                model=adapter.models[0].id if adapter.models else None,
                mode="headless",
            )
            db.add(db_session)
            await db.flush()

            now = datetime.utcnow()
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
                message=f"Auto-starting {agent.name} for assigned task #{task.id} in isolated git worktree",
                workspace_path=workspace_path,
                command=shell_command,
            ))
            await self._mark_task_started(db, task, agent)

            await db.commit()
            session_id = db_session.id

        env = os.environ.copy()
        env["KANBAN_AGENT_NAME"] = agent.name
        env["KANBAN_AGENT_ROLE"] = "worker"
        env["KANBAN_API_BASE"] = self.api_base
        if _tmux_has_session(session_name):
            subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True, timeout=5)
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", workspace_path],
            capture_output=True,
            check=True,
            timeout=10,
        )
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}"
            for key, value in env.items()
            if key.startswith("KANBAN_")
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, f"{env_prefix} {shell_command}", "Enter"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        logger.info("Auto-started %s for task #%s in tmux session %s", agent.name, task_id, session_name)
        return session_id

    async def _mark_task_started(self, db, task: Task, agent: Entity) -> None:
        if task.status == TaskStatus.IN_PROGRESS and task.stage and task.stage.name.strip().lower() == "in progress":
            return
        stage_result = await db.execute(
            select(Stage)
            .filter(Stage.project_id == task.project_id)
            .order_by(Stage.order)
        )
        stages = stage_result.scalars().all()
        in_progress_stage = next(
            (stage for stage in stages if stage.name and stage.name.strip().lower() in {"in progress", "in_progress"}),
            None,
        )
        old_stage = task.stage.name if task.stage else task.stage_id
        if in_progress_stage:
            task.stage_id = in_progress_stage.id
            task.stage = in_progress_stage
        task.status = TaskStatus.IN_PROGRESS
        task.version += 1
        task.updated_at = datetime.utcnow()
        db.add(TaskLog(
            task_id=task.id,
            message=(
                f"Execution started by {agent.name}; moved from {old_stage} "
                "to In Progress after a tmux-backed task session was available"
            ),
            log_type="handoff",
        ))

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
        return resumed

    async def assign_orphaned_tasks(self, workspace_path: Optional[str] = None) -> int:
        """Assign unowned active tasks to configured roles and reset them to To Do.

        This is recovery, not routing for healthy work. It handles cards that are
        already in a runnable state but lost their assignee due to a failed UI
        assign, restart, or stale board state.
        """
        prefs = load_preferences()
        if not prefs:
            return 0
        role_assignments = prefs.get_role_assignments()
        adapters = {a.name: a for a in load_all_adapters()}
        assigned = 0

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
            tasks = [task for task in result.scalars().all() if not task.assignees and task.project_id]

            for task in tasks:
                todo_result = await db.execute(
                    select(Stage)
                    .filter(Stage.project_id == task.project_id)
                    .order_by(Stage.order)
                )
                stages = todo_result.scalars().all()
                todo_stage = next(
                    (stage for stage in stages if stage.name and stage.name.strip().lower() in {"to do", "todo"}),
                    None,
                )
                if not todo_stage:
                    continue

                role_name = _select_role_for_task(task)
                assignment = role_assignments.get(role_name) or role_assignments.get("worker")
                if not assignment:
                    db.add(TaskLog(
                        task_id=task.id,
                        message=f"Auto assignment skipped: no configured role for {role_name} and no worker fallback",
                        log_type="error",
                    ))
                    continue

                adapter = adapters.get(assignment.agent)
                command = adapter.invoke.command if adapter else (assignment.command or assignment.agent)
                active = shutil.which(command) is not None
                entity_result = await db.execute(
                    select(Entity).filter(Entity.name == assignment.agent, Entity.entity_type == EntityType.AGENT)
                )
                entity = entity_result.scalar_one_or_none()
                skills = ", ".join(adapter.capabilities) if adapter else ", ".join(assignment.capabilities or [role_name])
                if entity:
                    entity.skills = skills
                    entity.role = Role.MANAGER if role_name == "orchestrator" else Role.WORKER
                    entity.is_active = active
                else:
                    entity = Entity(
                        name=assignment.agent,
                        entity_type=EntityType.AGENT,
                        skills=skills,
                        role=Role.MANAGER if role_name == "orchestrator" else Role.WORKER,
                        is_active=active,
                    )
                    db.add(entity)
                    await db.flush()

                if not entity.is_active:
                    db.add(TaskLog(
                        task_id=task.id,
                        message=f"Auto assignment skipped: CLI '{command}' for role '{role_name}' is not installed",
                        log_type="error",
                    ))
                    continue

                old_stage = task.stage.name if task.stage else task.stage_id
                task.stage_id = todo_stage.id
                task.stage = todo_stage
                task.status = TaskStatus.PENDING
                task.assignees.append(entity)
                task.version += 1
                task.updated_at = datetime.utcnow()
                db.add(TaskLog(
                    task_id=task.id,
                    message=(
                        f"Auto-assigned orphaned task to {role_name} role agent {entity.name}; "
                        f"moved from {old_stage} to To Do for execution recovery"
                    ),
                    log_type="action",
                ))
                db.add(OrchestrationDecision(
                    project_id=task.project_id,
                    manager_agent_id=entity.id,
                    decision_type=DecisionType.TASK_ASSIGN,
                    input_summary="Recovered orphaned active task with no assignee",
                    rationale=f"Selected role '{role_name}' from task text/skills and assigned {entity.name}.",
                    affected_task_ids=str(task.id),
                    affected_agent_ids=str(entity.id),
                ))
                assigned += 1

            await db.commit()

        if assigned:
            logger.info("Auto-assigned %s orphaned task(s) to configured roles", assigned)
        return assigned


assignment_launcher = AssignmentLauncher()
