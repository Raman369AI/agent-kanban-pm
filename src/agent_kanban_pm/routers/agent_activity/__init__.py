"""Agent activity API, split by concern.

Each submodule owns a ``router``; this package assembles them under ``/agents``.
Route order matters because the ``/{agent_id}/...`` routes in ``sessions`` and
``activity`` are parameterised, so those modules are included last.
"""
from fastapi import APIRouter

from . import (
    approvals,
    contributions,
    coordination,
    diff_reviews,
    launch_requests,
    stage_policies,
    usage,
    activity,
    sessions,
)
from .approvals import *  # noqa: F401,F403
from .contributions import *  # noqa: F401,F403
from .coordination import *  # noqa: F401,F403
from .diff_reviews import *  # noqa: F401,F403
from .launch_requests import *  # noqa: F401,F403
from .stage_policies import *  # noqa: F401,F403
from .usage import *  # noqa: F401,F403
from .activity import *  # noqa: F401,F403
from .sessions import *  # noqa: F401,F403

router = APIRouter(prefix="/agents", tags=["agent-activity"])
for _module in (
    usage,
    contributions,
    coordination,
    diff_reviews,
    launch_requests,
    approvals,
    stage_policies,
    activity,
    sessions,
):
    router.include_router(_module.router)
