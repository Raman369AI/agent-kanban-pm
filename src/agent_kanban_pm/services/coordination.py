"""Authorization and ownership checks shared by coordination interfaces."""
import asyncio
import hashlib
import json
from agent_kanban_pm.models import AgentSession, Entity, Task, Project, Role
from agent_kanban_pm.services.tasks import TaskReferenceError


async def validate_approval_scope(db, project_id, task_id, session_id, agent_id):
    if await db.get(Entity, agent_id) is None:
        raise TaskReferenceError('Approval agent does not exist')
    if session_id is not None:
        session = await db.get(AgentSession, session_id)
        if session is None:
            raise TaskReferenceError('Approval session does not exist')
        if session.project_id != project_id or session.agent_id != agent_id:
            raise PermissionError('Approval must belong to the same project and agent as its session')
        if session.ended_at is not None:
            raise TaskReferenceError('Cannot request approval for an ended session')
        if task_id is not None and task_id != session.task_id:
            raise TaskReferenceError('Approval task does not match its session')
        task_id = session.task_id
    if task_id is not None:
        task = await db.get(Task, task_id)
        if task is None or task.project_id != project_id:
            raise TaskReferenceError('Approval task does not belong to this project')
    return task_id


def authorize_review_update(review, actor):
    if actor is None or not actor.is_active or actor.role == Role.VIEWER:
        raise PermissionError('An active reviewer or manager is required')
    if actor.role in (Role.OWNER, Role.MANAGER):
        return
    if actor.id != review.reviewer_id or actor.id == review.requester_id:
        raise PermissionError('Only the designated independent reviewer or a manager can resolve this review')


async def review_work_revision(db, project_id, task_id):
    """Bind a review to the recorded implementation, never a client-supplied hash."""
    if task_id is None:
        return None
    task = await db.get(Task, task_id)
    if task is None or task.project_id != project_id:
        raise TaskReferenceError('Review task does not belong to this project')
    from agent_kanban_pm.runtime.review_gate import implementation_for_task
    source = await implementation_for_task(db, task_id)
    return source.work_revision if source else None

async def review_evidence(db, project_id, task_id):
    """Capture the current implementation diff from Git and bind it to its revision."""
    revision = await review_work_revision(db, project_id, task_id)
    if task_id is None or revision is None:
        return {"work_revision": revision, "base_revision": None, "diff": None, "diff_sha256": None, "file_paths": None, "is_critical": False}
    from agent_kanban_pm.runtime.review_gate import implementation_for_task
    from agent_kanban_pm.runtime.workspaces import git_revision, WorkspacePreparationError
    from agent_kanban_pm.runtime.assignment_launcher import _task_branch_name
    from agent_kanban_pm.services.git_diff import read_task_git_diff
    from agent_kanban_pm.runtime.stage_policy import CRITICAL_FILE_PATTERNS
    source = await implementation_for_task(db, task_id)
    project = await db.get(Project, project_id)
    agent = await db.get(Entity, source.agent_id) if source else None
    if source is None or project is None or not project.path:
        raise TaskReferenceError("Review has no implementation workspace")
    try:
        current_revision = await asyncio.to_thread(git_revision, source.workspace_path)
    except WorkspacePreparationError as exc:
        raise TaskReferenceError(str(exc)) from exc
    if current_revision != revision:
        raise TaskReferenceError("Implementation changed after handoff")
    branch = _task_branch_name(await db.get(Task, task_id), agent) if agent else None
    snapshot = await asyncio.to_thread(
        read_task_git_diff,
        project.path,
        source.workspace_path,
        branch,
        source.review_base_revision,
    )
    if snapshot is None or snapshot.get("truncated"):
        raise TaskReferenceError("A complete server-generated Git diff is required for review")
    if source.review_base_revision is None:
        source.review_base_revision = snapshot["base_revision"]
    patch = snapshot.get("diff", "")
    paths = []
    for line in patch.splitlines():
        if line.startswith("diff --git a/") and " b/" in line:
            path = line.split(" b/", 1)[1]
            if path not in paths:
                paths.append(path)
    return {
        "work_revision": revision,
        "base_revision": source.review_base_revision,
        "diff": patch,
        "diff_sha256": hashlib.sha256(patch.encode("utf-8")).hexdigest(),
        "file_paths": json.dumps(paths),
        "is_critical": any(any(pattern in path for pattern in CRITICAL_FILE_PATTERNS) for path in paths),
    }
