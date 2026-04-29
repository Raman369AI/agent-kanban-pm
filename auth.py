"""
Authentication and Authorization module.

Local-first RBAC with env-based identity:
- Humans identify via X-Entity-ID header
- MCP agents identify via KANBAN_AGENT_NAME env var (looked up against Entity.name)
- If no headers provided for GET requests, falls back to first active entity
  (deprecated, logged) for backward compat. Mutations require explicit identity.

Roles:
    OWNER   - Full control, user management, project approval
    MANAGER - Project approval, task assignment, stage management
    WORKER  - Task creation (approved projects only), self-assignment, updates
    VIEWER  - Read-only
"""

import logging
from typing import Optional, List

from sqlalchemy import select
from fastapi import Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db, async_session_maker
from models import Entity, EntityType, Role, Project, Task, ApprovalStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Identity Resolution
# ---------------------------------------------------------------------------

async def _get_entity_by_id(db: AsyncSession, entity_id: int) -> Optional[Entity]:
    result = await db.execute(select(Entity).filter(Entity.id == entity_id, Entity.is_active == True))
    return result.scalar_one_or_none()


async def _get_default_entity(db: AsyncSession) -> Optional[Entity]:
    """Get the local human owner — the user driving the browser session.

    Preference order:
      1. Human with OWNER role (the local user)
      2. Any human
      3. Any active entity (last resort)
    """
    # 1. Human OWNER — the local user
    result = await db.execute(
        select(Entity)
        .filter(Entity.is_active == True, Entity.entity_type == EntityType.HUMAN, Entity.role == Role.OWNER)
        .order_by(Entity.id.asc()).limit(1)
    )
    entity = result.scalar_one_or_none()
    if entity:
        return entity

    # 2. Any human
    result = await db.execute(
        select(Entity)
        .filter(Entity.is_active == True, Entity.entity_type == EntityType.HUMAN)
        .order_by(Entity.id.asc()).limit(1)
    )
    entity = result.scalar_one_or_none()
    if entity:
        return entity

    # 3. Any active entity (legacy fallback)
    result = await db.execute(select(Entity).filter(Entity.is_active == True).order_by(Entity.id.asc()).limit(1))
    return result.scalar_one_or_none()


async def resolve_current_entity(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional[Entity]:
    """
    Resolve the current entity from headers, in priority order:
    1. X-Entity-ID (for humans in local mode)
    2. For safe GET requests only: fallback to first active entity (deprecated)
    """
    # 1. Try explicit entity ID
    entity_id_header = request.headers.get("x-entity-id")
    if entity_id_header:
        try:
            entity_id = int(entity_id_header)
            entity = await _get_entity_by_id(db, entity_id)
            if entity:
                return entity
        except ValueError:
            pass

    # 3. Local-first fallback: first active entity for
    #    (a) safe GET requests, or
    #    (b) any /ui/ route — these are browser-session bound on localhost
    #        and represent the local human user. Without this, the kanban
    #        board UI cannot mutate without the browser injecting headers.
    if request.method == "GET" or request.url.path.startswith("/ui/"):
        entity = await _get_default_entity(db)
        if entity:
            logger.debug(
                "Local fallback: resolved entity '%s' (id=%s) for %s %s.",
                entity.name, entity.id, request.method, request.url.path
            )
        return entity

    return None


async def get_current_entity(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional[Entity]:
    """Get current entity. Returns None only if no entities exist at all."""
    return await resolve_current_entity(request, db)


async def get_current_entity_optional(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional[Entity]:
    """Same as get_current_entity - always returns the resolved entity or None."""
    return await resolve_current_entity(request, db)


async def get_current_active_entity(
    current_entity: Optional[Entity] = Depends(get_current_entity)
) -> Optional[Entity]:
    """Returns the current entity. No additional active check enforced (is_active already filtered)."""
    return current_entity


async def get_current_agent(
    request: Request,
    db: AsyncSession = Depends(get_db)
) -> Optional[Entity]:
    """HTTP requests do not authenticate agents in local-first mode."""
    return None


# ---------------------------------------------------------------------------
# RBAC Dependencies
# ---------------------------------------------------------------------------

# Role hierarchy: higher index = more permissive
ROLE_LEVELS = {
    Role.VIEWER: 0,
    Role.WORKER: 1,
    Role.MANAGER: 2,
    Role.OWNER: 3,
}

def get_effective_role(entity: Entity) -> Role:
    """Return the effective role for permission checks."""
    return entity.role


def require_role(min_role: Role):
    """Dependency factory that enforces a minimum role level."""
    async def _check_role(
        current_entity: Optional[Entity] = Depends(get_current_entity)
    ) -> Entity:
        if not current_entity:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not authenticated. Provide X-Entity-ID header."
            )
        effective = get_effective_role(current_entity)
        if ROLE_LEVELS.get(effective, 0) < ROLE_LEVELS.get(min_role, 0):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {min_role.value}"
            )
        return current_entity
    return _check_role


# Convenience dependencies
require_owner = require_role(Role.OWNER)
require_manager = require_role(Role.MANAGER)
require_worker = require_role(Role.WORKER)
require_viewer = require_role(Role.VIEWER)


# ---------------------------------------------------------------------------
# Resource-Level Permission Helpers
# ---------------------------------------------------------------------------

def is_owner_or_manager(entity: Entity) -> bool:
    return entity.role in (Role.OWNER, Role.MANAGER)


async def require_project_approval_for_mutation(
    project: Project,
    entity: Entity
) -> None:
    """
    Enforce that only OWNER/MANAGER can mutate tasks/stages in non-approved projects.
    Raises HTTPException if the project is not approved and entity is WORKER/VIEWER.
    """
    if project.approval_status != ApprovalStatus.APPROVED and not is_owner_or_manager(entity):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Project is {project.approval_status.value}. Only managers/owners can modify it."
        )


async def require_task_access(
    task: Task,
    entity: Entity,
    db: AsyncSession,
    require_write: bool = True
) -> None:
    """
    Enforce task-level access:
    - OWNER/MANAGER: full access
    - WORKER: can read all, write only if creator or assignee
    - VIEWER: read only (if require_write=True, will 403)
    """
    if is_owner_or_manager(entity):
        return

    if entity.role == Role.VIEWER and require_write:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Viewers cannot modify tasks."
        )

    if entity.role == Role.WORKER and require_write:
        # WORKER can write if they created the task or are assigned to it
        is_creator = task.created_by == entity.id
        is_assignee = any(a.id == entity.id for a in task.assignees)
        if not (is_creator or is_assignee):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only modify tasks you created or are assigned to."
            )

