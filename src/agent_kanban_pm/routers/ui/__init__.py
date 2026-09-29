"""Server-rendered UI pages and UI-facing JSON endpoints, split by concern.

``pages`` renders HTML, ``tasks`` and ``projects`` hold the mutation endpoints,
``config_api`` serves settings/roles/workspace pickers, and ``roles`` owns the
role-assignment helpers. This package assembles them into one hidden router.
"""
from fastapi import APIRouter

from . import _common, config_api, pages, projects, roles, tasks  # noqa: F401
from ._common import *  # noqa: F401,F403
from ._common import (  # noqa: F401
    _activity_preview,
    _notify_stage_policy_for_todo,
    _plan_items_from_chat,
    _stage_name_matches,
    _status_label,
    _utc_iso,
)
from .config_api import *  # noqa: F401,F403
from .pages import *  # noqa: F401,F403
from .projects import *  # noqa: F401,F403
from .roles import _ensure_role_entity, _role_assignment_payload, _role_to_entity_role  # noqa: F401
from .tasks import *  # noqa: F401,F403
from .tasks import _render_acceptance, _render_dependencies  # noqa: F401

router = APIRouter(include_in_schema=False)
for _module in (pages, tasks, config_api, projects):
    router.include_router(_module.router)
