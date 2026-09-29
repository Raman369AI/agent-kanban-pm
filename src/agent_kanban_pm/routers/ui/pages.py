from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import case, select, func
from sqlalchemy.orm import selectinload
from typing import Optional
from collections import Counter
import json

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    Project, Task, Entity, Stage, EntityType, TaskStatus, ApprovalStatus,
    AgentSession, AgentSessionStatus, AgentApproval, AgentApprovalStatus,
    AgentActivity, LaunchRequest, StagePolicy,
)
from agent_kanban_pm.auth import get_current_entity
from agent_kanban_pm.runtime.stage_identity import STAGE_STATUSES
from ._common import (
    templates,
    ACTIVITY_LABELS,
    MEANINGFUL_ACTIVITY_TYPES,
    _activity_preview,
    _utc_iso,
)
from . import roles

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """Dashboard page"""
    projects_result = await db.execute(select(Project))
    visible_projects = [
        p for p in projects_result.scalars().all()
        if p.approval_status != ApprovalStatus.REJECTED
    ]
    total_tasks = await db.execute(select(func.count(Task.id)))
    completed_tasks = await db.execute(select(func.count(Task.id)).where(Task.status == TaskStatus.COMPLETED))
    role_payload = await roles._role_assignment_payload()

    stats = {
        "total_projects": len(visible_projects),
        "total_tasks": total_tasks.scalar(),
        "completed_tasks": completed_tasks.scalar(),
        "total_entities": len(role_payload["roles"])
    }

    recent_projects = sorted(visible_projects, key=lambda p: p.created_at, reverse=True)[:5]

    for project in recent_projects:
        task_count_result = await db.execute(
            select(func.count(Task.id)).where(Task.project_id == project.id)
        )
        project.task_count = task_count_result.scalar()

    result = await db.execute(
        select(Task).order_by(Task.created_at.desc()).limit(6)
    )
    recent_tasks = result.scalars().all()

    visible_ids = {project.id for project in visible_projects}
    task_rows = (await db.execute(select(Task).where(Task.project_id.in_(visible_ids)))).scalars().all() if visible_ids else []
    tasks_by_id = {task.id: task for task in task_rows}
    approval_rows = (await db.execute(
        select(AgentApproval).where(
            AgentApproval.project_id.in_(visible_ids),
            AgentApproval.status == AgentApprovalStatus.PENDING,
        ).order_by(AgentApproval.requested_at.desc())
    )).scalars().all() if visible_ids else []
    session_rows = (await db.execute(
        select(AgentSession).where(
            AgentSession.project_id.in_(visible_ids),
            AgentSession.task_id.is_not(None),
        ).order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
    )).scalars().all() if visible_ids else []
    latest_sessions = {}
    for session in session_rows:
        latest_sessions.setdefault(session.task_id, session)
    attention = []
    for approval in approval_rows:
        task = tasks_by_id.get(approval.task_id)
        if task:
            attention.append({"kind": "Approval", "title": approval.title,
                              "task": task, "detail": "Agent is waiting for a decision"})
    for task in task_rows:
        latest = latest_sessions.get(task.id)
        if latest and latest.status == AgentSessionStatus.ERROR:
            attention.append({"kind": "Failed session", "title": task.title,
                              "task": task, "detail": "Inspect the failed run"})
    for task in task_rows:
        if task.status == TaskStatus.IN_REVIEW:
            attention.append({"kind": "Ready for review", "title": task.title,
                              "task": task, "detail": "Review the result"})

    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "stats": stats,
        "recent_projects": recent_projects,
        "recent_tasks": recent_tasks,
        "attention": attention[:12],
    })


