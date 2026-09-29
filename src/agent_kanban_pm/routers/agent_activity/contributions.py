from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List, Optional
from datetime import UTC, datetime
import asyncio
import logging
import json
import subprocess

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Entity, EntityType, Project, UserContribution,
    ContributionType,
)
from agent_kanban_pm.schemas import (
    UserContributionCreate, UserContributionResponse,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)
router = APIRouter()


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


def _git_config_value(workspace_path: str, key: str) -> Optional[str]:
    try:
        value = _run_project_command(["git", "config", key], workspace_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return value or None


def _gh_available() -> bool:
    import shutil as _shutil
    return _shutil.which("gh") is not None


def _gh_authenticated(workspace_path: str) -> bool:
    try:
        _run_project_command(["gh", "auth", "status", "--hostname", "github.com"], workspace_path)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


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
    from agent_kanban_pm.runtime.preferences import load_preferences
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

    # End the request's read transaction before running git or gh. Networked
    # gh calls can take up to 30 seconds and must not block the event loop.
    await db.commit()
    repos = await asyncio.to_thread(_discover_github_repos, project.path)
    if not repos:
        raise HTTPException(status_code=422, detail="No GitHub remotes found for project workspace")

    local_name, local_email, gh_present = await asyncio.gather(
        asyncio.to_thread(_git_config_value, project.path, "user.name"),
        asyncio.to_thread(_git_config_value, project.path, "user.email"),
        asyncio.to_thread(_gh_available),
    )
    local_author = local_name or local_email
    gh_authenticated = (
        await asyncio.to_thread(_gh_authenticated, project.path)
        if gh_present else False
    )
    github_author = author or (
        await asyncio.to_thread(_github_current_user, project.path)
        if gh_authenticated else None
    )
    commit_author = local_author or github_author
    if not commit_author and not github_author:
        raise HTTPException(
            status_code=422,
            detail=("Could not determine local git author from git config user.name or user.email. "
                    "Pass ?author=<login> for GitHub enrichment, or configure local git identity.")
        )

    synced = 0
    seen = 0
    errors: list[str] = []
    if gh_present and not gh_authenticated:
        errors.append("gh CLI is installed but not authenticated; PR/issue/review sync skipped and local git commits were synced instead")
    if gh_authenticated and not github_author:
        errors.append("Could not determine current GitHub user from gh; PR/issue/review sync skipped")

    pending_contributions: list[tuple[ContributionType, str, dict]] = []

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
        contribution.recorded_at = datetime.now(UTC)

    for repo in repos:
        if commit_author:
            # Local git is the baseline path and follows the same identity/config
            # users rely on for ordinary git operations.
            local_commits = await asyncio.to_thread(
                _git_local_commits, project.path, commit_author, 50
            )
            for entry in local_commits:
                sha = entry.get("sha") or ""
                if not sha:
                    continue
                pending_contributions.append((
                    ContributionType.COMMIT,
                    f"{repo}@{sha}",
                    entry,
                ))

        if gh_authenticated and github_author:
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
                    output = await asyncio.to_thread(_run_project_command, command, project.path)
                    items = json.loads(output or "[]")
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as exc:
                    errors.append(f"{repo} {contribution_type.value}: {exc}")
                    continue
                for item in items:
                    pending_contributions.append((contribution_type, f"{repo}#{item['number']}", item))

            # Reviews authored by the user across the repo's PRs.
            try:
                output = await asyncio.to_thread(
                    _run_project_command,
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
                pending_contributions.append((
                    ContributionType.REVIEW,
                    external_id,
                    {
                        "title": f"Reviewed PR #{pr['number']}: {pr.get('title', '')}",
                        "url": pr.get("url"),
                        "state": pr.get("state"),
                        "createdAt": pr.get("createdAt"),
                        "updatedAt": pr.get("updatedAt"),
                    },
                ))

            # Commits authored by the user across the repo (gh search).
            try:
                output = await asyncio.to_thread(
                    _run_project_command,
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
                pending_contributions.append((
                    ContributionType.COMMIT,
                    f"{repo}@{sha}",
                    {
                        "title": first_line,
                        "url": entry.get("url"),
                        "state": "committed",
                        "createdAt": committed,
                        "updatedAt": committed,
                    },
                ))
        elif not gh_present:
            errors.append(f"{repo}: gh CLI not installed; PR/issue/review sync skipped")

    for contribution_type, external_id, item in pending_contributions:
        await _upsert(contribution_type, external_id, item)

    event_bus.enqueue(db,
        EventType.USER_CONTRIBUTION_LOGGED.value,
        {
            "project_id": project_id,
            "provider": "github",
            "author": commit_author or github_author,
            "repos": repos,
            "seen": seen,
            "created": synced,
        },
        project_id=project_id,
        entity_id=current_entity.id
    )
    await db.commit()

    return {
        "project_id": project_id,
        "author": commit_author or github_author,
        "github_author": github_author,
        "local_author": local_author,
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
    await db.flush()
    event_bus.enqueue(db,
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
    await db.commit()
    await db.refresh(db_contribution)
    return db_contribution
