#!/usr/bin/env python3
"""
MCP Server for Agent Kanban PM

Provides Model Context Protocol tools for AI agents to interact with
the Kanban board. Designed for ephemeral CLI agents (Claude Code, Codex,
OpenCode, Gemini CLI) that connect via stdio.

Because MCP stdio servers cannot push events, agents must poll for updates
using the `get_pending_events` tool.

Usage:
    python mcp_server.py

The MCP server communicates over stdin/stdout with the host AI tool.
"""

import asyncio
import json
import logging
import sys
from typing import Any, Optional, Sequence
from datetime import UTC, datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database import async_session_maker
from models import (
    Project, Task, Entity, Stage, TaskStatus, EntityType,
    ApprovalStatus, AgentConnection, ProtocolType, PendingEvent, Role,
    Comment, AgentSession, AgentSessionStatus, AgentActivity, ActivityType,
    OrchestrationDecision, DecisionType, TaskLease, LeaseStatus,
    ActivitySummary, UserContribution, ContributionType, ProjectWorkspace,
    DiffReview, DiffReviewStatus,
    AgentApproval, AgentApprovalStatus, ApprovalType
)
from event_bus import event_bus, EventType
from auth import is_owner_or_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool,
        TextContent,
    )
    MCP_AVAILABLE = True
except ImportError as e:
    MCP_AVAILABLE = False
    logger.error(f"MCP library not available: {e}")
    logger.error("Install with: pip install mcp")