@router.get("/ui/projects", response_class=HTMLResponse)
async def ui_projects(
    request: Request,
    all: bool = False,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Projects list page"""
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.stages), selectinload(Project.tasks))
        .order_by(Project.created_at.desc())
    )
    projects = result.scalars().all()
    if not all:
        projects = [
            p for p in projects
            if p.approval_status != ApprovalStatus.REJECTED
        ]

    project_ids = {project.id for project in projects}
    pending_rows = (await db.execute(select(AgentApproval.project_id).where(
        AgentApproval.project_id.in_(project_ids),
        AgentApproval.status == AgentApprovalStatus.PENDING,
    ))).scalars().all() if project_ids else []
    pending_counts = Counter(pending_rows)
    session_rows = (await db.execute(select(AgentSession).where(
        AgentSession.project_id.in_(project_ids), AgentSession.task_id.is_not(None)
    ).order_by(AgentSession.started_at.desc(), AgentSession.id.desc()))).scalars().all() if project_ids else []
    latest_sessions = {}
    for session in session_rows:
        latest_sessions.setdefault(session.task_id, session)
    for project in projects:
        project.ui_total = len(project.tasks)
        project.ui_completed = sum(task.status == TaskStatus.COMPLETED for task in project.tasks)
        project.ui_blocked = sum(
            task.status == TaskStatus.BLOCKED or
            (latest_sessions.get(task.id) is not None and
             latest_sessions[task.id].status in (AgentSessionStatus.BLOCKED, AgentSessionStatus.ERROR))
            for task in project.tasks
        )
        project.ui_attention = project.ui_blocked + pending_counts.get(project.id, 0) + sum(
            task.status == TaskStatus.IN_REVIEW for task in project.tasks
        )

    agents_result = await db.execute(
        select(Entity).filter(Entity.entity_type == EntityType.AGENT, Entity.is_active == True)
    )
    agents = agents_result.scalars().all()

    return templates.TemplateResponse(request, "projects.html", {
        "request": request,
        "projects": projects,
        "agents": agents,
        "current_entity": current_entity,
    })


@router.get("/ui/projects/{project_id}/board", response_class=HTMLResponse)
async def project_kanban_board(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    """Kanban board for a project"""
    result = await db.execute(
        select(Project)
        .filter(Project.id == project_id)
        .options(
            selectinload(Project.stages).selectinload(Stage.tasks).selectinload(Task.assignees),
            selectinload(Project.stages).selectinload(Stage.tasks).selectinload(Task.subtasks),
            selectinload(Project.stages).selectinload(Stage.tasks).selectinload(Task.comments),
            selectinload(Project.creator)
        )
    )
    project = result.scalar_one_or_none()

    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # A worker is ready only when the configured worker CLI is available.
    # Task stage alone cannot prove execution: use durable agent sessions.
    role_data = await roles._role_assignment_payload()
    worker_role = next((role for role in role_data["roles"] if role["role"] == "worker"), None)
    git_pr_role = next((role for role in role_data["roles"] if role["role"] == "git_pr"), None)
    has_folder = bool(project.path and project.path.strip())
    has_worker = bool(worker_role and worker_role["installed"])

    all_tasks = [task for stage in project.stages for task in stage.tasks]
    latest_activity_by_task = {}
    task_ids = [task.id for task in all_tasks]
    if task_ids:
        # Prefer the latest high-signal activity. Thought/observation entries
        # are retained as a fallback when a task has no more useful event.
        activity_priority = case(
            (AgentActivity.activity_type.in_(MEANINGFUL_ACTIVITY_TYPES), 0),
            else_=1,
        )
        ranked_activity = (
            select(
                AgentActivity.id.label("activity_id"),
                AgentActivity.task_id,
                AgentActivity.agent_id,
                Entity.name.label("agent_name"),
                AgentActivity.activity_type,
                AgentActivity.message,
                AgentActivity.created_at,
                func.row_number().over(
                    partition_by=AgentActivity.task_id,
                    order_by=(activity_priority, AgentActivity.id.desc()),
                ).label("activity_rank"),
            )
            .join(Entity, Entity.id == AgentActivity.agent_id)
            .where(
                AgentActivity.project_id == project_id,
                AgentActivity.task_id.in_(task_ids),
            )
            .subquery()
        )
        activity_result = await db.execute(
            select(ranked_activity).where(ranked_activity.c.activity_rank == 1)
        )
        for row in activity_result.mappings():
            activity_type = getattr(row["activity_type"], "value", row["activity_type"])
            latest_activity_by_task[row["task_id"]] = {
                "id": row["activity_id"],
                "agent_id": row["agent_id"],
                "agent_name": row["agent_name"],
                "type": activity_type,
                "type_label": ACTIVITY_LABELS.get(activity_type, str(activity_type).replace("_", " ").title()),
                "message": _activity_preview(row["message"]) or "Activity recorded",
                "created_at": _utc_iso(row["created_at"]),
            }
    session_result = await db.execute(
        select(AgentSession)
        .where(AgentSession.project_id == project_id, AgentSession.task_id.is_not(None))
        .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
    )
    sessions = session_result.scalars().all()
    latest_session_by_task = {}
    for session in sessions:
        latest_session_by_task.setdefault(session.task_id, session.status.value)
    implementation_session_by_task = {}
    for session in sessions:
        assigned_role = (session.assigned_role or "worker").strip().lower()
        if (
            session.status == AgentSessionStatus.DONE
            and session.handoff_received_at is not None
            and assigned_role not in {"test", "diff_review", "git_pr"}
        ):
            implementation_session_by_task.setdefault(session.task_id, session)
    open_launch_by_task = {}
    if task_ids:
        launch_result = await db.execute(
            select(LaunchRequest.task_id, LaunchRequest.status)
            .where(
                LaunchRequest.task_id.in_(task_ids),
                LaunchRequest.archived_at.is_(None),
                LaunchRequest.status.in_(("queued", "blocked", "reserved", "starting", "started")),
            )
            .order_by(LaunchRequest.id.desc())
        )
        for task_id, launch_status in launch_result:
            open_launch_by_task.setdefault(task_id, launch_status)

    review_task_ids = [
        task.id for stage in project.stages if stage.key == "review" for task in stage.tasks
    ]
    latest_git_launch_by_task = {}
    if review_task_ids:
        ranked_git_launch = (
            select(
                LaunchRequest.id.label("request_id"),
                LaunchRequest.task_id,
                LaunchRequest.status,
                LaunchRequest.source_session_id,
                LaunchRequest.session_id,
                func.row_number().over(
                    partition_by=LaunchRequest.task_id,
                    order_by=LaunchRequest.id.desc(),
                ).label("request_rank"),
            )
            .where(
                LaunchRequest.task_id.in_(review_task_ids),
                LaunchRequest.role == "git_pr",
                LaunchRequest.archived_at.is_(None),
            )
            .subquery()
        )
        git_launch_result = await db.execute(
            select(ranked_git_launch).where(ranked_git_launch.c.request_rank == 1)
        )
        for row in git_launch_result.mappings():
            latest_git_launch_by_task[row["task_id"]] = dict(row)

    active_session_by_task = {}
    for session in sessions:
        if session.task_id and session.ended_at is None:
            active_session_by_task.setdefault(session.task_id, session.status.value)

    todo_stage_id = next((stage.id for stage in project.stages if stage.key == "to_do"), None)
    start_state_by_task = {}
    for stage in project.stages:
        if stage.key != "backlog":
            continue
        for task in stage.tasks:
            active_agents = [
                assignee for assignee in task.assignees
                if assignee.entity_type == EntityType.AGENT and assignee.is_active
            ]
            if todo_stage_id is None:
                state = {"state": "unavailable", "label": "To Do unavailable"}
            elif task.id in active_session_by_task:
                state = {"state": "running", "label": "Agent active"}
            elif task.id in open_launch_by_task:
                launch_status = open_launch_by_task[task.id]
                state = {
                    "state": "queued",
                    "label": "Launch blocked" if launch_status == "blocked" else "Launch queued",
                }
            elif len(active_agents) == 1:
                state = {
                    "state": "eligible",
                    "label": "Start",
                    "agent_id": active_agents[0].id,
                    "agent_name": active_agents[0].name,
                }
            elif not active_agents:
                state = {"state": "needs_assignment", "label": "Assign agent"}
            else:
                state = {"state": "choose_agent", "label": "Choose agent"}
            start_state_by_task[task.id] = state

    review_stage_ids = [stage.id for stage in project.stages if stage.key == "review"]
    review_policy_roles = {}
    if review_stage_ids:
        policy_result = await db.execute(
            select(StagePolicy).where(
                StagePolicy.project_id == project_id,
                StagePolicy.stage_id.in_(review_stage_ids),
            )
        )
        for policy in policy_result.scalars():
            review_policy_roles[policy.stage_id] = set(
                json.loads(policy.on_enter_roles_json or "[]")
            )

    git_pr_state_by_task = {}
    for stage in project.stages:
        if stage.key != "review":
            continue
        policy_queues_git_pr = "git_pr" in review_policy_roles.get(stage.id, set())
        for task in stage.tasks:
            source = implementation_session_by_task.get(task.id)
            current_git_session = next((
                session for session in sessions
                if session.task_id == task.id
                and (session.assigned_role or "").strip().lower() == "git_pr"
                and source is not None
                and session.source_session_id == source.id
                and session.work_revision == source.work_revision
            ), None)
            latest_request = latest_git_launch_by_task.get(task.id)
            if latest_request and source is not None:
                if latest_request["source_session_id"] != source.id:
                    latest_request = None

            if current_git_session and current_git_session.ended_at is None:
                session_status = current_git_session.status.value
                label = "Git/PR blocked" if session_status == "blocked" else "Git/PR active"
                state = {"state": session_status, "label": label}
            elif current_git_session and current_git_session.status == AgentSessionStatus.DONE:
                state = {"state": "completed", "label": "Git/PR complete"}
            elif latest_request and latest_request["status"] in {
                "queued", "blocked", "reserved", "starting", "started",
            }:
                request_status = latest_request["status"]
                state = {
                    "state": request_status,
                    "label": "Git/PR blocked" if request_status == "blocked" else "Git/PR queued",
                }
            elif source is None:
                state = {"state": "waiting", "label": "Waiting for implementation"}
            elif not git_pr_role:
                state = {"state": "configure", "label": "Configure Git/PR"}
            elif not git_pr_role["installed"]:
                state = {"state": "unavailable", "label": "Git/PR unavailable"}
            elif policy_queues_git_pr and not latest_request and not current_git_session:
                state = {"state": "policy_queued", "label": "Git/PR queued by policy"}
            else:
                state = {"state": "request", "label": "Request Git/PR"}
            git_pr_state_by_task[task.id] = state
    approvals_result = await db.execute(
        select(AgentApproval.task_id).where(
            AgentApproval.project_id == project_id,
            AgentApproval.status == AgentApprovalStatus.PENDING,
            AgentApproval.task_id.is_not(None),
        )
    )
    pending_approval_task_ids = set(approvals_result.scalars())
    has_started = any(
        session.status in (
            AgentSessionStatus.ACTIVE,
            AgentSessionStatus.IDLE,
            AgentSessionStatus.BLOCKED,
            AgentSessionStatus.DONE,
        )
        for session in sessions
    )
    execution_message = None
    if sessions:
        latest = sessions[0]
        if latest.status == AgentSessionStatus.ERROR:
            execution_message = f"Agent session failed for task #{latest.task_id}. Check Activity."
        elif latest.status == AgentSessionStatus.BLOCKED:
            execution_message = f"Agent session blocked on task #{latest.task_id}. Check Activity."
        elif latest.status == AgentSessionStatus.DONE:
            execution_message = f"Agent session finished for task #{latest.task_id}."
        elif latest.status == AgentSessionStatus.STARTING:
            execution_message = f"Agent starting task #{latest.task_id}."
        else:
            execution_message = f"Agent session active on task #{latest.task_id}."

    first_backlog_task_id = next(
        (task.id for stage in project.stages if stage.key == "backlog" for task in stage.tasks),
        None,
    )
    def has_worker_assignee(task: Task) -> bool:
        return bool(worker_role and any(
            assignee.entity_type == EntityType.AGENT and assignee.name == worker_role["agent"]
            for assignee in task.assignees
        ))

    first_todo_unassigned_task_id = next(
        (task.id for stage in project.stages if stage.key == "to_do"
         for task in stage.tasks if not has_worker_assignee(task)),
        None,
    )
    first_todo_assigned_task_id = next(
        (task.id for stage in project.stages if stage.key == "to_do"
         for task in stage.tasks if has_worker_assignee(task)),
        None,
    )
    completed_count = sum((has_folder, has_worker, bool(all_tasks), has_started))
    setup_checklist = {
        "has_folder": has_folder,
        "has_worker": has_worker,
        "worker_configured": worker_role is not None,
        "worker_agent": worker_role["agent"] if worker_role else "None",
        "worker_name": worker_role["display_name"] if worker_role else "Not configured",
        "has_tasks": bool(all_tasks),
        "task_count": len(all_tasks),
        "has_started": has_started,
        "execution_message": execution_message,
        "first_backlog_task_id": first_backlog_task_id,
        "todo_stage_id": todo_stage_id,
        "first_todo_unassigned_task_id": first_todo_unassigned_task_id,
        "first_todo_assigned_task_id": first_todo_assigned_task_id,
        "completed_count": completed_count,
        "all_complete": completed_count == 4,
    }

    return templates.TemplateResponse(request, "kanban_board.html", {
        "request": request,
        "project": project,
        "current_entity": current_entity,
        "stage_statuses": STAGE_STATUSES,
        "setup_checklist": setup_checklist,
        "latest_session_by_task": latest_session_by_task,
        "latest_activity_by_task": latest_activity_by_task,
        "start_state_by_task": start_state_by_task,
        "git_pr_state_by_task": git_pr_state_by_task,
        "pending_approval_task_ids": pending_approval_task_ids,
        "active_page": "board",
    })


@router.get("/ui/projects/{project_id}/workbench", response_class=HTMLResponse)
async def project_workbench(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    result = await db.execute(
        select(Project).filter(Project.id == project_id).options(selectinload(Project.creator))
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse(request, "project_workbench.html", {
        "request": request, "project": project, "current_entity": current_entity,
        "active_page": "activity"
    })


@router.get("/ui/projects/{project_id}/git", response_class=HTMLResponse)
async def project_git(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    result = await db.execute(
        select(Project).filter(Project.id == project_id).options(selectinload(Project.creator))
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse(request, "project_git.html", {
        "request": request, "project": project, "current_entity": current_entity,
        "active_page": "changes"
    })


@router.get("/ui/projects/{project_id}/settings", response_class=HTMLResponse)
async def project_settings(
    request: Request,
    project_id: int,
    db: AsyncSession = Depends(get_db),
    current_entity: Optional[Entity] = Depends(get_current_entity)
):
    result = await db.execute(
        select(Project)
        .filter(Project.id == project_id)
        .options(
            selectinload(Project.stages),
            selectinload(Project.creator)
        )
    )
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    role_data = await roles._role_assignment_payload()
    return templates.TemplateResponse(request, "project_settings.html", {
        "request": request,
        "project": project,
        "current_entity": current_entity,
        "role_data": role_data,
        "active_page": "settings",
    })


@router.get("/ui/users", response_class=HTMLResponse)
async def ui_users(request: Request, db: AsyncSession = Depends(get_db)):
    """Show configured roles, CLI availability, and current agent work."""
    result = await db.execute(select(Entity).order_by(Entity.created_at.desc()))
    users = result.scalars().all()
    role_payload = await roles._role_assignment_payload()
    from agent_kanban_pm.runtime.preferences import get_manager_agent_name
    manager_agent = get_manager_agent_name()

    active_result = await db.execute(
        select(AgentSession)
        .where(
            AgentSession.ended_at.is_(None),
            AgentSession.status.in_([
                AgentSessionStatus.STARTING,
                AgentSessionStatus.ACTIVE,
                AgentSessionStatus.IDLE,
                AgentSessionStatus.BLOCKED,
            ]),
        )
        .options(
            selectinload(AgentSession.agent),
            selectinload(AgentSession.task),
            selectinload(AgentSession.project),
        )
        .order_by(AgentSession.started_at.desc(), AgentSession.id.desc())
    )
    current_work = {}
    for session in active_result.scalars():
        if session.agent.name not in current_work:
            current_work[session.agent.name] = {
                "task_id": session.task_id,
                "task_title": session.task.title if session.task else None,
                "project_id": session.project_id,
                "project_name": session.project.name if session.project else None,
                "status": session.status.value,
            }

    return templates.TemplateResponse(request, "users.html", {
        "request": request,
        "users": users,
        "role_payload": role_payload,
        "manager_agent": manager_agent,
        "current_work": current_work,
    })
