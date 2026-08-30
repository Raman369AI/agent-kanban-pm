# Changelog

All notable changes to Agent Kanban PM are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Security
- **Closed the `/ui` auth bypass** — All `/ui/*` mutations (task create/edit/delete/move, project create/edit/delete, role assign, open-workspace) and every `/ui/api/*` JSON endpoint now require the Kanban token. Only HTML page GETs remain exempt so a browser can load the UI and receive the auth cookie.
- **CSRF + hardened auth cookie** — The `kanban-token` cookie is now `HttpOnly` with `SameSite=strict`. Cookie-authenticated mutations must also send `X-CSRF-Token`, an HMAC of the instance token embedded in every page as a meta tag and attached automatically by a `fetch` wrapper in `base.html`. Header-authenticated callers (`X-Kanban-Token`, used by the CLI and role supervisor) are unaffected.
- **Host-header validation** — `TrustedHostMiddleware` rejects requests whose `Host` is not `localhost`/`127.0.0.1` before routing, closing the DNS-rebinding path against the loopback UI. `KANBAN_ALLOWED_HOSTS` whitelists additional hostnames.
- **Timing-safe token comparison** — The auth token was compared with `!=` in the HTTP middleware and the WebSocket handshake, leaking match length through timing. All three comparisons (header token, cookie token, CSRF token) now go through a shared `tokens_match()` helper using `hmac.compare_digest` over encoded bytes, which also turns a non-ASCII token header into a clean 401 instead of a 500.
- **Token file permissions** — `~/.kanban/token` is created `0600` (and the directory `0700`); pre-existing files with looser permissions are tightened on read.
- **Supervised-by-default agent autonomy** — Bundled adapter CLIs no longer launch with bypass flags by default. `--permission-mode bypassPermissions` / `--dangerously-skip-permissions` / `--full-auto` / `--auto` / `--yes-always` moved from `task_command.args` to `task_command.auto_args` in the adapter YAMLs and are appended only for roles with `autonomy: auto` in `preferences.yaml` (default `supervised`; unknown values fall back to supervised). `kanban init` asks once, loudly, before enabling AUTO for all roles; `kanban roles assign --autonomy` and the roles UI API accept the knob. Task prompts and the launch activity payload record the effective autonomy.

