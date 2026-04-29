from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc, and_
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import datetime, timedelta
import logging
import json
import subprocess

from database import get_db
from models import (
    AgentHeartbeat, AgentActivity, AgentSession, Entity, EntityType, Task, Project,
    AgentStatusType, ActivityType, AgentSessionStatus, ProjectWorkspace,
    OrchestrationDecision, TaskLease, ActivitySummary, AgentCheckpoint, UserContribution,
    LeaseStatus, ContributionType, DiffReview, DiffReviewStatus,
    AgentApproval, AgentApprovalStatus, ApprovalType
)
from schemas import (
    AgentHeartbeatResponse, AgentActivityResponse, AgentActivityCreate,
    AgentStatusUpdate, AgentSessionCreate, AgentSessionUpdate, AgentSessionResponse,
    ProjectWorkspaceCreate, ProjectWorkspaceResponse,
    OrchestrationDecisionCreate, OrchestrationDecisionResponse,
    TaskLeaseCreate, TaskLeaseResponse,
    ActivitySummaryCreate, ActivitySummaryResponse,
    AgentCheckpointCreate, AgentCheckpointResponse,
    UserContributionCreate, UserContributionResponse,
    AgentTerminalResponse,
    DiffReviewCreate, DiffReviewUpdate, DiffReviewResponse,
    AgentApprovalCreate, AgentApprovalResolve, AgentApprovalResponse
)
from auth import get_current_entity, is_owner_or_manager
from event_bus import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/agents", tags=["agent-activity"])


def _parse_github_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _run_project_command(command: list[str], cwd: str) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return result.stdout.strip()


def _github_repo_from_remote(remote_url: str) -> Optional[str]:
    remote_url = remote_url.strip()
    if not remote_url:
        return None
    if remote_url.endswith(".git"):
        remote_url = remote_url[:-4]
    if remote_url.startswith("git@github.com:"):
        return remote_url.split("git@github.com:", 1)[1]
    marker = "github.com/"
    if marker in remote_url:
        return remote_url.split(marker, 1)[1]
    return None


