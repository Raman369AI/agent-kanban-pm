"""MCP tool definitions (name, description, JSON input schema) for the Kanban server."""


def build_tools(Tool) -> list:
    """Return the tool list, built with the installed ``mcp.types.Tool`` class."""
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
                    "status": {"type": "string", "enum": ["pending", "in_progress", "in_review", "completed", "blocked"]},
                    "override_reason": {"type": "string", "description": "Required for a human override of unsatisfied workflow evidence"}
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
                    "has_required_outputs": {"type": "boolean", "default": True},
                    "has_diff_review": {"type": "boolean", "default": False},
                    "is_critical": {"type": "boolean", "default": False}
                },
                "required": ["project_id", "from_stage_id", "to_stage_id"]
            }
        )
    ]