### Added
- **Antigravity CLI replaces the retired Gemini CLI** — Google shut Gemini CLI down for consumer accounts on 2026-06-18. The `antigravity` adapter (`agy`) is corrected against the real binary: `--print` for non-interactive runs, `--add-dir` for the workspace, and `--dangerously-skip-permissions` as the auto-mode flag (it previously carried Codex's `--full-auto` and a `--model`/`--mcp` flag `agy` does not accept). `agy` is now what `kanban agents discover` looks for.
- **Adapter deprecation metadata** — `deprecated`, `deprecation_note`, and `replaced_by` on `AdapterSpec`. The `gemini` adapter is marked deprecated rather than deleted, since Gemini Code Assist Standard/Enterprise licences still work: it is hidden from `kanban init` and `kanban agents discover`, shown as `deprecated` in `kanban agents list`, and warns when assigned.
- **PTY execution for terminal-only CLIs** — `chat_designer.requires_tty` runs an adapter on a pseudo-terminal instead of a pipe. `agy --print` writes nothing when stdout is not a TTY (it blocks until the timeout and exits 0 with zero bytes), which made it unusable as an orchestrator; output is drained continuously, ANSI-stripped, and CRLF-normalised.
- **OpenCode adapter corrected** — Now uses `opencode run --dir {workspace} {prompt}` with `--auto` as the auto-mode flag. The previous definition passed the literal filename `.kanban_task.md` as the message, so the agent never received the task.
- **Schema upgrade tests** — `tests/test_db_upgrade.py` builds a pre-migration database, upgrades it, and asserts every migrated column arrives, rows survive, roles backfill, the workspace backfill runs once, the migration chain is recorded, and re-running changes nothing. Nothing previously exercised the upgrade path: every other test starts from an empty database where the migrations are no-ops.
- **Phase 1 packaging completion** — MCP is now an installed dependency with a `kanban-mcp` entry point, Python 3.13 is tested, and Linux/macOS support is explicit.
- **Collision-safe src package layout** — Application, CLI, routers, runtime, MCP server, and packaged assets now live under `src/agent_kanban_pm/`; installed distributions expose only the `agent_kanban_pm` namespace.
- **Per-task git worktrees on real branches** — Each agent session now runs on a `kanban/task-{id}-{agent}` branch started from the detected base ref (`origin/HEAD` → `origin/main` → `origin/master` → `main` → `master`) instead of a detached `HEAD`. This unblocks the eventual merge-back path (PR or `merge --ff-only`).
- **Launch-time rebase** — Before the tmux session starts, the worktree is `fetch`ed and `rebase`d onto the base. If the worktree is dirty, no base is found, or the rebase conflicts, the launcher logs the reason as an `AgentActivity` and leaves the worktree untouched. Records `branch`, `base_ref`, and `base_sync` in the activity payload for audit.
- **`SchedulingConfig` in preferences** — Caps on per-agent active tasks and per-project parallel implementation (defaults: 1/1, review parallelism allowed). Surfaced via the existing `_scheduling_blocker` path.
- **Preferences cache with mtime invalidation** — Repeated `load_preferences()` calls inside a single launch no longer re-parse `preferences.yaml`; the cache busts on file mtime change or 5-second TTL.
- **`PROJECT_REJECTED` event type** — Project rejection now emits its own event instead of reusing `PROJECT_APPROVED`.
- **`kanban_runtime/default_stages.py`** — Single source of truth for the default Backlog/To Do/In Progress/Review/Done stages, consumed by `mcp_server.py`, `routers/projects.py`, and `routers/ui.py`.
- **Tests** — `tests/test_worktree_integration.py` (20 cases): locks in the auto-mode CLI flags, exercises base-ref detection, branch reuse, rebase sync (clean / dirty / conflict / no-base), and verifies the launcher wires the new helpers. Adds a scheduling-blocker test to `test_roles_and_reviews.py`.

### Changed
- **Per-task session stage handoff** — Completed worker sessions now advance the task from To Do/In Progress to Review, and completed review sessions (`test` / `diff_review`) advance Review tasks to Done. Stage-entry policy roles are assigned and emitted as `TASK_ASSIGNED` events so the next agent can continue the chain.
- **Role-specific launch gates** — Assigned `test` and `diff_review` agents can now launch from Review, and `git_pr` agents can launch from Done; implementation roles remain limited to To Do/In Progress.
- **Bundled adapter CLIs declare auto-approval flags separately** — `claude` declares `--permission-mode bypassPermissions`, `antigravity` `--dangerously-skip-permissions`, `codex` `--full-auto`, `opencode` `--auto`, `aider` `--yes-always` as `task_command.auto_args` instead of inline in `task_command.args`. Supervised remains the default; see the Security section for the opt-in model.
- **Agent prompt template** — Autonomy guidance now follows the role's `autonomy` setting: auto mode tells the agent to operate autonomously and record risky actions in `STATUS.md`; supervised mode tells it to stop at CLI approval prompts, which are answered through the Kanban approval queue.
- **SQLite pragmas on startup** — `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON` are applied in `init_db()` for safer concurrent writes under the local-first runtime.
- **Heartbeat sweeper** — Single JOIN query returns `(AgentHeartbeat, Entity.name)` rows instead of an N+1 fetch per heartbeat.
- **Pending-event sweeper** — Uses `DELETE … WHERE` with a single round-trip; reports purge count from `rowcount`.
- **Board header UI** — Simpler title row with inline project meta (status badge + path), a tighter chat input, and an `<details>` "Advanced" disclosure for less-used actions (Open folder, Team roles, Stage policy, Notifications, GitHub sync, Git view).
- **WebSocket / connection-manager logging** — `print(...)` replaced with `logger.warning(...)`.

### Fixed
- **Tmux collaboration not crossing Review/Done** — Worker tmux sessions previously only marked the `AgentSession` done and logged a handoff, leaving cards stuck in In Progress. Session completion now updates the board stage/status and publishes task movement/update events.
- **Connection status enum comparisons** — Event delivery paths now compare `AgentConnection.status` with `ConnectionStatus.ONLINE` instead of raw string values, keeping MCP pending-event persistence and adapter dispatch on the same enum contract.
- **Pydantic / Starlette deprecations** — Response schemas now use `ConfigDict(from_attributes=True)`, and UI templates use the current `TemplateResponse(request, name, context)` call order.
- **`_actor_id()` in `routers/stages.py` and `routers/tasks.py`** — No longer falls back to a fake `id=1` when the entity is missing; returns `None` so audit-trail rows stay accurate.
- **Project rejection event** — Previously published `PROJECT_APPROVED` on reject.
- **Database indexes** — `agent_activities.id`, `agent_activities.task_id`, and `pending_events.consumed_at` are now indexed (faster sweeper and per-task activity queries).
- **`routers/ui.py` list-comprehension indentation** — Cleaned up after the removal of `_is_noisy_project()`.

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
- Pytest now uses a per-run temporary SQLite database and disposes it after cleanup, avoiding shared `kanban.db` state and stale aiosqlite event-loop warnings.
- MCP `get_tasks(assigned_to_me=true)` now filters against the authenticated caller instead of an undeclared `agent_id` argument.
- Kanban board task create/edit/assign and WebSocket task updates refresh the board region without forcing a full page reload.

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