class KanbanMCPServer:
    """MCP Server for Agent Kanban PM system"""

    def __init__(self):
        if not MCP_AVAILABLE:
            raise RuntimeError("MCP library not installed")
        self.server = Server("agent-kanban-pm")
        self.caller_entity: Optional[Entity] = None
        self._caller_name: Optional[str] = None
        self._caller_role: str = "worker"
        self._resolve_caller()
        self.setup_handlers()

    def _resolve_caller(self):
        """
        Resolve caller identity from KANBAN_AGENT_NAME env var.
        Local-first: the OS process is the trust boundary.
        """
        import os
        name = os.environ.get("KANBAN_AGENT_NAME")
        if not name:
            logger.error("KANBAN_AGENT_NAME not set in environment. MCP identity required.")
            raise RuntimeError("KANBAN_AGENT_NAME not set in environment")
        self._caller_name = name
        self._caller_role = os.environ.get("KANBAN_AGENT_ROLE", "worker")

    async def _authenticate(self) -> Entity:
        """Authenticate the caller by looking up Entity.name."""
        if self.caller_entity:
            return self.caller_entity

        async with async_session_maker() as db:
            result = await db.execute(
                select(Entity).filter(Entity.name == self._caller_name, Entity.is_active == True)
            )
            entity = result.scalar_one_or_none()
            if not entity:
                logger.error(f"No active entity named '{self._caller_name}'")
                raise RuntimeError(f"MCP identity '{self._caller_name}' not found")
            self.caller_entity = entity
            logger.info(f"MCP authenticated as {entity.name} (role={entity.role.value})")
            return entity

    def _require_role(self, min_role: Role):
        """Check if the authenticated caller has at least the required role."""
        from auth import ROLE_LEVELS, get_effective_role
        entity = self.caller_entity
        if not entity:
            raise PermissionError("Not authenticated")
        effective = get_effective_role(entity)
        if ROLE_LEVELS.get(effective, 0) < ROLE_LEVELS.get(min_role, 0):
            raise PermissionError(f"Insufficient permissions. Required: {min_role.value}, have: {effective.value}")

    def _target_agent_id(self, args: dict) -> int:
        """Return the allowed agent target for a tool call."""
        requested = args.get("agent_id")
        if requested is None or requested == self.caller_entity.id:
            return self.caller_entity.id
        self._require_role(Role.MANAGER)
        return requested

    def setup_handlers(self):
        """Setup MCP handlers"""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            """List available MCP tools for agents"""
            return [
                Tool(
                    name="create_project",
                    description="Create a new project with default Kanban stages",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Project name"},
                            "description": {"type": "string", "description": "Project description"}
                        },
                        "required": ["name"]
                    }
                ),
                Tool(
                    name="get_projects",
                    description="Get all projects with basic details",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {
                                "type": "string",
                                "enum": ["pending", "approved", "rejected"],
                                "description": "Filter by approval status"
                            }
                        }
                    }
                ),
                Tool(
                    name="get_project_details",
                    description="Get detailed information about a specific project including stages and tasks",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Project ID"}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="create_task",
                    description="Create a new task in a project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Project ID"},
                            "title": {"type": "string", "description": "Task title"},
                            "description": {"type": "string", "description": "Task description"},
                            "required_skills": {"type": "string", "description": "Required skills (comma-separated)"},
                            "priority": {"type": "integer", "description": "Task priority (0-10, higher = more important)"}
                        },
                        "required": ["project_id", "title"]
                    }
                ),
                Tool(
                    name="get_tasks",
                    description="Get tasks with optional filters",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Filter by project ID"},
                            "status": {"type": "string", "description": "Filter by task status"},
                            "assigned_to_me": {"type": "boolean", "description": "Filter to tasks assigned to this agent"}
                        }
                    }
                ),
                Tool(
                    name="get_task_details",
                    description="Get detailed information about a task including comments and logs",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer", "description": "Task ID"}
                        },
                        "required": ["task_id"]
                    }
                ),
                Tool(
                    name="approve_project",
                    description="Approve a pending project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer", "description": "Project ID to approve"}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="move_task",
                    description="Move a task to a different stage or update status",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer"},
                            "stage_id": {"type": "integer", "description": "New stage ID (optional)"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "in_review", "completed", "blocked"]}
                        },
                        "required": ["task_id"]
                    }
                ),
                Tool(
                    name="assign_task",
                    description="Assign an entity to a task",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer"},
                            "entity_id": {"type": "integer", "description": "Entity ID to assign. Omit to self-assign."}
                        },
                        "required": ["task_id"]
                    }
                ),
                Tool(
                    name="add_comment",
                    description="Add a comment to a task",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer"},
                            "content": {"type": "string", "description": "Comment text"}
                        },
                        "required": ["task_id", "content"]
                    }
                ),
                Tool(
                    name="get_my_tasks",
                    description="Get all tasks currently assigned to me (the default agent)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "status": {"type": "string", "description": "Filter by status (optional)"}
                        }
                    }
                ),
                Tool(
                    name="get_pending_events",
                    description="Poll for recent task and project events for this agent. Returns events and clears them.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "integer", "description": "Your agent/entity ID"},
                            "limit": {"type": "integer", "description": "Maximum events to return", "default": 50}
                        },
                        "required": ["agent_id"]
                    }
                ),
                Tool(
                    name="register_subscription",
                    description="Register interest in specific events for your agent",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "integer", "description": "Your agent/entity ID"},
                            "events": {"type": "array", "items": {"type": "string"}, "description": "List of EventTypes to subscribe to (e.g., ['task_created', 'task_assigned'] or ['*'] for all)"},
                            "projects": {"type": "array", "items": {"type": "integer"}, "description": "List of project IDs to filter by (omit for all)"}
                        },
                        "required": ["agent_id", "events"]
                    }
                ),
                Tool(
                    name="list_agents",
                    description="List all registered agents with their skills",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                Tool(
                    name="list_entities",
                    description="List all entities (humans and agents)",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                Tool(
                    name="report_status",
                    description="Report your current status to the manager. Call this at least every heartbeat_interval seconds.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "integer", "description": "Your agent/entity ID"},
                            "status_type": {"type": "string", "enum": ["idle", "thinking", "working", "blocked", "waiting", "done"], "description": "Current status"},
                            "message": {"type": "string", "description": "Optional status message"},
                            "task_id": {"type": "integer", "description": "Task you are working on (optional)"}
                        },
                        "required": ["status_type"]
                    }
                ),
                Tool(
                    name="log_activity",
                    description="Log a structured activity entry for project/session visibility.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "integer", "description": "Your agent/entity ID"},
                            "session_id": {"type": "integer", "description": "Agent session ID (optional)"},
                            "project_id": {"type": "integer", "description": "Project ID (optional)"},
                            "activity_type": {"type": "string", "enum": ["thought", "action", "observation", "result", "error", "file_change", "command", "tool_call", "handoff"], "description": "Type of activity"},
                            "message": {"type": "string", "description": "Activity message"},
                            "task_id": {"type": "integer", "description": "Related task ID (optional)"},
                            "source": {"type": "string", "description": "Native source such as claude_hook, codex_event, stdout, mcp"},
                            "payload_json": {"type": "string", "description": "Raw structured event JSON string (optional)"},
                            "workspace_path": {"type": "string", "description": "Project folder/worktree path (optional)"},
                            "file_path": {"type": "string", "description": "File touched or inspected (optional)"},
                            "command": {"type": "string", "description": "Command or tool call summary (optional)"}
                        },
                        "required": ["activity_type", "message"]
                    }
                ),
                Tool(
                    name="start_agent_session",
                    description="Start a durable CLI-agent session scoped to a project workspace.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "integer", "description": "Agent/entity ID. Managers may target another agent."},
                            "project_id": {"type": "integer", "description": "Project ID"},
                            "task_id": {"type": "integer", "description": "Current task ID (optional)"},
                            "workspace_path": {"type": "string", "description": "Project folder/worktree path. Defaults to Project.path."},
                            "command": {"type": "string", "description": "Spawned command or CLI invocation"},
                            "model": {"type": "string", "description": "Model name (optional)"},
                            "mode": {"type": "string", "description": "supervised, auto, or headless"}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="end_agent_session",
                    description="End or update a durable CLI-agent session.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "integer"},
                            "status": {"type": "string", "enum": ["done", "error", "blocked", "idle"], "default": "done"},
                            "message": {"type": "string", "description": "Optional final message"}
                        },
                        "required": ["session_id"]
                    }
                ),
                Tool(
                    name="get_agent_sessions",
                    description="Get active or recent agent sessions.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "integer"},
                            "project_id": {"type": "integer"},
                            "task_id": {"type": "integer"},
                            "active_only": {"type": "boolean", "default": False},
                            "limit": {"type": "integer", "default": 50}
                        }
                    }
                ),
                Tool(
                    name="get_project_activity",
                    description="Get the structured orchestration feed for a project.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "limit": {"type": "integer", "default": 100}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="record_decision",
                    description="Manager-only: record why a routing, assignment, approval, or handoff decision was made.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "decision_type": {"type": "string", "enum": ["task_assign", "task_reassign", "task_split", "approval_request", "priority_change", "handoff", "other"]},
                            "input_summary": {"type": "string"},
                            "rationale": {"type": "string"},
                            "affected_task_ids": {"type": "array", "items": {"type": "integer"}},
                            "affected_agent_ids": {"type": "array", "items": {"type": "integer"}}
                        },
                        "required": ["project_id", "rationale"]
                    }
                ),
                Tool(
                    name="claim_task",
                    description="Claim an active work lease for a task to avoid duplicate agent work.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "integer"},
                            "agent_id": {"type": "integer"},
                            "session_id": {"type": "integer"},
                            "ttl_seconds": {"type": "integer", "default": 1800}
                        },
                        "required": ["task_id"]
                    }
                ),
                Tool(
                    name="release_task",
                    description="Release a previously claimed task lease.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "lease_id": {"type": "integer"}
                        },
                        "required": ["lease_id"]
                    }
                ),
                Tool(
                    name="summarize_activity",
                    description="Write a concise human-readable summary over a range of activity entries.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "task_id": {"type": "integer"},
                            "agent_id": {"type": "integer"},
                            "summary": {"type": "string"},
                            "from_activity_id": {"type": "integer"},
                            "to_activity_id": {"type": "integer"}
                        },
                        "required": ["project_id", "summary"]
                    }
                ),
                Tool(
                    name="log_contribution",
                    description="Record a user/agent contribution such as a GitHub issue, PR, review, or commit for project visibility.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "entity_id": {"type": "integer"},
                            "contribution_type": {"type": "string", "enum": ["issue", "pull_request", "commit", "review"]},
                            "provider": {"type": "string", "default": "github"},
                            "external_id": {"type": "string"},
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "status": {"type": "string"}
                        },
                        "required": ["project_id", "contribution_type", "title"]
                    }
                ),
                Tool(
                    name="get_project_context",
                    description="Get workspaces, active leases, decisions, summaries, and contributions for a project.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "limit": {"type": "integer", "default": 20}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="get_agent_statuses",
                    description="Get current heartbeats for all agents (manager use).",
                    inputSchema={
                        "type": "object",
                        "properties": {}
                    }
                ),
                Tool(
                    name="get_activity_feed",
                    description="Get recent activity feed, optionally filtered by agent or task.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "agent_id": {"type": "integer", "description": "Filter by agent ID (optional)"},
                            "task_id": {"type": "integer", "description": "Filter by task ID (optional)"},
                            "limit": {"type": "integer", "description": "Max entries to return", "default": 50}
                        }
                    }
                ),
                Tool(
                    name="request_diff_review",
                    description="Request a critical diff review before code changes land. Required for auth, security, subprocess, tmux, and data migration paths.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "task_id": {"type": "integer", "description": "Related task ID (optional)"},
                            "diff_content": {"type": "string", "description": "The diff content to review"},
                            "summary": {"type": "string", "description": "Summary of changes (optional)"},
                            "file_paths": {"type": "string", "description": "Comma-separated list of changed file paths (optional)"},
                            "is_critical": {"type": "boolean", "description": "Whether this diff touches critical code paths", "default": False}
                        },
                        "required": ["project_id", "diff_content"]
                    }
                ),
                Tool(
                    name="review_diff",
                    description="Approve, reject, or request changes on a pending diff review.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "review_id": {"type": "integer"},
                            "status": {"type": "string", "enum": ["approved", "rejected", "changes_requested"]},
                            "review_notes": {"type": "string", "description": "Reviewer comments (optional)"}
                        },
                        "required": ["review_id", "status"]
                    }
                ),
                Tool(
                    name="get_diff_reviews",
                    description="Get pending or recent diff reviews for a project.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "changes_requested"], "description": "Filter by status (optional)"},
                            "limit": {"type": "integer", "default": 20}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="request_approval",
                    description="Request human approval for an action that would otherwise block in a hidden CLI prompt (shell command, file write, network access, git push, PR create, tool call). The agent session is marked blocked until the human resolves the request.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "task_id": {"type": "integer"},
                            "session_id": {"type": "integer", "description": "Agent session that will be blocked until resolved"},
                            "approval_type": {
                                "type": "string",
                                "enum": [
                                    "shell_command", "file_write", "network_access",
                                    "git_push", "pr_create", "tool_call",
                                    "external_access", "other"
                                ]
                            },
                            "title": {"type": "string", "description": "Short label shown in the UI"},
                            "message": {"type": "string", "description": "Original prompt or normalized explanation"},
                            "command": {"type": "string", "description": "Command/tool/action summary (optional)"},
                            "diff_content": {"type": "string", "description": "Proposed patch (optional)"},
                            "payload_json": {"type": "string", "description": "Raw native prompt/event payload (optional)"}
                        },
                        "required": ["project_id", "title", "message"]
                    }
                ),
                Tool(
                    name="get_pending_approvals",
                    description="Fetch pending approval requests, optionally filtered by project, agent, or task.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "agent_id": {"type": "integer"},
                            "task_id": {"type": "integer"},
                            "session_id": {"type": "integer"},
                            "limit": {"type": "integer", "default": 50}
                        }
                    }
                ),
                Tool(
                    name="resolve_approval",
                    description="Approve, reject, or cancel a pending approval. The supervisor uses the result to resume or abort the blocked CLI session.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "approval_id": {"type": "integer"},
                            "decision": {"type": "string", "enum": ["approved", "rejected", "cancelled"]},
                            "response_message": {"type": "string", "description": "Optional human note"}
                        },
                        "required": ["approval_id", "decision"]
                    }
                ),
                Tool(
                    name="get_stage_policies",
                    description="Get stage policies for a project. Policies define expected roles, required outputs, and review mode per stage.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"}
                        },
                        "required": ["project_id"]
                    }
                ),
                Tool(
                    name="record_stage_policy_decision",
                    description="Record an orchestrator decision about a stage transition. Used when the orchestrator moves a card through an explicit decision, including assigned roles and rationale.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "task_id": {"type": "integer"},
                            "from_stage_id": {"type": "integer", "description": "Source stage ID"},
                            "to_stage_id": {"type": "integer", "description": "Target stage ID"},
                            "selected_roles": {"type": "array", "items": {"type": "string"}, "description": "Roles assigned for this stage"},
                            "rationale": {"type": "string", "description": "Why this transition was made"}
                        },
                        "required": ["project_id", "rationale"]
                    }
                ),
                Tool(
                    name="get_transition_validation",
                    description="Check whether a stage transition is valid under project stage policies. Returns validation result with optional rejection reason.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_id": {"type": "integer"},
                            "from_stage_id": {"type": "integer"},
                            "to_stage_id": {"type": "integer"},
                            "move_initiator": {"type": "string", "enum": ["orchestrator", "worker", "human", "owner"], "default": "orchestrator"},
                            "has_required_outputs": {"type": "boolean", "default": true},
                            "has_diff_review": {"type": "boolean", "default": false},
                            "is_critical": {"type": "boolean", "default": false}
                        },
                        "required": ["project_id", "from_stage_id", "to_stage_id"]
                    }
                )
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
            """Handle tool calls from agents"""
            try:
                # Authenticate on first tool call
                await self._authenticate()
                handler = getattr(self, f"_handle_{name}", None)
                if handler:
                    result = await handler(arguments or {})
                    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
                else:
                    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]
            except PermissionError as e:
                logger.warning(f"Permission denied for tool {name}: {e}")
                return [TextContent(type="text", text=json.dumps({"error": f"Permission denied: {e}"}))]
            except Exception as e:
                logger.error(f"Error in tool {name}: {e}")
                return [TextContent(type="text", text=json.dumps({"error": str(e)}))]

    # ========================================================================
    # HELPERS
    # ========================================================================

    # ========================================================================
    # TOOL HANDLERS
    # ========================================================================

    async def _handle_create_project(self, args: dict) -> dict:
        """Create a new project using ORM"""
        self._require_role(Role.MANAGER)
        creator_id = self.caller_entity.id
        async with async_session_maker() as db:
            project = Project(
                name=args["name"],
                description=args.get("description", ""),
                creator_id=creator_id,
                approval_status=ApprovalStatus.PENDING
            )
            db.add(project)
            await db.commit()
            await db.refresh(project)

            # Create default stages
            default_stages = [
                ("Backlog", "Tasks to be done", 1),
                ("To Do", "Ready to start", 2),
                ("In Progress", "Currently being worked on", 3),
                ("Review", "Awaiting review", 4),
                ("Done", "Completed tasks", 5)
            ]

            for name, desc, order in default_stages:
                stage = Stage(name=name, description=desc, order=order, project_id=project.id)
                db.add(stage)

            await db.commit()

            await event_bus.publish(
                EventType.PROJECT_CREATED.value,
                {"project_id": project.id, "name": project.name},
                project_id=project.id
            )

            return {
                "success": True,
                "project_id": project.id,
                "message": f"Project '{project.name}' created successfully",
                "stages": len(default_stages)
            }

    async def _handle_get_projects(self, args: dict) -> list:
        """Get all projects using ORM"""
        async with async_session_maker() as db:
            query = select(Project)
            if "status" in args:
                query = query.filter(Project.approval_status == args["status"].upper())

            result = await db.execute(query.order_by(Project.created_at.desc()))
            projects = result.scalars().all()

            return [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "approval_status": str(p.approval_status),
                    "created_at": p.created_at.isoformat() if p.created_at else None
                }
                for p in projects
            ]

    async def _handle_get_project_details(self, args: dict) -> dict:
        """Get detailed project information using ORM"""
        async with async_session_maker() as db:
            result = await db.execute(
                select(Project)
                .filter(Project.id == args["project_id"])
                .options(
                    selectinload(Project.stages),
                    selectinload(Project.tasks).selectinload(Task.assignees)
                )
            )
            project = result.scalar_one_or_none()

            if not project:
                return {"error": "Project not found"}

            return {
                "project": {
                    "id": project.id,
                    "name": project.name,
                    "description": project.description,
                    "approval_status": str(project.approval_status),
                },
                "stages": [
                    {"id": s.id, "name": s.name, "order": s.order}
                    for s in project.stages
                ],
                "tasks": [
                    {
                        "id": t.id,
                        "title": t.title,
                        "status": str(t.status),
                        "priority": t.priority,
                        "stage_id": t.stage_id,
                        "assignees": [a.name for a in t.assignees]
                    }
                    for t in project.tasks
                ]
            }

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
            task = Task(
                title=args["title"],
                description=args.get("description", ""),
                status=TaskStatus.PENDING,
                project_id=args["project_id"],
                stage_id=stage_id,
                required_skills=args.get("required_skills", ""),
                priority=args.get("priority", 0),
                created_by=creator_id
            )
            db.add(task)
            await db.commit()
            await db.refresh(task)

            await event_bus.publish(
                EventType.TASK_CREATED.value,
                {"task_id": task.id, "title": task.title},
                project_id=task.project_id,
                entity_id=creator_id
            )

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
                    if args.get("agent_id") not in assignee_ids:
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

    async def _handle_approve_project(self, args: dict) -> dict:
        """Approve a project using ORM and publish event"""
        self._require_role(Role.MANAGER)
        async with async_session_maker() as db:
            result = await db.execute(select(Project).filter(Project.id == args["project_id"]))
            project = result.scalar_one_or_none()
            if not project:
                return {"error": "Project not found"}

            project.approval_status = ApprovalStatus.APPROVED
            project.updated_at = datetime.now(UTC)
            await db.commit()

            await event_bus.publish(
                EventType.PROJECT_UPDATED.value,
                {"project_id": project.id, "status": "approved"},
                project_id=project.id
            )

            return {"success": True, "project_id": args["project_id"], "status": "approved"}

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

            # Stage policy transition validation (P0-3)
            old_stage = task.stage_id
            if stage_id and old_stage and stage_id != old_stage:
                try:
                    from kanban_runtime.stage_policy import get_stage_policy_for_stage, validate_transition, gather_transition_context
                    from_policy = await get_stage_policy_for_stage(db, task.project_id, old_stage)
                    to_policy = await get_stage_policy_for_stage(db, task.project_id, stage_id)
                    move_initiator = self.caller_entity.name
                    ctx = await gather_transition_context(db, task_id, task.project_id)
                    transition_warning = validate_transition(
                        from_policy=from_policy,
                        to_policy=to_policy,
                        move_initiator=move_initiator,
                        has_diff_review=ctx["has_diff_review"],
                        has_required_outputs=True,
                        is_critical=ctx["is_critical"],
                    )
                    if transition_warning:
                        return {"error": transition_warning, "transition_blocked": True}
                except ImportError:
                    pass  # stage_policy not available, skip validation

            if stage_id:
                task.stage_id = stage_id
            if status:
                task.status = status
                if status == "completed" and task.completed_at is None:
                    task.completed_at = datetime.now(UTC)

            task.updated_at = datetime.now(UTC)
            await db.commit()

            await event_bus.publish(
                EventType.TASK_MOVED.value,
                {"task_id": task.id, "stage_id": task.stage_id, "status": str(task.status)},
                project_id=task.project_id
            )

            return {"success": True, "task_id": task_id, "new_stage_id": task.stage_id, "status": str(task.status)}

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

            if entity not in task.assignees:
                task.assignees.append(entity)
                await db.commit()

                await event_bus.publish(
                    EventType.TASK_ASSIGNED.value,
                    {"task_id": task.id, "entity_id": entity_id},
                    project_id=task.project_id
                )

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
            await db.commit()
            await db.refresh(comment)

            await event_bus.publish(
                EventType.TASK_COMMENTED.value,
                {"task_id": task.id, "comment": comment.content},
                project_id=task.project_id
            )

            return {"success": True, "comment_id": comment.id, "task_id": args["task_id"]}

    async def _handle_get_pending_events(self, args: dict) -> list:
        """Poll for recent events for an agent (reads from shared DB).

        Uses soft-delete via consumed_at column — events are not deleted on read,
        allowing multi-consumer scenarios. The background sweeper handles cleanup.
        """
        agent_id = self._target_agent_id(args)
        limit = args.get("limit", 50)

        async with async_session_maker() as db:
            result = await db.execute(
                select(PendingEvent)
                .filter(
                    PendingEvent.agent_id == agent_id,
                    PendingEvent.consumed_at.is_(None),
                )
                .order_by(PendingEvent.created_at.asc())
                .limit(limit)
            )
            pending = result.scalars().all()

            events = []
            now = datetime.now(UTC)
            for pe in pending:
                try:
                    payload = json.loads(pe.payload)
                    payload["pending_event_id"] = pe.id
                    events.append(payload)
                    pe.consumed_at = now  # Soft-delete: mark consumed, don't delete
                except Exception as exc:
                    logger.warning(
                        "Skipping PendingEvent id=%s with malformed payload: %s",
                        pe.id, exc,
                    )
                    # Do NOT delete — preserve for debugging / manual cleanup

            await db.commit()

        return events

    async def _handle_register_subscription(self, args: dict) -> dict:
        """Register event subscriptions for an MCP agent"""
        agent_id = self._target_agent_id(args)
        events = args["events"]
        projects = args.get("projects")

        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentConnection).filter(
                    AgentConnection.entity_id == agent_id,
                    AgentConnection.protocol == ProtocolType.MCP
                )
            )
            conn = result.scalar_one_or_none()

            if not conn:
                conn = AgentConnection(
                    entity_id=agent_id,
                    protocol=ProtocolType.MCP,
                    config="{}",
                    status="online"
                )
                db.add(conn)

            conn.subscribed_events = json.dumps(events)
            conn.subscribed_projects = json.dumps(projects) if projects else None
            conn.last_seen = datetime.now(UTC)
            await db.commit()

        return {"success": True, "message": "Subscriptions updated", "events": events}

    async def _handle_list_agents(self, args: dict) -> list:
        """List all registered agents"""
        async with async_session_maker() as db:
            result = await db.execute(
                select(Entity).filter(Entity.entity_type == EntityType.AGENT, Entity.is_active == True)
            )
            agents = result.scalars().all()
            return [
                {"id": a.id, "name": a.name, "skills": a.skills}
                for a in agents
            ]

    async def _handle_list_entities(self, args: dict) -> list:
        """List all entities"""
        async with async_session_maker() as db:
            result = await db.execute(select(Entity).filter(Entity.is_active == True))
            entities = result.scalars().all()
            return [
                {"id": e.id, "name": e.name, "type": str(e.entity_type), "skills": e.skills}
                for e in entities
            ]

    async def _handle_report_status(self, args: dict) -> dict:
        """Upsert agent heartbeat"""
        from models import AgentHeartbeat, AgentStatusType
        agent_id = self._target_agent_id(args)
        status_type = AgentStatusType(args["status_type"])
        message = args.get("message")
        task_id = args.get("task_id")

        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentHeartbeat).filter(AgentHeartbeat.agent_id == agent_id)
            )
            heartbeat = result.scalar_one_or_none()

            if heartbeat:
                heartbeat.status_type = status_type
                heartbeat.message = message
                heartbeat.task_id = task_id
                heartbeat.updated_at = datetime.now(UTC)
            else:
                heartbeat = AgentHeartbeat(
                    agent_id=agent_id,
                    status_type=status_type,
                    message=message,
                    task_id=task_id
                )
                db.add(heartbeat)

            await db.commit()
            await db.refresh(heartbeat)

            await event_bus.publish(
                EventType.AGENT_STATUS_UPDATED.value,
                {
                    "agent_id": agent_id,
                    "status_type": status_type.value,
                    "message": message,
                    "task_id": task_id
                },
                entity_id=agent_id
            )

            return {"success": True, "agent_id": agent_id, "status_type": status_type.value}

    async def _handle_log_activity(self, args: dict) -> dict:
        """Append agent activity log entry"""
        agent_id = self._target_agent_id(args)
        activity_type = ActivityType(args["activity_type"])
        message = args["message"]
        task_id = args.get("task_id")
        project_id = args.get("project_id")
        session_id = args.get("session_id")

        async with async_session_maker() as db:
            if task_id and project_id is None:
                task_result = await db.execute(select(Task).filter(Task.id == task_id))
                task = task_result.scalar_one_or_none()
                if task:
                    project_id = task.project_id

            activity = AgentActivity(
                agent_id=agent_id,
                session_id=session_id,
                project_id=project_id,
                activity_type=activity_type,
                message=message,
                task_id=task_id,
                source=args.get("source"),
                payload_json=args.get("payload_json"),
                workspace_path=args.get("workspace_path"),
                file_path=args.get("file_path"),
                command=args.get("command")
            )
            db.add(activity)
            await db.commit()
            await db.refresh(activity)

            await event_bus.publish(
                EventType.AGENT_ACTIVITY_LOGGED.value,
                {
                    "agent_id": agent_id,
                    "activity_id": activity.id,
                    "session_id": session_id,
                    "project_id": project_id,
                    "activity_type": activity_type.value,
                    "message": message,
                    "task_id": task_id,
                    "source": args.get("source"),
                    "workspace_path": args.get("workspace_path"),
                    "file_path": args.get("file_path"),
                    "command": args.get("command")
                },
                project_id=project_id,
                entity_id=agent_id
            )

            return {"success": True, "activity_id": activity.id}

    async def _handle_start_agent_session(self, args: dict) -> dict:
        """Start a durable CLI-agent session for visibility."""
        agent_id = self._target_agent_id(args)
        project_id = args["project_id"]

        async with async_session_maker() as db:
            project_result = await db.execute(select(Project).filter(Project.id == project_id))
            project = project_result.scalar_one_or_none()
            if not project:
                return {"error": "Project not found"}

            workspace_path = args.get("workspace_path") or project.path
            if not workspace_path:
                return {"error": "workspace_path is required when the project has no path"}

            session = AgentSession(
                agent_id=agent_id,
                project_id=project_id,
                task_id=args.get("task_id"),
                workspace_path=workspace_path,
                command=args.get("command"),
                model=args.get("model"),
                mode=args.get("mode"),
                status=AgentSessionStatus.ACTIVE,
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)

            await event_bus.publish(
                EventType.AGENT_ACTIVITY_LOGGED.value,
                {
                    "agent_id": agent_id,
                    "session_id": session.id,
                    "project_id": project_id,
                    "task_id": session.task_id,
                    "activity_type": "session_started",
                    "message": f"Session started in {workspace_path}",
                    "workspace_path": workspace_path,
                    "command": session.command,
                },
                project_id=project_id,
                entity_id=agent_id
            )

            return {
                "success": True,
                "session_id": session.id,
                "agent_id": agent_id,
                "project_id": project_id,
                "task_id": session.task_id,
                "workspace_path": workspace_path,
                "status": session.status.value,
            }

    async def _handle_end_agent_session(self, args: dict) -> dict:
        """End or update a durable CLI-agent session."""
        session_id = args["session_id"]
        status_value = args.get("status", "done")

        async with async_session_maker() as db:
            result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
            session = result.scalar_one_or_none()
            if not session:
                return {"error": "Session not found"}
            if session.agent_id != self.caller_entity.id:
                self._require_role(Role.MANAGER)

            session.status = AgentSessionStatus(status_value)
            session.last_seen_at = datetime.now(UTC)
            if session.status in (AgentSessionStatus.DONE, AgentSessionStatus.ERROR):
                session.ended_at = datetime.now(UTC)

            if args.get("message"):
                activity = AgentActivity(
                    agent_id=session.agent_id,
                    session_id=session.id,
                    project_id=session.project_id,
                    task_id=session.task_id,
                    activity_type=ActivityType.RESULT if session.status == AgentSessionStatus.DONE else ActivityType.ERROR,
                    source="session_update",
                    message=args["message"],
                    workspace_path=session.workspace_path,
                )
                db.add(activity)

            await db.commit()

            await event_bus.publish(
                EventType.AGENT_STATUS_UPDATED.value,
                {
                    "agent_id": session.agent_id,
                    "session_id": session.id,
                    "project_id": session.project_id,
                    "task_id": session.task_id,
                    "status_type": session.status.value,
                    "message": args.get("message"),
                    "workspace_path": session.workspace_path,
                },
                project_id=session.project_id,
                entity_id=session.agent_id
            )

            return {"success": True, "session_id": session.id, "status": session.status.value}

    async def _handle_get_agent_sessions(self, args: dict) -> list:
        """Get active or recent agent sessions."""
        async with async_session_maker() as db:
            query = select(AgentSession).order_by(AgentSession.last_seen_at.desc())
            if "agent_id" in args:
                query = query.filter(AgentSession.agent_id == args["agent_id"])
            if "project_id" in args:
                query = query.filter(AgentSession.project_id == args["project_id"])
            if "task_id" in args:
                query = query.filter(AgentSession.task_id == args["task_id"])
            if args.get("active_only"):
                query = query.filter(AgentSession.ended_at.is_(None))
            result = await db.execute(query.limit(args.get("limit", 50)))
            sessions = result.scalars().all()
            return [
                {
                    "id": s.id,
                    "agent_id": s.agent_id,
                    "project_id": s.project_id,
                    "task_id": s.task_id,
                    "workspace_path": s.workspace_path,
                    "status": s.status.value,
                    "command": s.command,
                    "model": s.model,
                    "mode": s.mode,
                    "started_at": s.started_at.isoformat() if s.started_at else None,
                    "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                    "last_seen_at": s.last_seen_at.isoformat() if s.last_seen_at else None,
                }
                for s in sessions
            ]

    async def _handle_get_project_activity(self, args: dict) -> list:
        """Get structured orchestration feed for a project."""
        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentActivity)
                .filter(AgentActivity.project_id == args["project_id"])
                .order_by(AgentActivity.created_at.desc())
                .limit(args.get("limit", 100))
            )
            activities = result.scalars().all()
            return [
                {
                    "id": a.id,
                    "agent_id": a.agent_id,
                    "session_id": a.session_id,
                    "project_id": a.project_id,
                    "task_id": a.task_id,
                    "activity_type": a.activity_type.value,
                    "source": a.source,
                    "message": a.message,
                    "workspace_path": a.workspace_path,
                    "file_path": a.file_path,
                    "command": a.command,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in activities
            ]

    async def _handle_record_decision(self, args: dict) -> dict:
        """Record a durable manager decision and rationale."""
        self._require_role(Role.MANAGER)
        project_id = args["project_id"]
        affected_task_ids = args.get("affected_task_ids")
        affected_agent_ids = args.get("affected_agent_ids")
        async with async_session_maker() as db:
            decision = OrchestrationDecision(
                project_id=project_id,
                manager_agent_id=self.caller_entity.id,
                decision_type=DecisionType(args.get("decision_type", "other")),
                input_summary=args.get("input_summary"),
                rationale=args["rationale"],
                affected_task_ids=json.dumps(affected_task_ids) if affected_task_ids is not None else None,
                affected_agent_ids=json.dumps(affected_agent_ids) if affected_agent_ids is not None else None,
            )
            db.add(decision)
            await db.commit()
            await db.refresh(decision)

            await event_bus.publish(
                EventType.ORCHESTRATION_DECISION_LOGGED.value,
                {
                    "decision_id": decision.id,
                    "project_id": project_id,
                    "decision_type": decision.decision_type.value,
                    "rationale": decision.rationale,
                    "affected_task_ids": decision.affected_task_ids,
                    "affected_agent_ids": decision.affected_agent_ids,
                },
                project_id=project_id,
                entity_id=self.caller_entity.id
            )
            return {"success": True, "decision_id": decision.id}

    async def _handle_claim_task(self, args: dict) -> dict:
        """Claim an active task lease."""
        self._require_role(Role.WORKER)
        agent_id = self._target_agent_id(args)
        task_id = args["task_id"]
        now = datetime.now(UTC)
        async with async_session_maker() as db:
            task_result = await db.execute(select(Task).filter(Task.id == task_id))
            task = task_result.scalar_one_or_none()
            if not task:
                return {"error": "Task not found"}

            active_result = await db.execute(
                select(TaskLease).filter(
                    TaskLease.task_id == task_id,
                    TaskLease.status == LeaseStatus.ACTIVE,
                    TaskLease.expires_at > now,
                    TaskLease.agent_id != agent_id,
                )
            )
            active = active_result.scalar_one_or_none()
            if active:
                return {"error": f"Task is already leased by agent {active.agent_id}", "lease_id": active.id}

            existing_result = await db.execute(
                select(TaskLease).filter(
                    TaskLease.task_id == task_id,
                    TaskLease.agent_id == agent_id,
                    TaskLease.status == LeaseStatus.ACTIVE,
                )
            )
            for existing in existing_result.scalars().all():
                existing.status = LeaseStatus.RELEASED
                existing.released_at = now

            lease = TaskLease(
                task_id=task_id,
                agent_id=agent_id,
                session_id=args.get("session_id"),
                status=LeaseStatus.ACTIVE,
                expires_at=now + timedelta(seconds=max(60, args.get("ttl_seconds", 1800))),
            )
            db.add(lease)
            await db.commit()
            await db.refresh(lease)

            await event_bus.publish(
                EventType.TASK_LEASE_UPDATED.value,
                {
                    "lease_id": lease.id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "session_id": lease.session_id,
                    "status": lease.status.value,
                    "expires_at": lease.expires_at.isoformat(),
                },
                project_id=task.project_id,
                entity_id=agent_id
            )
            return {"success": True, "lease_id": lease.id, "expires_at": lease.expires_at.isoformat()}

    async def _handle_release_task(self, args: dict) -> dict:
        """Release a task lease."""
        async with async_session_maker() as db:
            result = await db.execute(select(TaskLease).filter(TaskLease.id == args["lease_id"]).options(selectinload(TaskLease.task)))
            lease = result.scalar_one_or_none()
            if not lease:
                return {"error": "Lease not found"}
            if lease.agent_id != self.caller_entity.id:
                self._require_role(Role.MANAGER)
            lease.status = LeaseStatus.RELEASED
            lease.released_at = datetime.now(UTC)
            await db.commit()

            await event_bus.publish(
                EventType.TASK_LEASE_UPDATED.value,
                {"lease_id": lease.id, "task_id": lease.task_id, "agent_id": lease.agent_id, "status": lease.status.value},
                project_id=lease.task.project_id if lease.task else None,
                entity_id=lease.agent_id
            )
            return {"success": True, "lease_id": lease.id, "status": lease.status.value}

    async def _handle_summarize_activity(self, args: dict) -> dict:
        """Store a summarized activity timeline entry."""
        agent_id = args.get("agent_id") or self.caller_entity.id
        if agent_id != self.caller_entity.id:
            self._require_role(Role.MANAGER)
        async with async_session_maker() as db:
            summary = ActivitySummary(
                project_id=args["project_id"],
                task_id=args.get("task_id"),
                agent_id=agent_id,
                summary=args["summary"],
                from_activity_id=args.get("from_activity_id"),
                to_activity_id=args.get("to_activity_id"),
            )
            db.add(summary)
            await db.commit()
            await db.refresh(summary)
            await event_bus.publish(
                EventType.ACTIVITY_SUMMARY_CREATED.value,
                {"summary_id": summary.id, "project_id": summary.project_id, "summary": summary.summary},
                project_id=summary.project_id,
                entity_id=agent_id
            )
            return {"success": True, "summary_id": summary.id}

    async def _handle_log_contribution(self, args: dict) -> dict:
        """Record a GitHub/SCM contribution."""
        entity_id = args.get("entity_id") or self.caller_entity.id
        if entity_id != self.caller_entity.id:
            self._require_role(Role.MANAGER)
        async with async_session_maker() as db:
            contribution = UserContribution(
                project_id=args["project_id"],
                entity_id=entity_id,
                contribution_type=ContributionType(args["contribution_type"]),
                provider=args.get("provider", "github"),
                external_id=args.get("external_id"),
                title=args["title"],
                url=args.get("url"),
                status=args.get("status"),
            )
            db.add(contribution)
            await db.commit()
            await db.refresh(contribution)
            await event_bus.publish(
                EventType.USER_CONTRIBUTION_LOGGED.value,
                {
                    "contribution_id": contribution.id,
                    "project_id": contribution.project_id,
                    "entity_id": contribution.entity_id,
                    "contribution_type": contribution.contribution_type.value,
                    "title": contribution.title,
                    "url": contribution.url,
                    "status": contribution.status,
                },
                project_id=contribution.project_id,
                entity_id=entity_id
            )
            return {"success": True, "contribution_id": contribution.id}

    async def _handle_get_project_context(self, args: dict) -> dict:
        """Get the coordination context the manager and UI need."""
        project_id = args["project_id"]
        limit = args.get("limit", 20)
        now = datetime.now(UTC)
        async with async_session_maker() as db:
            workspaces = (await db.execute(
                select(ProjectWorkspace).filter(ProjectWorkspace.project_id == project_id).order_by(ProjectWorkspace.is_primary.desc(), ProjectWorkspace.created_at)
            )).scalars().all()
            decisions = (await db.execute(
                select(OrchestrationDecision).filter(OrchestrationDecision.project_id == project_id).order_by(OrchestrationDecision.created_at.desc()).limit(limit)
            )).scalars().all()
            leases = (await db.execute(
                select(TaskLease).join(Task, Task.id == TaskLease.task_id).filter(
                    Task.project_id == project_id,
                    TaskLease.status == LeaseStatus.ACTIVE,
                    TaskLease.expires_at > now,
                ).order_by(TaskLease.created_at.desc())
            )).scalars().all()
            summaries = (await db.execute(
                select(ActivitySummary).filter(ActivitySummary.project_id == project_id).order_by(ActivitySummary.created_at.desc()).limit(limit)
            )).scalars().all()
            contributions = (await db.execute(
                select(UserContribution).filter(UserContribution.project_id == project_id).order_by(UserContribution.recorded_at.desc()).limit(limit)
            )).scalars().all()
            return {
                "workspaces": [
                    {"id": w.id, "root_path": w.root_path, "label": w.label, "is_primary": w.is_primary}
                    for w in workspaces
                ],
                "decisions": [
                    {"id": d.id, "decision_type": d.decision_type.value, "rationale": d.rationale, "created_at": d.created_at.isoformat() if d.created_at else None}
                    for d in decisions
                ],
                "leases": [
                    {"id": l.id, "task_id": l.task_id, "agent_id": l.agent_id, "session_id": l.session_id, "expires_at": l.expires_at.isoformat() if l.expires_at else None}
                    for l in leases
                ],
                "summaries": [
                    {"id": s.id, "task_id": s.task_id, "agent_id": s.agent_id, "summary": s.summary, "created_at": s.created_at.isoformat() if s.created_at else None}
                    for s in summaries
                ],
                "contributions": [
                    {"id": c.id, "entity_id": c.entity_id, "contribution_type": c.contribution_type.value, "title": c.title, "url": c.url, "status": c.status}
                    for c in contributions
                ],
            }

    async def _handle_get_agent_statuses(self, args: dict) -> list:
        """Get all agent heartbeats (manager polling)"""
        self._require_role(Role.MANAGER)
        from models import AgentHeartbeat
        async with async_session_maker() as db:
            result = await db.execute(
                select(AgentHeartbeat).order_by(AgentHeartbeat.updated_at.desc())
            )
            heartbeats = result.scalars().all()
            return [
                {
                    "agent_id": h.agent_id,
                    "status_type": str(h.status_type),
                    "message": h.message,
                    "task_id": h.task_id,
                    "updated_at": h.updated_at.isoformat() if h.updated_at else None
                }
                for h in heartbeats
            ]

    async def _handle_get_activity_feed(self, args: dict) -> list:
        """Get activity feed with optional filters"""
        from models import AgentActivity
        from sqlalchemy import desc
        async with async_session_maker() as db:
            query = select(AgentActivity).order_by(desc(AgentActivity.created_at))
            if "agent_id" in args:
                query = query.filter(AgentActivity.agent_id == args["agent_id"])
            if "task_id" in args:
                query = query.filter(AgentActivity.task_id == args["task_id"])

            limit = args.get("limit", 50)
            result = await db.execute(query.limit(limit))
            activities = result.scalars().all()

            return [
                {
                    "id": a.id,
                    "agent_id": a.agent_id,
                    "activity_type": str(a.activity_type),
                    "message": a.message,
                    "task_id": a.task_id,
                    "created_at": a.created_at.isoformat() if a.created_at else None
                }
                for a in activities
            ]

    async def _handle_request_diff_review(self, args: dict) -> dict:
        """Create a diff review request for critical code paths."""
        project_id = args["project_id"]
        async with async_session_maker() as db:
            review = DiffReview(
                project_id=project_id,
                task_id=args.get("task_id"),
                reviewer_id=None,
                requester_id=self.caller_entity.id,
                diff_content=args["diff_content"],
                summary=args.get("summary"),
                file_paths=args.get("file_paths"),
                is_critical=args.get("is_critical", False),
                status=DiffReviewStatus.PENDING,
            )
            db.add(review)
            await db.commit()
            await db.refresh(review)

            await event_bus.publish(
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
            review.status = new_status
            review.review_notes = args.get("review_notes")
            review.reviewer_id = self.caller_entity.id
            if new_status in (DiffReviewStatus.APPROVED, DiffReviewStatus.REJECTED, DiffReviewStatus.CHANGES_REQUESTED):
                review.reviewed_at = datetime.now(UTC)
            await db.commit()

            await event_bus.publish(
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

    async def _handle_request_approval(self, args: dict) -> dict:
        """Create an approval queue entry blocking the agent session until resolved."""
        project_id = args["project_id"]
        try:
            approval_type = ApprovalType(args.get("approval_type", "other"))
        except ValueError:
            approval_type = ApprovalType.OTHER
        async with async_session_maker() as db:
            approval = AgentApproval(
                project_id=project_id,
                task_id=args.get("task_id"),
                session_id=args.get("session_id"),
                agent_id=self.caller_entity.id,
                approval_type=approval_type,
                title=args["title"],
                message=args["message"],
                command=args.get("command"),
                diff_content=args.get("diff_content"),
                payload_json=args.get("payload_json"),
                status=AgentApprovalStatus.PENDING,
            )
            db.add(approval)
            await db.commit()
            await db.refresh(approval)

            session_id = args.get("session_id")
            if session_id:
                sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == session_id))
                session_row = sess_result.scalar_one_or_none()
                if session_row and session_row.status != AgentSessionStatus.BLOCKED:
                    session_row.status = AgentSessionStatus.BLOCKED
                    session_row.last_seen_at = datetime.now(UTC)
                    await db.commit()

            await event_bus.publish(
                EventType.AGENT_APPROVAL_REQUESTED.value,
                {
                    "approval_id": approval.id,
                    "project_id": project_id,
                    "task_id": approval.task_id,
                    "session_id": approval.session_id,
                    "agent_id": approval.agent_id,
                    "approval_type": approval.approval_type.value,
                    "title": approval.title,
                    "message": approval.message,
                    "command": approval.command,
                },
                project_id=project_id,
                entity_id=approval.agent_id
            )
            return {
                "success": True,
                "approval_id": approval.id,
                "status": approval.status.value,
                "approval_type": approval.approval_type.value,
            }

    async def _handle_get_pending_approvals(self, args: dict) -> list:
        """List pending approvals scoped by project/agent/task/session."""
        async with async_session_maker() as db:
            query = (
                select(AgentApproval)
                .filter(AgentApproval.status == AgentApprovalStatus.PENDING)
                .order_by(AgentApproval.requested_at.desc())
            )
            for field, column in (
                ("project_id", AgentApproval.project_id),
                ("agent_id", AgentApproval.agent_id),
                ("task_id", AgentApproval.task_id),
                ("session_id", AgentApproval.session_id),
            ):
                if field in args and args[field] is not None:
                    query = query.filter(column == args[field])
            limit = args.get("limit", 50)
            result = await db.execute(query.limit(limit))
            approvals = result.scalars().all()
            return [
                {
                    "id": a.id,
                    "project_id": a.project_id,
                    "task_id": a.task_id,
                    "session_id": a.session_id,
                    "agent_id": a.agent_id,
                    "approval_type": a.approval_type.value,
                    "title": a.title,
                    "message": a.message,
                    "command": a.command,
                    "diff_content": a.diff_content,
                    "status": a.status.value,
                    "requested_at": a.requested_at.isoformat() if a.requested_at else None,
                }
                for a in approvals
            ]

    async def _handle_resolve_approval(self, args: dict) -> dict:
        """Resolve an approval. Used by managers/diff reviewers and the supervisor."""
        approval_id = args["approval_id"]
        try:
            decision = AgentApprovalStatus(args["decision"])
        except ValueError:
            return {"error": f"invalid decision: {args.get('decision')}"}
        if decision not in (
            AgentApprovalStatus.APPROVED,
            AgentApprovalStatus.REJECTED,
            AgentApprovalStatus.CANCELLED,
        ):
            return {"error": "decision must be approved, rejected, or cancelled"}

        async with async_session_maker() as db:
            result = await db.execute(select(AgentApproval).filter(AgentApproval.id == approval_id))
            approval = result.scalar_one_or_none()
            if not approval:
                return {"error": "Approval not found"}
            if approval.status != AgentApprovalStatus.PENDING:
                return {"error": f"Approval already {approval.status.value}"}

            if decision == AgentApprovalStatus.CANCELLED:
                if approval.agent_id != self.caller_entity.id:
                    self._require_role(Role.MANAGER)
            else:
                self._require_role(Role.MANAGER)

            # Optimistic concurrency: CAS on update_version
            expected_version = approval.update_version
            from sqlalchemy import update as sa_update
            cas_result = await db.execute(
                sa_update(AgentApproval)
                .where(AgentApproval.id == approval_id, AgentApproval.update_version == expected_version)
                .values(
                    status=decision,
                    resolved_at=datetime.now(UTC),
                    resolved_by_entity_id=self.caller_entity.id,
                    response_message=args.get("response_message"),
                    update_version=expected_version + 1,
                )
            )
            if cas_result.rowcount == 0:
                return {"error": "Approval was resolved by another request (concurrent modification)"}

            if approval.session_id:
                sess_result = await db.execute(select(AgentSession).filter(AgentSession.id == approval.session_id))
                session_row = sess_result.scalar_one_or_none()
                if session_row and session_row.status == AgentSessionStatus.BLOCKED:
                    session_row.status = AgentSessionStatus.ACTIVE
                    session_row.last_seen_at = datetime.now(UTC)

            await db.commit()
            await db.refresh(approval)

            await event_bus.publish(
                EventType.AGENT_APPROVAL_RESOLVED.value,
                {
                    "approval_id": approval.id,
                    "project_id": approval.project_id,
                    "task_id": approval.task_id,
                    "session_id": approval.session_id,
                    "agent_id": approval.agent_id,
                    "approval_type": approval.approval_type.value,
                    "status": approval.status.value,
                    "resolved_by_entity_id": self.caller_entity.id,
                    "response_message": approval.response_message,
                },
                project_id=approval.project_id,
                entity_id=approval.agent_id
            )
            return {
                "success": True,
                "approval_id": approval.id,
                "status": approval.status.value,
            }

    async def _handle_get_stage_policies(self, args: dict) -> dict:
        """Return stage policies for a project."""
        project_id = args.get("project_id")
        if not project_id:
            return {"error": "project_id is required"}
        async with async_session_maker() as db:
            from kanban_runtime.stage_policy import get_stage_policies
            policies = await get_stage_policies(db, project_id)
            from schemas import StagePolicyResponse
            return {"policies": [StagePolicyResponse.from_model(p).model_dump() for p in policies]}

    async def _handle_record_stage_policy_decision(self, args: dict) -> dict:
        """Record an orchestrator decision about a stage transition."""
        self._require_role(Role.MANAGER)
        project_id = args.get("project_id")
        task_id = args.get("task_id")
        from_stage_id = args.get("from_stage_id")
        to_stage_id = args.get("to_stage_id")
        selected_roles = args.get("selected_roles", [])
        rationale = args.get("rationale", "")
        if not project_id or not rationale:
            return {"error": "project_id and rationale are required"}
        async with async_session_maker() as db:
            decision = OrchestrationDecision(
                project_id=project_id,
                manager_agent_id=self.caller_entity.id,
                decision_type=DecisionType.STAGE_POLICY,
                input_summary=f"Stage transition from {from_stage_id} to {to_stage_id}",
                rationale=rationale,
                affected_task_ids=str(task_id) if task_id else None,
                affected_agent_ids=None,
            )
            db.add(decision)
            await db.commit()
            await db.refresh(decision)
            await event_bus.publish(
                EventType.STAGE_POLICY_UPDATED.value,
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "from_stage_id": from_stage_id,
                    "to_stage_id": to_stage_id,
                    "selected_roles": selected_roles,
                    "decision_id": decision.id,
                },
                project_id=project_id,
                entity_id=self.caller_entity.id,
            )
            return {
                "success": True,
                "decision_id": decision.id,
                "message": "Stage transition decision recorded.",
            }

    async def _handle_get_transition_validation(self, args: dict) -> dict:
        """Validate a stage transition against project stage policies."""
        project_id = args.get("project_id")
        from_stage_id = args.get("from_stage_id")
        to_stage_id = args.get("to_stage_id")
        move_initiator = args.get("move_initiator", "orchestrator")
        has_required_outputs = args.get("has_required_outputs", True)
        has_diff_review = args.get("has_diff_review", False)
        is_critical = args.get("is_critical", False)
        if not project_id or not from_stage_id or not to_stage_id:
            return {"error": "project_id, from_stage_id, and to_stage_id are required"}
        from kanban_runtime.stage_policy import get_stage_policy_for_stage, validate_transition
        async with async_session_maker() as db:
            to_policy = await get_stage_policy_for_stage(db, project_id, to_stage_id)
            from_policy = await get_stage_policy_for_stage(db, project_id, from_stage_id)
            error = validate_transition(
                from_policy=from_policy,
                to_policy=to_policy,
                move_initiator=move_initiator,
                has_required_outputs=has_required_outputs,
                has_diff_review=has_diff_review,
                is_critical=is_critical,
            )
            if error:
                return {"valid": False, "reason": error}
            return {"valid": True}

    async def run(self):
        """Run the MCP server"""
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(read_stream, write_stream, self.server.create_initialization_options())


async def main():
    """Main entry point"""
    if not MCP_AVAILABLE:
        print("Error: MCP library not available", file=sys.stderr)
        print("Install with: pip install mcp", file=sys.stderr)
        return 1

    server = KanbanMCPServer()
    await server.run()


if __name__ == "__main__":
    asyncio.run(main())