def _discover_github_repos(workspace_path: str) -> list[str]:
    repos: list[str] = []
    for remote in ("upstream", "origin"):
        try:
            url = _run_project_command(["git", "remote", "get-url", remote], workspace_path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
        repo = _github_repo_from_remote(url)
        if repo and repo not in repos:
            repos.append(repo)
    return repos


def _github_current_user(workspace_path: str) -> Optional[str]:
    try:
        return _run_project_command(["gh", "api", "user", "--jq", ".login"], workspace_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _gh_available() -> bool:
    import shutil as _shutil
    return _shutil.which("gh") is not None


def _git_local_commits(workspace_path: str, author: str, limit: int = 50) -> list[dict]:
    """Fallback when `gh` is unavailable: enumerate local commits authored by `author`.

    Returns a list of dicts shaped like the gh `search commits --json` output we use:
      {"sha", "title", "url" (None), "createdAt", "updatedAt"}
    """
    try:
        log_format = "%H%x09%aI%x09%s"
        output = _run_project_command(
            ["git", "log", f"--author={author}", f"--pretty=format:{log_format}", f"-n{limit}"],
            workspace_path,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []
    commits = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        sha, when, subject = parts[0], parts[1], "\t".join(parts[2:])
        commits.append({
            "sha": sha,
            "title": subject,
            "url": None,
            "createdAt": when,
            "updatedAt": when,
        })
    return commits


def _is_git_pr_role(entity: Optional[Entity]) -> bool:
    """Check if the given entity is assigned the git_pr role in preferences.

    Per AGENTS.md: Git/PR operations are assigned only to the Git PR Agent.
    Other roles should not open PRs unless the human explicitly overrides.
    """
    if not entity:
        return False
    from kanban_runtime.preferences import load_preferences
    prefs = load_preferences()
    if not prefs:
        return True
    roles = prefs.get_roles()
    if roles and roles.git_pr and roles.git_pr.agent == entity.name:
        return True
    if roles and not roles.git_pr:
        return True
    if not roles and prefs.manager and prefs.manager.agent == entity.name:
        return True
    return False


@router.get("/projects/{project_id}/workspaces", response_model=List[ProjectWorkspaceResponse])
async def get_project_workspaces(
    project_id: int,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ProjectWorkspace)
        .filter(ProjectWorkspace.project_id == project_id)
        .order_by(desc(ProjectWorkspace.is_primary), ProjectWorkspace.created_at)
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/workspaces", response_model=ProjectWorkspaceResponse, status_code=status.HTTP_201_CREATED)
async def create_project_workspace(
    project_id: int,
    workspace: ProjectWorkspaceCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can add workspaces")
    if workspace.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")

    if workspace.is_primary:
        existing = await db.execute(select(ProjectWorkspace).filter(ProjectWorkspace.project_id == project_id))
        for item in existing.scalars().all():
            item.is_primary = False

    db_workspace = ProjectWorkspace(**workspace.model_dump())
    db.add(db_workspace)
    await db.commit()
    await db.refresh(db_workspace)
    return db_workspace


@router.get("/projects/{project_id}/decisions", response_model=List[OrchestrationDecisionResponse])
async def get_project_decisions(
    project_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(OrchestrationDecision)
        .filter(OrchestrationDecision.project_id == project_id)
        .order_by(desc(OrchestrationDecision.created_at))
        .limit(limit)
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/decisions", response_model=OrchestrationDecisionResponse, status_code=status.HTTP_201_CREATED)
async def log_project_decision(
    project_id: int,
    decision: OrchestrationDecisionCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can log orchestration decisions")
    if decision.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")

    db_decision = OrchestrationDecision(**decision.model_dump())
    if db_decision.manager_agent_id is None and current_entity.entity_type.value == "agent":
        db_decision.manager_agent_id = current_entity.id
    db.add(db_decision)
    await db.commit()
    await db.refresh(db_decision)

    await event_bus.publish(
        EventType.ORCHESTRATION_DECISION_LOGGED.value,
        {
            "decision_id": db_decision.id,
            "project_id": project_id,
            "decision_type": db_decision.decision_type.value,
            "rationale": db_decision.rationale,
            "affected_task_ids": db_decision.affected_task_ids,
            "affected_agent_ids": db_decision.affected_agent_ids,
        },
        project_id=project_id,
        entity_id=current_entity.id
    )
    return db_decision


@router.get("/projects/{project_id}/summaries", response_model=List[ActivitySummaryResponse])
async def get_project_summaries(
    project_id: int,
    limit: int = 20,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ActivitySummary)
        .filter(ActivitySummary.project_id == project_id)
        .order_by(desc(ActivitySummary.created_at))
        .limit(limit)
    )
    return result.scalars().all()


@router.post("/projects/{project_id}/summaries", response_model=ActivitySummaryResponse, status_code=status.HTTP_201_CREATED)
async def create_activity_summary(
    project_id: int,
    summary: ActivitySummaryCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if summary.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")
    if summary.agent_id is not None and not is_owner_or_manager(current_entity) and summary.agent_id != current_entity.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only summarize your own activity")

    db_summary = ActivitySummary(**summary.model_dump())
    db.add(db_summary)
    await db.commit()
    await db.refresh(db_summary)

    await event_bus.publish(
        EventType.ACTIVITY_SUMMARY_CREATED.value,
        {"summary_id": db_summary.id, "project_id": project_id, "summary": db_summary.summary},
        project_id=project_id,
        entity_id=current_entity.id
    )
    return db_summary


@router.get("/projects/{project_id}/contributions", response_model=List[UserContributionResponse])
async def get_project_contributions(
    project_id: int,
    entity_id: Optional[int] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    query = (
        select(UserContribution)
        .filter(UserContribution.project_id == project_id)
        .order_by(desc(UserContribution.updated_at_external), desc(UserContribution.recorded_at))
    )
    if entity_id:
        query = query.filter(UserContribution.entity_id == entity_id)
    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.post("/projects/{project_id}/contributions/sync/github")
async def sync_github_contributions(
    project_id: int,
    author: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Sync GitHub PRs/issues authored by the current GitHub user into contribution records."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    if current_entity.entity_type != EntityType.HUMAN and not _is_git_pr_role(current_entity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only humans or the assigned Git PR role may sync GitHub contribution visibility. "
                   "Branch, push, and PR creation remain isolated to the Git PR role.",
        )

    project_result = await db.execute(select(Project).filter(Project.id == project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not project.path:
        raise HTTPException(status_code=422, detail="Project has no workspace path")

    repos = _discover_github_repos(project.path)
    if not repos:
        raise HTTPException(status_code=422, detail="No GitHub remotes found for project workspace")

    gh_present = _gh_available()
    github_author = author
    if gh_present and not github_author:
        github_author = _github_current_user(project.path)
    if not github_author:
        # Fall back to local git config user.name so the commit-only path still works.
        try:
            github_author = _run_project_command(["git", "config", "user.name"], project.path)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            github_author = None
    if not github_author:
        raise HTTPException(
            status_code=422,
            detail=("Could not determine GitHub user; pass ?author=<login>. "
                    "(`gh` is required for PR/issue/review sync; commit sync also works without it.)")
        )

    synced = 0
    seen = 0
    errors: list[str] = []

    async def _upsert(contribution_type: ContributionType, external_id: str, item: dict):
        nonlocal seen, synced
        seen += 1
        existing_result = await db.execute(
            select(UserContribution).filter(
                UserContribution.project_id == project_id,
                UserContribution.provider == "github",
                UserContribution.contribution_type == contribution_type,
                UserContribution.external_id == external_id,
            )
        )
        contribution = existing_result.scalar_one_or_none()
        if not contribution:
            contribution = UserContribution(
                project_id=project_id,
                entity_id=current_entity.id,
                contribution_type=contribution_type,
                provider="github",
                external_id=external_id,
            )
            db.add(contribution)
            synced += 1
        contribution.title = item.get("title") or external_id
        contribution.url = item.get("url")
        contribution.status = (item.get("state") or "").lower()
        contribution.created_at_external = _parse_github_datetime(item.get("createdAt"))
        contribution.updated_at_external = _parse_github_datetime(item.get("updatedAt"))
        contribution.recorded_at = datetime.utcnow()

    for repo in repos:
        if gh_present:
            for contribution_type, command in (
                (ContributionType.PULL_REQUEST, [
                    "gh", "pr", "list",
                    "--repo", repo,
                    "--author", github_author,
                    "--state", "all",
                    "--limit", "100",
                    "--json", "number,title,state,url,updatedAt,createdAt,headRefName,baseRefName",
                ]),
                (ContributionType.ISSUE, [
                    "gh", "issue", "list",
                    "--repo", repo,
                    "--author", github_author,
                    "--state", "all",
                    "--limit", "100",
                    "--json", "number,title,state,url,updatedAt,createdAt",
                ]),
            ):
                try:
                    output = _run_project_command(command, project.path)
                    items = json.loads(output or "[]")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
                    errors.append(f"{repo} {contribution_type.value}: {exc}")
                    continue
                for item in items:
                    await _upsert(contribution_type, f"{repo}#{item['number']}", item)

            # Reviews authored by the user across the repo's PRs.
            try:
                output = _run_project_command(
                    [
                        "gh", "search", "prs",
                        "--repo", repo,
                        "--reviewed-by", github_author,
                        "--limit", "100",
                        "--json", "number,title,state,url,updatedAt,createdAt",
                    ],
                    project.path,
                )
                review_prs = json.loads(output or "[]")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
                errors.append(f"{repo} review: {exc}")
                review_prs = []
            for pr in review_prs:
                external_id = f"{repo}#{pr['number']}/review:{github_author}"
                await _upsert(
                    ContributionType.REVIEW,
                    external_id,
                    {
                        "title": f"Reviewed PR #{pr['number']}: {pr.get('title', '')}",
                        "url": pr.get("url"),
                        "state": pr.get("state"),
                        "createdAt": pr.get("createdAt"),
                        "updatedAt": pr.get("updatedAt"),
                    },
                )

            # Commits authored by the user across the repo (gh search).
            try:
                output = _run_project_command(
                    [
                        "gh", "search", "commits",
                        "--repo", repo,
                        "--author", github_author,
                        "--limit", "100",
                        "--json", "sha,commit,url",
                    ],
                    project.path,
                )
                commits = json.loads(output or "[]")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
                errors.append(f"{repo} commit: {exc}")
                commits = []
            for entry in commits:
                sha = entry.get("sha") or ""
                if not sha:
                    continue
                commit_meta = entry.get("commit", {}) or {}
                message = commit_meta.get("message", "")
                first_line = message.splitlines()[0] if message else sha[:7]
                committed = (commit_meta.get("author") or {}).get("date")
                await _upsert(
                    ContributionType.COMMIT,
                    f"{repo}@{sha}",
                    {
                        "title": first_line,
                        "url": entry.get("url"),
                        "state": "committed",
                        "createdAt": committed,
                        "updatedAt": committed,
                    },
                )
        else:
            errors.append(f"{repo}: gh CLI not installed; PR/issue/review sync skipped")

        # Local-git commit fallback runs whether `gh` is present or not — it
        # captures commits that may not yet have been pushed.
        for entry in _git_local_commits(project.path, github_author, limit=50):
            sha = entry.get("sha") or ""
            if not sha:
                continue
            await _upsert(
                ContributionType.COMMIT,
                f"{repo}@{sha}",
                entry,
            )

    await db.commit()

    await event_bus.publish(
        EventType.USER_CONTRIBUTION_LOGGED.value,
        {
            "project_id": project_id,
            "provider": "github",
            "author": github_author,
            "repos": repos,
            "seen": seen,
            "created": synced,
        },
        project_id=project_id,
        entity_id=current_entity.id
    )

    return {
        "project_id": project_id,
        "author": github_author,
        "repos": repos,
        "seen": seen,
        "created": synced,
        "errors": errors,
    }


@router.post("/projects/{project_id}/contributions", response_model=UserContributionResponse, status_code=status.HTTP_201_CREATED)
async def log_project_contribution(
    project_id: int,
    contribution: UserContributionCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if contribution.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")
    if contribution.entity_id is None:
        contribution.entity_id = current_entity.id
    if not is_owner_or_manager(current_entity) and contribution.entity_id != current_entity.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only log your own contributions")

    db_contribution = UserContribution(**contribution.model_dump())
    db.add(db_contribution)
    await db.commit()
    await db.refresh(db_contribution)

    await event_bus.publish(
        EventType.USER_CONTRIBUTION_LOGGED.value,
        {
            "contribution_id": db_contribution.id,
            "project_id": project_id,
            "entity_id": db_contribution.entity_id,
            "contribution_type": db_contribution.contribution_type.value,
            "title": db_contribution.title,
            "url": db_contribution.url,
            "status": db_contribution.status,
        },
        project_id=project_id,
        entity_id=db_contribution.entity_id
    )
    return db_contribution


@router.get("/sessions/{session_id}/terminal", response_model=AgentTerminalResponse)
async def get_agent_terminal(
    session_id: int,
    limit: int = 200,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    activity_result = await db.execute(
        select(AgentActivity)
        .filter(AgentActivity.session_id == session_id)
        .order_by(AgentActivity.created_at.asc())
        .limit(limit)
    )
    return {"session": session, "activities": activity_result.scalars().all()}


@router.get("/tasks/{task_id}/active-session", response_model=Optional[AgentSessionResponse])
async def get_active_session_for_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return the most recent active agent session bound to a task, or null.

    Used by the kanban board to jump from a task card straight into the
    terminal tab for whichever role is currently executing it.
    """
    result = await db.execute(
        select(AgentSession)
        .filter(AgentSession.task_id == task_id, AgentSession.ended_at.is_(None))
        .order_by(desc(AgentSession.last_seen_at))
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/tasks/{task_id}/checkpoints", response_model=List[AgentCheckpointResponse])
async def get_task_checkpoints(
    task_id: int,
    agent_id: Optional[int] = None,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Return durable resume checkpoints for a task."""
    query = (
        select(AgentCheckpoint)
        .filter(AgentCheckpoint.task_id == task_id)
        .order_by(desc(AgentCheckpoint.updated_at))
    )
    if agent_id:
        query = query.filter(AgentCheckpoint.agent_id == agent_id)
    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.post("/tasks/{task_id}/checkpoints", response_model=AgentCheckpointResponse, status_code=status.HTTP_201_CREATED)
async def create_task_checkpoint(
    task_id: int,
    checkpoint: AgentCheckpointCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity),
):
    """Create or update a durable resume checkpoint for a task session."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if checkpoint.task_id != task_id:
        raise HTTPException(status_code=422, detail="Path task_id and body task_id must match")

    agent_id = checkpoint.agent_id or current_entity.id
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only checkpoint your own work")

    task_result = await db.execute(select(Task).filter(Task.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.project_id != checkpoint.project_id:
        raise HTTPException(status_code=422, detail="Checkpoint project_id does not match task")

    existing_result = await db.execute(
        select(AgentCheckpoint)
        .filter(
            AgentCheckpoint.task_id == task_id,
            AgentCheckpoint.agent_id == agent_id,
            AgentCheckpoint.session_id == checkpoint.session_id,
        )
        .order_by(desc(AgentCheckpoint.updated_at))
        .limit(1)
    )
    db_checkpoint = existing_result.scalar_one_or_none()
    now = datetime.utcnow()
    if db_checkpoint:
        db_checkpoint.workspace_path = checkpoint.workspace_path
        db_checkpoint.summary = checkpoint.summary
        db_checkpoint.terminal_tail = checkpoint.terminal_tail
        db_checkpoint.payload_json = checkpoint.payload_json
        db_checkpoint.updated_at = now
    else:
        db_checkpoint = AgentCheckpoint(
            agent_id=agent_id,
            project_id=checkpoint.project_id,
            task_id=task_id,
            session_id=checkpoint.session_id,
            workspace_path=checkpoint.workspace_path,
            summary=checkpoint.summary,
            terminal_tail=checkpoint.terminal_tail,
            payload_json=checkpoint.payload_json,
            created_at=now,
            updated_at=now,
        )
        db.add(db_checkpoint)

    await db.commit()
    await db.refresh(db_checkpoint)
    return db_checkpoint


@router.get("/projects/{project_id}/leases", response_model=List[TaskLeaseResponse])
async def get_project_leases(
    project_id: int,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db)
):
    now = datetime.utcnow()
    query = (
        select(TaskLease)
        .join(Task, Task.id == TaskLease.task_id)
        .filter(Task.project_id == project_id)
        .order_by(desc(TaskLease.created_at))
    )
    if active_only:
        query = query.filter(and_(TaskLease.status == LeaseStatus.ACTIVE, TaskLease.expires_at > now))
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/tasks/{task_id}/lease", response_model=TaskLeaseResponse, status_code=status.HTTP_201_CREATED)
async def claim_task_lease(
    task_id: int,
    lease: TaskLeaseCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    agent_id = lease.agent_id or current_entity.id
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only claim your own lease")
    if lease.task_id != task_id:
        raise HTTPException(status_code=422, detail="Path task_id and body task_id must match")

    task_result = await db.execute(select(Task).filter(Task.id == task_id))
    task = task_result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    now = datetime.utcnow()
    active_result = await db.execute(
        select(TaskLease).filter(
            TaskLease.task_id == task_id,
            TaskLease.status == LeaseStatus.ACTIVE,
            TaskLease.expires_at > now,
            TaskLease.agent_id != agent_id,
        )
    )
    active = active_result.scalar_one_or_none()
    if active:
        raise HTTPException(status_code=409, detail=f"Task is already leased by agent {active.agent_id}")

    # Release this agent's older leases on the same task.
    existing_result = await db.execute(
        select(TaskLease).filter(TaskLease.task_id == task_id, TaskLease.agent_id == agent_id, TaskLease.status == LeaseStatus.ACTIVE)
    )
    for existing in existing_result.scalars().all():
        existing.status = LeaseStatus.RELEASED
        existing.released_at = now

    db_lease = TaskLease(
        task_id=task_id,
        agent_id=agent_id,
        session_id=lease.session_id,
        status=LeaseStatus.ACTIVE,
        expires_at=now + timedelta(seconds=max(60, lease.ttl_seconds)),
    )
    db.add(db_lease)
    await db.commit()
    await db.refresh(db_lease)

    await event_bus.publish(
        EventType.TASK_LEASE_UPDATED.value,
        {
            "lease_id": db_lease.id,
            "task_id": task_id,
            "agent_id": agent_id,
            "session_id": lease.session_id,
            "status": db_lease.status.value,
            "expires_at": db_lease.expires_at.isoformat(),
        },
        project_id=task.project_id,
        entity_id=agent_id
    )
    return db_lease


@router.patch("/leases/{lease_id}/release", response_model=TaskLeaseResponse)
async def release_task_lease(
    lease_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    result = await db.execute(select(TaskLease).filter(TaskLease.id == lease_id).options(selectinload(TaskLease.task)))
    lease = result.scalar_one_or_none()
    if not lease:
        raise HTTPException(status_code=404, detail="Lease not found")
    if not is_owner_or_manager(current_entity) and current_entity.id != lease.agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only release your own lease")

    lease.status = LeaseStatus.RELEASED
    lease.released_at = datetime.utcnow()
    await db.commit()
    await db.refresh(lease)

    await event_bus.publish(
        EventType.TASK_LEASE_UPDATED.value,
        {"lease_id": lease.id, "task_id": lease.task_id, "agent_id": lease.agent_id, "status": lease.status.value},
        project_id=lease.task.project_id if lease.task else None,
        entity_id=lease.agent_id
    )
    return lease


@router.get("/status", response_model=List[AgentHeartbeatResponse])
async def get_agent_statuses(
    db: AsyncSession = Depends(get_db)
):
    """Get all current agent heartbeats."""
    result = await db.execute(
        select(AgentHeartbeat)
        .options(selectinload(AgentHeartbeat.agent))
        .filter(AgentHeartbeat.status_type != AgentStatusType.IDLE)
        .order_by(desc(AgentHeartbeat.updated_at))
    )
    return result.scalars().all()


@router.get("/activity", response_model=List[AgentActivityResponse])
async def get_activity_feed(
    agent_id: Optional[int] = None,
    project_id: Optional[int] = None,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get recent agent activity feed, optionally filtered."""
    query = select(AgentActivity).order_by(desc(AgentActivity.created_at))

    if agent_id:
        query = query.filter(AgentActivity.agent_id == agent_id)
    if project_id:
        query = query.filter(AgentActivity.project_id == project_id)
    if session_id:
        query = query.filter(AgentActivity.session_id == session_id)
    if task_id:
        query = query.filter(AgentActivity.task_id == task_id)

    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.get("/sessions", response_model=List[AgentSessionResponse])
async def get_agent_sessions(
    agent_id: Optional[int] = None,
    project_id: Optional[int] = None,
    task_id: Optional[int] = None,
    active_only: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    """Get agent CLI sessions, optionally scoped to a project or task."""
    query = select(AgentSession).order_by(desc(AgentSession.last_seen_at))
    if agent_id:
        query = query.filter(AgentSession.agent_id == agent_id)
    if project_id:
        query = query.filter(AgentSession.project_id == project_id)
    if task_id:
        query = query.filter(AgentSession.task_id == task_id)
    if active_only:
        query = query.filter(AgentSession.ended_at.is_(None))

    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.post("/{agent_id}/sessions", response_model=AgentSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_agent_session(
    agent_id: int,
    session: AgentSessionCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Start a durable visibility session for a CLI agent run."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only start your own session")

    agent_result = await db.execute(select(Entity).filter(Entity.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    project_result = await db.execute(select(Project).filter(Project.id == session.project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    workspace_path = session.workspace_path or project.path
    if not workspace_path:
        raise HTTPException(status_code=422, detail="workspace_path is required when project has no path")

    db_session = AgentSession(
        agent_id=agent_id,
        project_id=session.project_id,
        task_id=session.task_id,
        workspace_path=workspace_path,
        command=session.command,
        model=session.model,
        mode=session.mode,
        status=AgentSessionStatus.ACTIVE,
    )
    db.add(db_session)
    await db.commit()
    await db.refresh(db_session)

    await event_bus.publish(
        EventType.AGENT_ACTIVITY_LOGGED.value,
        {
            "agent_id": agent_id,
            "session_id": db_session.id,
            "project_id": db_session.project_id,
            "task_id": db_session.task_id,
            "activity_type": "session_started",
            "message": f"Session started in {db_session.workspace_path}",
            "workspace_path": db_session.workspace_path,
        },
        project_id=db_session.project_id,
        entity_id=agent_id
    )

    return db_session


@router.patch("/sessions/{session_id}", response_model=AgentSessionResponse)
async def update_agent_session(
    session_id: int,
    update: AgentSessionUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Update or end a durable CLI-agent session."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
    db_session = result.scalar_one_or_none()
    if not db_session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not is_owner_or_manager(current_entity) and current_entity.id != db_session.agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own session")

    db_session.status = update.status
    if update.task_id is not None:
        db_session.task_id = update.task_id
    db_session.last_seen_at = datetime.utcnow()
    if update.status in (AgentSessionStatus.DONE, AgentSessionStatus.ERROR):
        db_session.ended_at = datetime.utcnow()

    await db.commit()
    await db.refresh(db_session)

    if update.message:
        activity = AgentActivity(
            agent_id=db_session.agent_id,
            session_id=db_session.id,
            project_id=db_session.project_id,
            task_id=db_session.task_id,
            activity_type=ActivityType.RESULT if update.status == AgentSessionStatus.DONE else ActivityType.ACTION,
            source="session_update",
            message=update.message,
            workspace_path=db_session.workspace_path,
        )
        db.add(activity)
        await db.commit()

    await event_bus.publish(
        EventType.AGENT_STATUS_UPDATED.value,
        {
            "agent_id": db_session.agent_id,
            "session_id": db_session.id,
            "project_id": db_session.project_id,
            "task_id": db_session.task_id,
            "status_type": update.status.value,
            "message": update.message,
            "workspace_path": db_session.workspace_path,
        },
        project_id=db_session.project_id,
        entity_id=db_session.agent_id
    )

    return db_session


@router.post("/{agent_id}/status", response_model=AgentHeartbeatResponse)
async def update_agent_status(
    agent_id: int,
    update: AgentStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Update an agent's heartbeat status. Called by agents or server fallback."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only update your own status")
    # Verify the agent exists
    result = await db.execute(select(Entity).filter(Entity.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Find existing heartbeat or create new
    result = await db.execute(
        select(AgentHeartbeat).filter(AgentHeartbeat.agent_id == agent_id)
    )
    heartbeat = result.scalar_one_or_none()

    if heartbeat:
        heartbeat.status_type = update.status_type
        heartbeat.message = update.message
        heartbeat.task_id = update.task_id
        heartbeat.updated_at = datetime.utcnow()
    else:
        heartbeat = AgentHeartbeat(
            agent_id=agent_id,
            status_type=update.status_type,
            message=update.message,
            task_id=update.task_id
        )
        db.add(heartbeat)

    await db.commit()
    await db.refresh(heartbeat)

    await event_bus.publish(
        EventType.AGENT_STATUS_UPDATED.value,
        {
            "agent_id": agent_id,
            "status_type": update.status_type.value,
            "message": update.message,
            "task_id": update.task_id
        },
        entity_id=agent_id
    )

    return heartbeat


@router.post("/{agent_id}/activity", response_model=AgentActivityResponse, status_code=status.HTTP_201_CREATED)
async def log_agent_activity(
    agent_id: int,
    activity: AgentActivityCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Log an activity entry for an agent."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only log your own activity")
    result = await db.execute(select(Entity).filter(Entity.id == agent_id))
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    db_activity = AgentActivity(
        agent_id=agent_id,
        session_id=activity.session_id,
        project_id=activity.project_id,
        task_id=activity.task_id,
        activity_type=activity.activity_type,
        source=activity.source,
        message=activity.message,
        payload_json=activity.payload_json,
        workspace_path=activity.workspace_path,
        file_path=activity.file_path,
        command=activity.command,
    )
    db.add(db_activity)
    await db.commit()
    await db.refresh(db_activity)

    # Derive project_id from task_id for per-project filtering
    project_id = activity.project_id
    if activity.task_id:
        task_result = await db.execute(select(Task).filter(Task.id == activity.task_id))
        task = task_result.scalar_one_or_none()
        if task:
            project_id = task.project_id
            if db_activity.project_id is None:
                db_activity.project_id = project_id
                await db.commit()

    await event_bus.publish(
        EventType.AGENT_ACTIVITY_LOGGED.value,
        {
            "agent_id": agent_id,
            "activity_id": db_activity.id,
            "session_id": db_activity.session_id,
            "project_id": project_id,
            "activity_type": activity.activity_type.value,
            "message": activity.message,
            "task_id": activity.task_id,
            "source": activity.source,
            "workspace_path": activity.workspace_path,
            "file_path": activity.file_path,
            "command": activity.command,
        },
        project_id=project_id,
        entity_id=agent_id
    )

    return db_activity


@router.get("/projects/{project_id}/diff-reviews", response_model=List[DiffReviewResponse])
async def get_project_diff_reviews(
    project_id: int,
    status: Optional[str] = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db)
):
    query = (
        select(DiffReview)
        .filter(DiffReview.project_id == project_id)
        .order_by(desc(DiffReview.created_at))
        .limit(limit)
    )
    if status:
        query = query.filter(DiffReview.status == status)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/projects/{project_id}/diff-reviews", response_model=DiffReviewResponse, status_code=status.HTTP_201_CREATED)
async def create_diff_review(
    project_id: int,
    review: DiffReviewCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if review.project_id != project_id:
        raise HTTPException(status_code=422, detail="Path project_id and body project_id must match")

    db_review = DiffReview(
        project_id=project_id,
        task_id=review.task_id,
        reviewer_id=review.reviewer_id or current_entity.id,
        requester_id=review.requester_id or current_entity.id,
        diff_content=review.diff_content,
        summary=review.summary,
        file_paths=review.file_paths,
        is_critical=review.is_critical,
        status=DiffReviewStatus.PENDING,
    )
    db.add(db_review)
    await db.commit()
    await db.refresh(db_review)

    await event_bus.publish(
        EventType.DIFF_REVIEW_REQUESTED.value,
        {
            "review_id": db_review.id,
            "project_id": project_id,
            "task_id": review.task_id,
            "requester_id": current_entity.id,
            "is_critical": review.is_critical,
        },
        project_id=project_id,
        entity_id=current_entity.id
    )
    return db_review


@router.patch("/diff-reviews/{review_id}", response_model=DiffReviewResponse)
async def update_diff_review(
    review_id: int,
    update: DiffReviewUpdate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    result = await db.execute(select(DiffReview).filter(DiffReview.id == review_id))
    review = result.scalar_one_or_none()
    if not review:
        raise HTTPException(status_code=404, detail="Diff review not found")

    review.status = update.status
    review.review_notes = update.review_notes
    if update.status in (DiffReviewStatus.APPROVED, DiffReviewStatus.REJECTED, DiffReviewStatus.CHANGES_REQUESTED):
        review.reviewer_id = current_entity.id
        review.reviewed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(review)

    await event_bus.publish(
        EventType.DIFF_REVIEW_COMPLETED.value,
        {
            "review_id": review.id,
            "project_id": review.project_id,
            "status": review.status.value,
            "reviewer_id": current_entity.id,
        },
        project_id=review.project_id,
        entity_id=current_entity.id
    )
    return review


# ---------------------------------------------------------------------------
# Approval Queue
# ---------------------------------------------------------------------------


@router.post("/approvals", response_model=AgentApprovalResponse, status_code=status.HTTP_201_CREATED)
async def request_agent_approval(
    payload: AgentApprovalCreate,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Create an approval request from a CLI agent or supervisor."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    project_result = await db.execute(select(Project).filter(Project.id == payload.project_id))
    project = project_result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    agent_id = payload.agent_id or current_entity.id
    if not is_owner_or_manager(current_entity) and current_entity.id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only request approval for yourself"
        )

    approval = AgentApproval(
        project_id=payload.project_id,
        task_id=payload.task_id,
        session_id=payload.session_id,
        agent_id=agent_id,
        approval_type=payload.approval_type,
        title=payload.title,
        message=payload.message,
        command=payload.command,
        diff_content=payload.diff_content,
        payload_json=payload.payload_json,
        status=AgentApprovalStatus.PENDING,
    )
    db.add(approval)
    await db.commit()
    await db.refresh(approval)

    if payload.session_id:
        sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == payload.session_id))
        session_row = sess_result.scalar_one_or_none()
        if session_row and session_row.status != AgentSessionStatus.BLOCKED:
            session_row.status = AgentSessionStatus.BLOCKED
            session_row.last_seen_at = datetime.utcnow()
            await db.commit()

    await event_bus.publish(
        EventType.AGENT_APPROVAL_REQUESTED.value,
        {
            "approval_id": approval.id,
            "project_id": approval.project_id,
            "task_id": approval.task_id,
            "session_id": approval.session_id,
            "agent_id": approval.agent_id,
            "approval_type": approval.approval_type.value,
            "title": approval.title,
            "message": approval.message,
            "command": approval.command,
        },
        project_id=approval.project_id,
        entity_id=approval.agent_id
    )
    return approval


@router.get("/approvals", response_model=List[AgentApprovalResponse])
async def list_agent_approvals(
    request: Request,
    project_id: Optional[int] = None,
    agent_id: Optional[int] = None,
    task_id: Optional[int] = None,
    session_id: Optional[int] = None,
    status_filter: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """List approval queue entries. Use status_filter=pending for the workbench tab."""
    has_explicit_identity = bool(request.headers.get("x-entity-id"))
    if not current_entity or not has_explicit_identity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    entity_id_header = request.headers.get("x-entity-id")
    if entity_id_header:
        try:
            if int(entity_id_header) != current_entity.id:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid identity")
        except ValueError:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid identity")

    query = select(AgentApproval).order_by(desc(AgentApproval.requested_at))
    if project_id is not None:
        query = query.filter(AgentApproval.project_id == project_id)
    if agent_id is not None:
        query = query.filter(AgentApproval.agent_id == agent_id)
    if task_id is not None:
        query = query.filter(AgentApproval.task_id == task_id)
    if session_id is not None:
        query = query.filter(AgentApproval.session_id == session_id)
    if status_filter:
        try:
            query = query.filter(AgentApproval.status == AgentApprovalStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid status: {status_filter}")

    if not is_owner_or_manager(current_entity):
        query = query.filter(AgentApproval.agent_id == current_entity.id)

    result = await db.execute(query.limit(limit))
    return result.scalars().all()


@router.patch("/approvals/{approval_id}/resolve", response_model=AgentApprovalResponse)
async def resolve_agent_approval(
    approval_id: int,
    payload: AgentApprovalResolve,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Approve, reject, or cancel a pending approval request."""
    if not current_entity:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if payload.decision not in (
        AgentApprovalStatus.APPROVED,
        AgentApprovalStatus.REJECTED,
        AgentApprovalStatus.CANCELLED,
    ):
        raise HTTPException(status_code=422, detail="decision must be approved, rejected, or cancelled")

    result = await db.execute(select(AgentApproval).filter(AgentApproval.id == approval_id))
    approval = result.scalar_one_or_none()
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != AgentApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail=f"Approval already {approval.status.value}")

    if payload.decision == AgentApprovalStatus.CANCELLED:
        if not is_owner_or_manager(current_entity) and current_entity.id != approval.agent_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the requester or a manager can cancel")
    else:
        if not is_owner_or_manager(current_entity):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only managers can approve or reject approvals")

    approval.status = payload.decision
    approval.resolved_at = datetime.utcnow()
    approval.resolved_by_entity_id = current_entity.id
    approval.response_message = payload.response_message

    if approval.session_id:
        sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == approval.session_id))
        session_row = sess_result.scalar_one_or_none()
        if session_row and session_row.status == AgentSessionStatus.BLOCKED:
            session_row.status = AgentSessionStatus.ACTIVE
            session_row.last_seen_at = datetime.utcnow()

    await db.commit()
    await db.refresh(approval)

    await event_bus.publish(
        EventType.AGENT_APPROVAL_RESOLVED.value,
        {
            "approval_id": approval.id,
            "project_id": approval.project_id,
            "task_id": approval.task_id,
            "session_id": approval.session_id,
            "agent_id": approval.agent_id,
            "approval_type": approval.approval_type.value,
            "status": approval.status.value,
            "resolved_by_entity_id": current_entity.id,
            "response_message": approval.response_message,
        },
        project_id=approval.project_id,
        entity_id=approval.agent_id
    )
    return approval
