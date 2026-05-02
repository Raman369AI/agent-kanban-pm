# Changelog

All notable changes to Agent Kanban PM are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.3.0a1] — 2026-05-02

Alpha release candidate for the local-first, role-based agent runtime.

### Added
- **CLI Approval Queue** — Headless CLI prompts (shell command, file write, network access, git push, PR create, tool call, …) are now bubbled into a durable Kanban approval queue instead of hiding inside a tmux pane.
  - New `agent_approvals` table with `ApprovalType` and `AgentApprovalStatus` enums
  - REST endpoints: `POST /agents/approvals`, `GET /agents/approvals`, `PATCH /agents/approvals/{id}/resolve`
  - MCP tools: `request_approval`, `get_pending_approvals`, `resolve_approval`
  - Events: `AGENT_APPROVAL_REQUESTED`, `AGENT_APPROVAL_RESOLVED`
  - Role supervisor watches `tmux capture-pane` output for known prompt patterns, files an approval, marks the agent session `BLOCKED`, and resumes the CLI by sending `y` / `n` (or the human's response text) via `tmux send-keys` once resolved
  - "Approvals" workbench tab on the kanban board with pending/recent lists, approve/reject controls, and a pending-count badge
- **Per-task terminal binding** — `GET /agents/tasks/{task_id}/active-session` returns the active agent session for a task; each card now has a 🖥️ Terminal button that jumps to the Terminal workbench tab for that session.
- **OS open-folder action** — `POST /ui/api/open-workspace` invokes the platform-native opener (`xdg-open` / `open` / `explorer`) with a strict whitelist (the path must match a known `Project.path` or `ProjectWorkspace.root_path`). Wired to "📂 Open Folder" buttons on the board header and projects list.
- **Adapter Registry** — YAML-driven agent definitions. Drop a `.yaml` file in `~/.kanban/agents/` to register a new tool. No Python changes required.
- **Agent Activity Visibility** — Live heartbeat and activity logging:
  - `AgentHeartbeat` and `AgentActivity` database tables
  - REST endpoints: `GET /agents/status`, `GET /agents/activity`, `POST /agents/{id}/status`, `POST /agents/{id}/activity`
  - MCP tools: `report_status`, `log_activity`, `get_agent_statuses`, `get_activity_feed`
  - Live activity sidebar on the Kanban board with WebSocket updates
  - Background staleness sweeper marks idle heartbeats after timeout
- **Manager Daemon** — `kanban_runtime/manager_daemon.py` spawns the chosen manager CLI tool with:
  - Restart loop with exponential backoff (5s → 300s max)
  - PID file tracking (`~/.kanban/daemon.pid`)
  - Per-session MCP config generation (`~/.kanban/mcp/kanban_mcp.json`)
  - `KANBAN_AGENT_NAME` and `KANBAN_AGENT_ROLE` env vars for spawned processes
- **Preferences System** — `~/.kanban/preferences.yaml` stores manager selection, mode, and worker config
- **`kanban` CLI** — New commands:
  - `python -m kanban_cli init` — Interactive setup wizard
  - `python -m kanban_cli agents list` — Show installed adapters
  - `python -m kanban_cli daemon` — Start manager daemon
  - `python -m kanban_cli daemon status` — Check daemon status
  - `python -m kanban_cli daemon stop` — Stop daemon
- **RBAC** — Enforceable role-based access control:
  - `Role` enum: `OWNER`, `MANAGER`, `WORKER`, `VIEWER`
  - `X-Entity-ID` header support
  - Backward-compat fallback restricted to GET requests only (with logging)
- **Project Approval Flow** — Projects start as `PENDING`; only `MANAGER`/`OWNER` can approve/reject
- **Optimistic Locking** — Tasks have a `version` field; concurrent edits return 409 Conflict
- **Task-Level Access Control** — Workers can only modify tasks they created or are assigned to
- **Automatic TaskLog Audit Trail** — Every task mutation is logged with `created_by` tracking
- **Database Migrations** — `_migrate_db_schema()` adds missing columns (`role`, `created_by`, `version`) and backfills existing data
- **4 Bundled Adapters** — `claude`, `gemini`, `opencode`, `codex`

### Changed
- **Package layout** — Runtime templates, static assets, bundled adapters, and MCP configs now live under `kanban_runtime/data/` so the PyPI wheel contains the files needed by `kanban run`.
- **Release status** — Package version is now `0.3.0a1` and the project is explicitly documented as alpha.
- **Documentation surface** — Architecture notes are consolidated into a compact diagram-first `ARCHITECTURE.md`.
- **Approval workbench UX** — Pending approval badges now open a focused review popup, approval controls are centralized, and warning indicators replace the previous lock icon.
- **Board task creation UX** — Column-level "Add Task" controls are limited to Backlog and To Do stages to avoid adding new work directly into execution/review/done columns.
- **Project PR sync** — `POST /agents/projects/{project_id}/contributions/sync/github` now also syncs reviews authored by the user (`gh search prs --reviewed-by`) and commits (`gh search commits`). When `gh` is missing it falls back to local `git log --author=...`, and `git config user.name` is used as a final author fallback so commit-only sync still works without `gh`.
- **Architecture inversion** — Server is now a "dumb state store." The manager agent owns all routing decisions via MCP tools.
- **Entity naming** — `Entity.name` now stores the adapter `name` (e.g., `claude`), not `display_name`. UI surfaces `display_name` only.
- **Adapter sync** — `sync_agents.py` is now a thin wrapper around `init_adapter_registry()`
- **REST signature consistency** — `POST /agents/{id}/status` now uses body (`AgentStatusUpdate` schema) instead of query params
- **Auth fallback** — Unauthenticated GET requests fall back to first active entity (deprecated, logged). All mutations require headers.
- **MCP auth** — MCP server now reads `KANBAN_AGENT_NAME` from env and looks up `Entity.name`.
- **UI settings endpoint** — `/ui/api/settings` now reads from `preferences.yaml` instead of in-memory autopilot config

### Removed
- **`routers/autopilot.py`** — Server-side autopilot loop deleted; manager agent owns assignment
- **`agent_reactor.py`** — Server-side auto-assignment reactor deleted
- **`_get_default_agent_id()` and `_get_default_human_id()`** — Removed from MCP server; all handlers use authenticated caller
- **In-memory autopilot state** — `auto_pilot_enabled` global removed from `ui.py`
- **Legacy manager file** — removed from the local runtime.
- **Root runtime asset folders** — Root-level `agents/`, `mcp_configs/`, `static/`, and `templates/` were removed after package data was consolidated under `kanban_runtime/data/`.
- **Development-only Markdown docs** — Repo-root agent/development notes and duplicate setup notes were removed from the package surface.

### Fixed
- PyPI build metadata now includes project URLs, package data, and a package-driven version.
- Wheel/sdist builds include runtime templates/static assets/adapters while excluding local test and workbench artifacts.
- RBAC bypass via missing headers fixed (GET-only fallback)
- Activity events now derive `project_id` from `task_id` for per-project filtering
- Adapter loader now verifies CLI availability via `shutil.which()` and marks missing tools `is_active=False`
- Heartbeat staleness reaped by background sweeper task

---

## [0.2.0] — 2025-04-25

### Added
- **Kanban Board Overhaul** — Horizontal layout, premium styling, 500 error fixes (`joinedload` & template syntax)
- **Registered Users View** — Team modal on the Kanban board
- **Folder-as-Project** — `open_project.py` registers local directories as Kanban projects
- **Autopilot** — Background loop that auto-assigns unassigned pending tasks every 5 seconds
- **Agent Reactor** — Event-driven auto-assignment (SQLite-limited)
- **Event Bus** — Async pub/sub with WebSocket + Webhook broadcast adapters
- **A2A (Agent-to-Agent)** — Handoff, delegation, and messaging between agents
- **MCP Server** — stdio MCP server for Claude Code, Codex, OpenCode, Gemini CLI
- **Pre-made MCP configs** — `mcp_configs/` for each supported tool
- **WebSocket real-time updates** — Live task movement on the Kanban board

### Changed
- Major UI refactor with gradient badges, drag-and-drop, toast notifications

---

## [0.1.0] — 2025-04-24

### Added
- Initial release: Agent Kanban PM
- FastAPI backend with SQLite database
- Basic Kanban board with stages (Backlog, To Do, In Progress, Review, Done)
- Task CRUD with assignees and comments
- Entity registration (humans and agents)
- REST API for projects, stages, tasks, comments, entities
- WebSocket support for real-time project updates
