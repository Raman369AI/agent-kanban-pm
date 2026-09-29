"""MCP tool handlers: reviews."""

import logging
from datetime import UTC, datetime
from sqlalchemy import select

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    DiffReview, DiffReviewStatus
)
from agent_kanban_pm.events import event_bus, EventType

logger = logging.getLogger(__name__)


class ReviewHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_request_diff_review(self, args: dict) -> dict:
        """Create a diff review request for critical code paths."""
        project_id = args["project_id"]
        async with async_session_maker() as db:
            from agent_kanban_pm.services.coordination import review_evidence
            evidence = await review_evidence(db, project_id, args.get("task_id"))
            review = DiffReview(
                work_revision=evidence["work_revision"],
                base_revision=evidence["base_revision"],
                diff_sha256=evidence["diff_sha256"],
                project_id=project_id,
                task_id=args.get("task_id"),
                reviewer_id=None,
                requester_id=self.caller_entity.id,
                diff_content=evidence["diff"] if evidence["diff"] is not None else args["diff_content"],
                summary=args.get("summary"),
                file_paths=evidence["file_paths"] if evidence["file_paths"] is not None else args.get("file_paths"),
                is_critical=evidence["is_critical"] if evidence["diff"] is not None else args.get("is_critical", False),
                status=DiffReviewStatus.PENDING,
            )
            db.add(review)
            await db.flush()
            event_bus.enqueue(db,
                EventType.DIFF_REVIEW_REQUESTED.value,
                {
                    "review_id": review.id,
                    "project_id": project_id,
                    "task_id": args.get("task_id"),
                    "requester_id": self.caller_entity.id,
                    "is_critical": review.is_critical,
                },
                project_id=project_id,
                entity_id=self.caller_entity.id
            )
            await db.commit()
            await db.refresh(review)
            return {
                "success": True,
                "review_id": review.id,
                "status": review.status.value,
                "is_critical": review.is_critical,
            }

    async def _handle_review_diff(self, args: dict) -> dict:
        """Approve, reject, or request changes on a diff review."""
        review_id = args["review_id"]
        new_status = DiffReviewStatus(args["status"])
        async with async_session_maker() as db:
            result = await db.execute(select(DiffReview).filter(DiffReview.id == review_id))
            review = result.scalar_one_or_none()
            if not review:
                return {"error": "Diff review not found"}
            from agent_kanban_pm.services.coordination import authorize_review_update
            authorize_review_update(review, self.caller_entity)
            review.status = new_status
            review.review_notes = args.get("review_notes")
            review.reviewer_id = self.caller_entity.id
            if new_status in (DiffReviewStatus.APPROVED, DiffReviewStatus.REJECTED, DiffReviewStatus.CHANGES_REQUESTED):
                review.reviewed_at = datetime.now(UTC)
            event_bus.enqueue(db,
                EventType.DIFF_REVIEW_COMPLETED.value,
                {
                    "review_id": review.id,
                    "project_id": review.project_id,
                    "status": new_status.value,
                    "reviewer_id": self.caller_entity.id,
                },
                project_id=review.project_id,
                entity_id=self.caller_entity.id
            )
            await db.commit()
            return {
                "success": True,
                "review_id": review.id,
                "status": new_status.value,
            }

    async def _handle_get_diff_reviews(self, args: dict) -> list:
        """Get diff reviews for a project."""
        project_id = args["project_id"]
        limit = args.get("limit", 20)
        async with async_session_maker() as db:
            query = (
                select(DiffReview)
                .filter(DiffReview.project_id == project_id)
                .order_by(DiffReview.created_at.desc())
                .limit(limit)
            )
            if "status" in args:
                query = query.filter(DiffReview.status == DiffReviewStatus(args["status"]))
            result = await db.execute(query)
            reviews = result.scalars().all()
            return [
                {
                    "id": r.id,
                    "task_id": r.task_id,
                    "requester_id": r.requester_id,
                    "reviewer_id": r.reviewer_id,
                    "status": r.status.value,
                    "is_critical": r.is_critical,
                    "summary": r.summary,
                    "file_paths": r.file_paths,
                    "review_notes": r.review_notes,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
                }
                for r in reviews
            ]
