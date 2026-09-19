"""Work-product identity and completion gates for runtime handoffs."""
from __future__ import annotations
import json
from pathlib import Path
from sqlalchemy import select, func, desc
from agent_kanban_pm.models import (
    AgentSession, AgentSessionStatus, Stage, StagePolicy, ReviewMode,
    EntityType, Role, DiffReview, DiffReviewStatus,
)
from agent_kanban_pm.runtime.workspaces import WorkspacePreparationError, git_revision


async def implementation_for_task(db, task_id):
    return await db.scalar(select(AgentSession).where(
        AgentSession.task_id == task_id,
        AgentSession.status == AgentSessionStatus.DONE,
        func.coalesce(AgentSession.assigned_role, 'worker').notin_(['test', 'diff_review', 'git_pr']),
        AgentSession.handoff_received_at.is_not(None),
    ).order_by(AgentSession.id.desc()).limit(1))


def is_reopening(current_stage, target_stage):
    # Display order is editable and cannot grant a workflow bypass.
    rank = {"backlog": 0, "to_do": 1, "in_progress": 2, "review": 3, "done": 4}
    return (current_stage is not None and current_stage.key in rank and target_stage.key in rank
            and rank[target_stage.key] <= rank[current_stage.key])


async def completion_blocker(db, session, task, current_stage, target_stage, *, actor=None):
    """Return a reason rather than silently treating configured gates as passed."""
    policies = list((await db.execute(select(StagePolicy).where(
        StagePolicy.project_id == task.project_id,
        StagePolicy.stage_id.in_([current_stage.id if current_stage else None, target_stage.id]),
    ))).scalars())
    source_policy = next((p for p in policies if current_stage and p.stage_id == current_stage.id), None)
    target_policy = next((p for p in policies if p.stage_id == target_stage.id), None)
    human = actor is not None and actor.entity_type == EntityType.HUMAN
    orchestrator = actor is not None and actor.role in (Role.OWNER, Role.MANAGER)
    if target_policy and target_policy.requires_orchestrator_move and not (human or orchestrator):
        return 'Stage requires an explicit orchestrator or human move'
    if target_policy and target_policy.review_mode == ReviewMode.HUMAN and not human:
        return 'Stage requires human review'
    if is_reopening(current_stage, target_stage):
        return None  # Reopening work does not require completion evidence.
    if target_stage.key == 'done' and (not current_stage or current_stage.key != 'review'):
        return 'Complete review before marking the task Done'
    if target_stage.key == 'review' and session is None:
        return 'Missing implementation handoff for review'
    relevant = [session] if session else []
    if current_stage and current_stage.key == 'review':
        if session is None or session.source_session_id is None:
            return 'Review has no implementation handoff identity'
        source = await implementation_for_task(db, task.id)
        if not source or source.id != session.source_session_id or source.work_revision != session.work_revision:
            return "Review does not match the current implementation handoff"
        relevant = list((await db.execute(select(AgentSession).where(
            AgentSession.task_id == task.id,
            AgentSession.source_session_id == session.source_session_id,
            AgentSession.status == AgentSessionStatus.DONE,
            AgentSession.work_revision == session.work_revision,
            AgentSession.handoff_received_at.is_not(None),
        ))).scalars())
        roles = set(json.loads(source_policy.on_enter_roles_json or '[]')) if source_policy else {session.assigned_role}
        completed_roles = {s.assigned_role for s in relevant if s.handoff_received_at and s.work_revision == session.work_revision}
        if not roles <= completed_roles:
            return 'Waiting for required review roles: ' + ', '.join(sorted(roles - completed_roles))
    if source_policy and source_policy.review_mode == ReviewMode.HUMAN and not human:
        return 'Review stage requires a human decision'
    # Entering Review starts its reviewers; its conditional outcome gate applies
    # on departure. A condition on Done must also be satisfied on entry.
    applicable = [source_policy] + ([target_policy] if target_stage.key != "review" else [])
    if any(policy and policy.review_mode == ReviewMode.AUTO_THEN_HUMAN_FOR_CRITICAL for policy in applicable):
        from agent_kanban_pm.runtime.stage_policy import gather_transition_context
        if session is None or session.work_revision is None:
            return "A committed revision is required for automatic diff review"
        context = await gather_transition_context(db, task.id, task.project_id, work_revision=session.work_revision)
        if context['is_critical'] or not context['has_diff_review']:
            return 'A current diff review or human decision is required'
        from agent_kanban_pm.services.coordination import review_evidence
        try:
            evidence = await review_evidence(db, task.project_id, task.id)
        except Exception:
            return 'The approved review snapshot can no longer be verified'
        if evidence["diff_sha256"] != context["diff_sha256"]:
            return 'The implementation diff changed after review approval'
    required = set(json.loads(source_policy.required_outputs_json or '[]')) if source_policy else set()
    provided = set()
    review_context = None
    for row in relevant:
        claimed = set(json.loads(row.handoff_outputs_json or '[]'))
        role = row.assigned_role or "worker"
        if "code_changes" in claimed and role not in {"test", "diff_review", "git_pr"} and row.work_revision:
            provided.add("code_changes")
        if "status_summary" in claimed and row.handoff_summary:
            provided.add("status_summary")
        if "test_result" in claimed and role == "test" and row.status == AgentSessionStatus.DONE and row.exit_code in (None, 0):
            provided.add("test_result")
        if "final_summary" in claimed and role == "git_pr" and row.status == AgentSessionStatus.DONE and row.exit_code in (None, 0) and row.handoff_summary:
            provided.add("final_summary")
        if "diff_review_result" in claimed and role == "diff_review":
            if review_context is None:
                from agent_kanban_pm.runtime.stage_policy import gather_transition_context
                review_context = await gather_transition_context(
                    db, task.id, task.project_id, work_revision=row.work_revision)
            if review_context["has_diff_review"]:
                provided.add("diff_review_result")
        for output in claimed - {"code_changes", "status_summary", "test_result", "diff_review_result", "final_summary"}:
            try:
                candidate = (Path(row.workspace_path) / output).resolve()
                if candidate.is_relative_to(Path(row.workspace_path).resolve()) and candidate.is_file():
                    provided.add(output)
            except (OSError, ValueError):
                pass
    if not required <= provided:
        return 'Missing required outputs: ' + ', '.join(sorted(required - provided))
    return None


