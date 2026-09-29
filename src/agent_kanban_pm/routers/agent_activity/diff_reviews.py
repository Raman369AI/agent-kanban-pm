from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List, Optional
from datetime import UTC, datetime
import asyncio
import logging

from agent_kanban_pm.db import get_db
from agent_kanban_pm.models import (
    AgentSession, Entity, Role, Task, Project, DiffReview, DiffReviewStatus,
)
from agent_kanban_pm.schemas import (
    DiffReviewCreate, DiffReviewUpdate, DiffReviewResponse,
)
from agent_kanban_pm.auth import get_current_entity, is_owner_or_manager
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.runtime.assignment_launcher import _task_branch_name
from agent_kanban_pm.services.git_diff import read_task_git_diff

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/projects/{project_id}/tasks/{task_id}/git-diff")
async def get_task_git_diff(
    project_id: int,
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Show task changes from a surviving worktree or committed task branch."""
    task = (await db.execute(select(Task).filter(
        Task.id == task_id, Task.project_id == project_id,
    ))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found in project")
    project = (await db.execute(select(Project).filter(Project.id == project_id))).scalar_one_or_none()
    sessions = []
    if project is not None and project.path:
        sessions = (await db.execute(
            select(AgentSession, Entity).join(Entity, AgentSession.agent_id == Entity.id)
            .filter(AgentSession.project_id == project_id, AgentSession.task_id == task_id)
            .order_by(desc(AgentSession.started_at), desc(AgentSession.id))
        )).all()
    empty_snapshot = None
    for session, agent in sessions:
        branch = _task_branch_name(task, agent)
        snapshot = await asyncio.to_thread(
            read_task_git_diff, project.path, session.workspace_path, branch,
            session.review_base_revision,
        )
        if snapshot is not None:
            snapshot["session_id"] = session.id
            if snapshot["diff"]:
                return snapshot
            if empty_snapshot is None:
                empty_snapshot = snapshot

    review = (await db.execute(
        select(DiffReview)
        .filter(DiffReview.project_id == project_id, DiffReview.task_id == task_id)
        .order_by(desc(DiffReview.created_at), desc(DiffReview.id))
        .limit(1)
    )).scalar_one_or_none()
    if review is not None:
        return {
            "source": "review", "diff": review.diff_content,
            "message": "Saved review snapshot; the task workspace is unavailable.",
            "review_id": review.id,
        }
    if empty_snapshot is not None:
        return empty_snapshot
    if project is None or not project.path:
        return {"source": "none", "diff": "", "message": "This project has no Git workspace."}
    return {
        "source": "none", "diff": "",
        "message": "No task Git changes are available. The worktree may have been removed before its changes were saved.",
    }


@router.get("/projects/{project_id}/diff-reviews", response_model=List[DiffReviewResponse])
async def get_project_diff_reviews(
    project_id: int,
    status: Optional[str] = None,
    task_id: Optional[int] = None,
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
    if task_id is not None:
        query = query.filter(DiffReview.task_id == task_id)
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
    if review.requester_id is not None and review.requester_id != current_entity.id:
        raise HTTPException(
            status_code=422,
            detail="requester_id is derived from the authenticated caller",
        )
    if (
        review.reviewer_id == current_entity.id
        and not is_owner_or_manager(current_entity)
    ):
        raise HTTPException(
            status_code=422,
            detail="An independent reviewer must differ from the requester",
        )
    if review.reviewer_id is not None:
        reviewer = await db.get(Entity, review.reviewer_id)
        if reviewer is None or not reviewer.is_active or reviewer.role == Role.VIEWER:
            raise HTTPException(status_code=422, detail="Reviewer must be an active non-viewer entity")

    from agent_kanban_pm.services.coordination import review_evidence
    evidence = await review_evidence(db, project_id, review.task_id)
    db_review = DiffReview(
        work_revision=evidence["work_revision"],
        base_revision=evidence["base_revision"],
        diff_sha256=evidence["diff_sha256"],
        project_id=project_id,
        task_id=review.task_id,
        reviewer_id=review.reviewer_id or current_entity.id,
        requester_id=current_entity.id,
        diff_content=evidence["diff"] if evidence["diff"] is not None else review.diff_content,
        summary=review.summary,
        file_paths=evidence["file_paths"] if evidence["file_paths"] is not None else review.file_paths,
        is_critical=evidence["is_critical"] if evidence["diff"] is not None else review.is_critical,
        status=DiffReviewStatus.PENDING,
    )
    db.add(db_review)
    await db.flush()
    event_bus.enqueue(db,
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
    await db.commit()
    await db.refresh(db_review)
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

    from agent_kanban_pm.services.coordination import authorize_review_update
    authorize_review_update(review, current_entity)
    review.status = update.status
    review.review_notes = update.review_notes
    if update.status in (DiffReviewStatus.APPROVED, DiffReviewStatus.REJECTED, DiffReviewStatus.CHANGES_REQUESTED):
        review.reviewer_id = current_entity.id
        review.reviewed_at = datetime.now(UTC)

    event_bus.enqueue(db,
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
    await db.commit()
    await db.refresh(review)
    return review


# ---------------------------------------------------------------------------
# Approval Queue
