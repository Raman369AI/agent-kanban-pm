"""MCP tool handlers: tasks."""

import logging
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from agent_kanban_pm.db import async_session_maker
from agent_kanban_pm.models import (
    Project, Task, Entity, Stage, TaskStatus, ApprovalStatus, Role,
    Comment
)
from agent_kanban_pm.events import event_bus, EventType
from agent_kanban_pm.services.tasks import (
    TaskReferenceError,
    TaskTransitionError,
    create_task_record,
    update_task_record,
)
from agent_kanban_pm.auth import is_owner_or_manager

logger = logging.getLogger(__name__)


class TaskHandlers:
    """Mixin for KanbanMCPServer."""

    async def _handle_create_task(self, args: dict) -> dict:
        """Create a new task using ORM"""
        self._require_role(Role.WORKER)
        async with async_session_maker() as db:
            # Verify project is approved before allowing task creation
            project_result = await db.execute(
                select(Project).filter(Project.id == args["project_id"])
            )
            project = project_result.scalar_one_or_none()
            if not project:
                return {"error": "Project not found"}
            if project.approval_status != ApprovalStatus.APPROVED:
                return {"error": f"Cannot create tasks in {project.approval_status.value} project. Project must be approved first."}

            # Get default stage (To Do) for this project
            result = await db.execute(
                select(Stage).filter(Stage.project_id == args["project_id"]).order_by(Stage.order)
            )
            stages = result.scalars().all()
            stage_id = stages[1].id if len(stages) > 1 else (stages[0].id if stages else None)

            creator_id = self.caller_entity.id
            try:
                task = await create_task_record(
                    db,
                    title=args["title"],
                    description=args.get("description", ""),
                    status=TaskStatus.PENDING,
                    project_id=args["project_id"],
                    stage_id=stage_id,
                    required_skills=args.get("required_skills", ""),
                    priority=args.get("priority", 0),
                    created_by=creator_id,
                )
            except TaskReferenceError as exc:
                return {"error": str(exc)}
            await db.commit()
            await db.refresh(task)

            return {"success": True, "task_id": task.id, "title": task.title}

    async def _handle_get_tasks(self, args: dict) -> list:
        """Get tasks with filters using ORM"""
        async with async_session_maker() as db:
            query = select(Task).options(selectinload(Task.assignees))
            if "project_id" in args:
                query = query.filter(Task.project_id == args["project_id"])
            if "status" in args:
                query = query.filter(Task.status == args["status"])

            result = await db.execute(query.order_by(Task.priority.desc()))
            tasks = result.scalars().all()

            task_list = []
            for t in tasks:
                # Filter by assigned_to_me if requested
                if args.get("assigned_to_me"):
                    assignee_ids = [a.id for a in t.assignees]
                    if self.caller_entity.id not in assignee_ids:
                        continue

                task_list.append({
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "status": str(t.status),
                    "project_id": t.project_id,
                    "stage_id": t.stage_id,
                    "priority": t.priority,
                    "assignees": [a.name for a in t.assignees]
                })

            return task_list

    async def _handle_get_my_tasks(self, args: dict) -> list:
        """Get tasks assigned to the authenticated caller"""
        agent_id = self.caller_entity.id
        async with async_session_maker() as db:
            query = select(Task).options(selectinload(Task.assignees))
            if "status" in args:
                query = query.filter(Task.status == args["status"])

            result = await db.execute(query.order_by(Task.priority.desc()))
            tasks = result.scalars().all()

            task_list = []
            for t in tasks:
                assignee_ids = [a.id for a in t.assignees]
                if agent_id not in assignee_ids:
                    continue

                task_list.append({
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "status": str(t.status),
                    "project_id": t.project_id,
                    "stage_id": t.stage_id,
                    "priority": t.priority,
                    "assignees": [a.name for a in t.assignees]
                })

            return task_list

    async def _handle_get_task_details(self, args: dict) -> dict:
        """Get detailed task information"""
        async with async_session_maker() as db:
            result = await db.execute(
                select(Task)
                .filter(Task.id == args["task_id"])
                .options(
                    selectinload(Task.assignees),
                    selectinload(Task.subtasks),
                    selectinload(Task.comments),
                    selectinload(Task.logs)
                )
            )
            task = result.scalar_one_or_none()

            if not task:
                return {"error": "Task not found"}

            return {
                "id": task.id,
                "title": task.title,
                "description": task.description,
                "status": str(task.status),
                "project_id": task.project_id,
                "stage_id": task.stage_id,
                "priority": task.priority,
                "assignees": [{"id": a.id, "name": a.name} for a in task.assignees],
                "comments": [
                    {"id": c.id, "content": c.content, "author_id": c.author_id, "created_at": c.created_at.isoformat() if c.created_at else None}
                    for c in task.comments
                ],
                "logs": [
                    {"id": l.id, "message": l.message, "log_type": l.log_type, "created_at": l.created_at.isoformat() if l.created_at else None}
                    for l in task.logs
                ]
            }


    async def _handle_move_task(self, args: dict) -> dict:
        """Move a task using ORM and publish event"""
        self._require_role(Role.WORKER)
        task_id = args["task_id"]
        stage_id = args.get("stage_id")
        status = args.get("status")

        async with async_session_maker() as db:
            result = await db.execute(select(Task).filter(Task.id == task_id))
            task = result.scalar_one_or_none()
            if not task:
                return {"error": "Task not found"}

            try:
                mutation = await update_task_record(
                    db,
                    task,
                    self.caller_entity,
                    changes={},
                    stage_id=stage_id,
                    status=status,
                    override_reason=args.get("override_reason"),
                )
            except (TaskReferenceError, TaskTransitionError, ValueError) as exc:
                return {"error": str(exc), "transition_blocked": True}
            transition_state = mutation.transition
            await db.commit()

            return {"success": True, "task_id": task_id, "new_stage_id": task.stage_id, "status": task.status.value}

    async def _handle_assign_task(self, args: dict) -> dict:
        """Assign an entity to a task using ORM and publish event"""
        task_id = args["task_id"]
        entity_id = args.get("entity_id")
        if entity_id is not None and entity_id != self.caller_entity.id:
            self._require_role(Role.MANAGER)

        async with async_session_maker() as db:
            result = await db.execute(
                select(Task).filter(Task.id == task_id).options(selectinload(Task.assignees))
            )
            task = result.scalar_one_or_none()
            if not task:
                return {"error": "Task not found"}

            # Project approval check (P0-2)
            project_result = await db.execute(select(Project).filter(Project.id == task.project_id))
            project = project_result.scalar_one_or_none()
            if project and project.approval_status != ApprovalStatus.APPROVED:
                if not is_owner_or_manager(self.caller_entity):
                    return {"error": f"Project is {project.approval_status.value}. Only managers/owners can modify it."}

            # Self-assign if no entity_id provided
            if entity_id is None:
                entity_id = self._target_agent_id(args)

            entity_result = await db.execute(select(Entity).filter(Entity.id == entity_id))
            entity = entity_result.scalar_one_or_none()
            if not entity:
                return {"error": "Entity not found"}

            from agent_kanban_pm.services.tasks import assign_task_record
            await assign_task_record(db, task, self.caller_entity, entity, role=args.get("role"), override_reason=args.get("override_reason"))
            await db.commit()

            return {"success": True, "task_id": task_id, "entity_id": entity_id, "entity_name": entity.name}

    async def _handle_add_comment(self, args: dict) -> dict:
        """Add a comment to a task"""
        async with async_session_maker() as db:
            task_res = await db.execute(select(Task).filter(Task.id == args["task_id"]))
            task = task_res.scalar_one_or_none()
            if not task:
                return {"error": "Task not found"}

            author_id = self._target_agent_id(args)
            comment = Comment(
                content=args["content"],
                task_id=args["task_id"],
                author_id=author_id
            )
            db.add(comment)
            await db.flush()
            event_bus.enqueue(db,
                EventType.TASK_COMMENTED.value,
                {"task_id": task.id, "comment": comment.content},
                project_id=task.project_id
            )
            await db.commit()
            await db.refresh(comment)

            return {"success": True, "comment_id": comment.id, "task_id": args["task_id"]}