def _json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


async def completion_gate_status(db, task):
    """Return a read-only explanation of the evidence required before Done."""
    current = await db.get(Stage, task.stage_id) if task.stage_id else None
    source = await implementation_for_task(db, task.id)
    review_stage = await db.scalar(
        select(Stage).where(Stage.project_id == task.project_id, Stage.workflow_key == "review")
    )
    review_policy = (
        await db.scalar(
            select(StagePolicy).where(
                StagePolicy.project_id == task.project_id,
                StagePolicy.stage_id == review_stage.id,
            )
        )
        if review_stage
        else None
    )
    required_roles = set(
        _json_list(review_policy.on_enter_roles_json)
        if review_policy
        else ["diff_review", "test", "git_pr"]
    )

    sessions = []
    if source and source.work_revision:
        sessions = list(
            (
                await db.execute(
                    select(AgentSession)
                    .where(
                        AgentSession.task_id == task.id,
                        AgentSession.source_session_id == source.id,
                        AgentSession.work_revision == source.work_revision,
                    )
                    .order_by(desc(AgentSession.id))
                )
            )
            .scalars()
            .all()
        )

    role_sessions = {}
    for session in sessions:
        role_sessions.setdefault((session.assigned_role or "").strip().lower(), session)

    implementation_complete = bool(
        source
        and source.status == AgentSessionStatus.DONE
        and source.handoff_received_at
        and source.work_revision
    )
    test_session = role_sessions.get("test")
    test_outputs = set(_json_list(test_session.handoff_outputs_json)) if test_session else set()
    test_complete = bool(
        test_session
        and test_session.status == AgentSessionStatus.DONE
        and test_session.handoff_received_at
        and test_session.exit_code in (None, 0)
        and "test_result" in test_outputs
    )

    latest_review = None
    if source and source.work_revision:
        latest_review = await db.scalar(
            select(DiffReview)
            .where(
                DiffReview.task_id == task.id,
                DiffReview.project_id == task.project_id,
                DiffReview.work_revision == source.work_revision,
            )
            .order_by(
                desc(func.coalesce(DiffReview.reviewed_at, DiffReview.created_at)),
                desc(DiffReview.id),
            )
            .limit(1)
        )
    diff_session = role_sessions.get("diff_review")
    diff_outputs = set(_json_list(diff_session.handoff_outputs_json)) if diff_session else set()
    diff_complete = bool(
        diff_session
        and diff_session.status == AgentSessionStatus.DONE
        and diff_session.handoff_received_at
        and "diff_review_result" in diff_outputs
        and latest_review
        and latest_review.status == DiffReviewStatus.APPROVED
        and latest_review.diff_sha256
    )
    is_critical = bool(latest_review and latest_review.is_critical)

    git_sessions = [
        row for row in sessions
        if (row.assigned_role or "").strip().lower() == "git_pr"
    ]
    submitted_artifact = None
    verified_artifact = None
    for row in git_sessions:
        for artifact in _json_list(row.handoff_artifacts_json):
            if not isinstance(artifact, dict) or artifact.get("kind") != "pull_request":
                continue
            submitted_artifact = submitted_artifact or artifact
            if (
                artifact.get("state") == "merged"
                and artifact.get("verified_at")
                and source
                and artifact.get("head_revision") == source.work_revision
            ):
                verified_artifact = artifact
                break
        if verified_artifact:
            break
    git_session = role_sessions.get("git_pr")
    git_outputs = set(_json_list(git_session.handoff_outputs_json)) if git_session else set()
    git_complete = bool(
        git_session
        and git_session.status == AgentSessionStatus.DONE
        and git_session.handoff_received_at
        and git_session.exit_code in (None, 0)
        and "final_summary" in git_outputs
        and verified_artifact
    )

    def gate(key, label, complete, detail, *, required=True, url=None):
        return {
            "key": key,
            "label": label,
            "state": "complete" if complete else ("waiting" if required else "not_required"),
            "required": required,
            "detail": detail,
            "url": url,
        }

    revision = source.work_revision if source else None
    gates = [
        gate(
            "implementation",
            "Implementation handoff",
            implementation_complete,
            (
                f"Committed revision {revision[:12]} is recorded."
                if implementation_complete
                else "Waiting for a committed implementation handoff."
            ),
        ),
        gate(
            "test",
            "Test result",
            test_complete,
            "Tests completed for the reviewed revision." if test_complete else "Waiting for the test role and its result.",
            required="test" in required_roles,
        ),
        gate(
            "diff_review",
            "Diff review",
            diff_complete,
            (
                "The saved diff is approved."
                if diff_complete
                else (
                    f"Latest decision: {latest_review.status.value.replace('_', ' ')}."
                    if latest_review
                    else "Waiting for a review of the saved diff."
                )
            ),
            required="diff_review" in required_roles,
        ),
        gate(
            "human_decision",
            "Human decision",
            task.status.value == "completed",
            (
                "A human completion decision was recorded."
                if task.status.value == "completed"
                else "This change is marked critical and requires a human completion decision."
            ),
            required=is_critical,
        ),
        gate(
            "pull_request",
            "Pull request supplied",
            submitted_artifact is not None,
            (
                "A GitHub pull request was supplied."
                if submitted_artifact
                else "Waiting for the Git/PR role to supply one GitHub pull request."
            ),
            required="git_pr" in required_roles,
            url=submitted_artifact.get("url") if submitted_artifact else None,
        ),
        gate(
            "pull_request_merged",
            "Pull request merged",
            git_complete,
            (
                "GitHub verified the merged PR at the reviewed revision."
                if git_complete
                else "Waiting for GitHub to confirm a merged PR at the reviewed revision."
            ),
            required="git_pr" in required_roles,
            url=(verified_artifact or submitted_artifact or {}).get("url"),
        ),
    ]
    required_gates = [item for item in gates if item["required"]]
    completed = sum(item["state"] == "complete" for item in required_gates)

    blocker = None
    if current and current.key == "review":
        evidence = next(
            (
                row
                for row in sessions
                if row.status == AgentSessionStatus.DONE
                and row.handoff_received_at
                and (row.assigned_role or "").strip().lower()
                in {"test", "diff_review", "git_pr"}
            ),
            None,
        )
        done_stage = await db.scalar(
            select(Stage).where(Stage.project_id == task.project_id, Stage.workflow_key == "done")
        )
        if done_stage:
            blocker = await completion_blocker(
                db, evidence, task, current, done_stage, actor=None
            )
    elif not current or current.key != "done":
        blocker = "Completion gates become active when the task enters Review."

    return {
        "task_id": task.id,
        "stage": current.key if current else None,
        "revision": revision,
        "gates": gates,
        "completed": completed,
        "total": len(required_gates),
        "blocker": blocker,
        "can_complete_automatically": bool(
            current
            and current.key == "review"
            and blocker is None
            and completed == len(required_gates)
        ),
        "is_critical": is_critical,
    }


def verify_handoff_revision(session):
    revision = git_revision(session.workspace_path)
    if session.source_session_id and session.work_revision != revision:
        raise WorkspacePreparationError('Review changed the implementation revision; a new implementation handoff is required')
    return revision
