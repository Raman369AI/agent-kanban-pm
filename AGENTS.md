# AGENTS.md — Working Spec for Hybrid Agentic PM

This document is the source of truth for what the Agent Kanban PM system
is, how it's built, and what's still open. Read this before touching the
codebase. Architecture decisions live here; implementation details live
in the code.

---

## 1. What This System Is

A local-first project management system for hybrid teams of humans and
AI agents. The server is a thin state-store + event bus. The chosen
"manager" agent is what actually decides who does what — server-side
Python no longer makes orchestration decisions.

```
┌─────────────────────────────────────────┐
│  Kanban Server (dumb state store)       │
│  ─ SQLite                               │
│  ─ REST + WebSocket + MCP               │
│  ─ Event bus (publish only, no logic)   │
│  ─ Adapter registry loader              │
│  ─ Heartbeat staleness sweeper          │
└──────────────────┬──────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
  Manager Agent          Worker Agents
  (one chosen by user)   (any registered tool)
  ─ owns the board       ─ poll for assigned tasks
  ─ assigns work         ─ report status + activity
  ─ talks to human       ─ never make routing decisions
```

**Inversion of control**: server stops deciding. Manager agent decides.
Server records state and broadcasts events.

---

## 2. Target Operating Model — Role-Based Headless Agent PM

The system should support a user-selected team of CLI agents, all running
headlessly, with explicit project roles. The user chooses which available
CLI agent fills each role, and roles may be mapped to the same adapter
when desired.

```
┌───────────────────────────────────────────────────────────┐
│ Local Kanban Runtime                                      │
│ ─ one command starts server + UI + agent supervisor        │
│ ─ discovers available CLI adapters                        │
│ ─ starts each selected role in a separate tmux session     │
│ ─ captures terminal output per task/session               │
└──────────────────────┬────────────────────────────────────┘
                       │
        ┌──────────────┴────────────────┐
        ▼                               ▼
  Orchestrator Agent              Specialist Agents
  ─ owns board movement           ─ UI Agent
  ─ assigns/preassigns tasks      ─ Architecture Agent
  ─ records decisions             ─ Worker Agent(s)
  ─ writes task feedback          ─ Test Writer/Checker
  ─ routes PR/review flow         ─ Diff Checker/Reviewer
                                  ─ Git PR Agent
```

### Required roles

| Role | Responsibility | Routing rule |
|---|---|---|
| Orchestrator Agent | Owns the board, task splitting, assignment, card movement, feedback, and escalation. | Exactly one active orchestrator per project. |
| UI Agent | Implements frontend views, UX polish, and project-local UI workflows. | Receives UI-tagged tasks or orchestrator handoffs. |
| Architecture Agent | Writes design notes, checks architectural fit, and updates `AGENTS.md` when the system model changes. | Must review cross-cutting changes before heavy implementation. |
| Worker Agent | Writes the bulk of implementation code. Multiple worker agents are allowed and should run separately. | Gets bounded implementation tasks and reports activity continuously. |
| Test Writer/Checker | Adds tests, runs smoke/regression checks, and reports failures back to the board. | Must verify every new MCP tool, task movement path, terminal path, and PR sync path. |
| Diff Checker/Reviewer | Reviews diffs for behavior risk. Shows the human the diff before critical code lands. | Must review critical code, security-sensitive code, auth, subprocess/tmux, and data migrations. |
| Git PR Agent | Creates branches/commits/PRs only when explicitly assigned to the Git role. | No other role should open PRs unless the user overrides this. |

The role assignment model belongs in user preferences, not hardcoded
Python. This is the local working team; adapter YAMLs and discovered CLI
tools are only candidates until they are assigned here:

```yaml
roles:
  orchestrator:
    agent: claude
    model: claude-sonnet-4-6
    models: [claude-sonnet-4-6]
    mode: headless
  ui:
    agent: codex
    model: codex-default
    models: [codex-default]
    mode: headless
  architecture:
    agent: claude
    model: claude-sonnet-4-6
    models: [claude-sonnet-4-6]
    mode: headless
  worker:
    agent: gemini
    model: gemini-2.5-pro
    models: [gemini-2.5-pro]
    mode: headless
  test:
    agent: codex
    model: codex-default
    models: [codex-default]
    mode: headless
  diff_review:
    agent: claude
    model: claude-sonnet-4-6
    models: [claude-sonnet-4-6]
    mode: headless
  git_pr:
    agent: gh
    command: gh
    model: default
    models: [default]
    mode: headless
```

`model` is the default model passed to a CLI when the adapter supports a
model flag. `models` is the allowed/display list for that role. Adapter YAML
models provide defaults; standalone CLI roles can provide their list through
`roles assign --model ... --models ...`.

### Runtime requirements

- **All selected agents run headless.** `supervised` and `auto` remain
  valid config values for compatibility, but the target PM workflow uses
  `headless` for all selected CLI-agent roles.
- **Role assignments may use standalone CLIs.** A role can point at an
  adapter name or directly at a local CLI command. Standalone CLI roles
  are stored in preferences and launched in tmux, but they do not imply a
  provider key, a Kanban-issued key, or a second human/user account.
- **Adapters and discovered CLIs are candidates.** The active team is the
  role assignment list, not every adapter YAML and not every CLI found on
  `PATH`. Adapter sync creates/updates agent entities for configured roles by
  default. `KANBAN_REGISTER_ALL_ADAPTERS=1` explicitly restores eager
  registration of every adapter YAML.
- **CLI discovery is read-only.** The runtime may check whether popular
  CLIs such as Claude, Gemini, Codex, OpenCode, Aider, Goose, Crush,
  Continue, Amp, Cursor Agent, or Qwen are installed. Discovery must not
  register them as agents or assume the user has authenticated them.
- **Separate workers.** Each worker role instance gets its own process,
  session row, heartbeat, lease, and terminal stream.
- **tmux-backed execution.** The supervisor may use tmux so every
  role/task can be inspected, detached, resumed, and terminated without
  losing terminal history.
- **Per-task terminal view.** Every executing task should expose the
  active agent session and ordered terminal/activity stream in the UI.
- **Assignment launches execution.** A task assignment event must start
  the assigned CLI agent in a tmux-backed task session when the project
  has a valid local workspace. The launcher records the session, lease,
  heartbeat, and activity before handing control to the CLI. This is an
  execution bridge only; it must not choose which agent gets the task.
- **Approval queue for CLI prompts.** When a headless CLI agent asks for
  permission (run shell command, edit files, access network, create PR,
  push branch, apply diff, etc.), the runtime must bubble that prompt
  into a durable Kanban approval queue instead of leaving it hidden in a
  tmux pane. The agent session waits until a human approves/rejects, then
  the supervisor resumes the CLI with the appropriate stdin response.
- **Agents move cards and write feedback.** Card movement and task
  comments are explicit MCP/REST actions performed by the orchestrator
  or assigned agent, then recorded as durable activity.
- **Critical diff visibility.** Before critical code changes are
  considered ready, the Diff Checker/Reviewer must show the relevant
  diff to the human and record a review result.
- **Git PR isolation.** Branch, commit, push, and pull-request creation
  are assigned only to the Git PR Agent. The PR Agent must associate the
  PR with the project and task.

### UI requirements

- **Agent picker.** The setup UI/CLI must list every discovered adapter,
  including inactive adapters, and explain why inactive adapters cannot
  currently run.
- **Role preassignment.** Users can preassign adapters to the roles
  above at project setup and can override role assignment per task.
- **Local project access.** Project create/edit must make local folder
  selection obvious. The board should show the active workspace path and
  offer a direct "open local project" action where the OS permits it.
- **Chatbot task creation.** The board needs a chat-style input where
  the human can describe work in natural language. The orchestrator turns
  that into one or more tasks, asks for clarification only when needed,
  then places cards on the board.
- **PR sync button.** Each project must expose a button to sync all PRs,
  issues, reviews, and commits associated with the project workspace.
  Sync should prefer the local GitHub CLI/session (`gh`) or existing git
  remotes. The Kanban app must not store GitHub tokens in the local-first
  path.

### Approval queue requirements

Approval handling is separate from diff review. Diff review answers
"is this code acceptable?" Approval queue answers "may this running CLI
process continue with this requested action?"

The implementation should add an `agent_approvals` table:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `project_id` | FK projects | project context |
| `task_id` | FK tasks nullable | task context |
| `session_id` | FK agent_sessions | CLI run waiting for approval |
| `agent_id` | FK entities | requesting agent |
| `approval_type` | enum/text | shell_command, file_write, network_access, git_push, pr_create, tool_call, external_access, other |
| `title` | text | short UI label |
| `message` | text | original prompt or normalized explanation |
| `command` | text nullable | command/tool/action summary |
| `diff_content` | text nullable | proposed patch, if applicable |
| `payload_json` | text nullable | raw native prompt/event |
| `status` | enum | pending, approved, rejected, expired, cancelled |
| `requested_at` | datetime | |
| `resolved_at` | datetime nullable | |
| `resolved_by_entity_id` | FK entities nullable | human/entity that decided |
| `response_message` | text nullable | optional human note |

Required APIs/MCP tools:

- `request_approval(...)` — called by structured agent hooks or the
  supervisor when a CLI prompt is detected.
- `get_pending_approvals(project_id?, agent_id?, task_id?)` — used by
  UI and orchestrator.
- `resolve_approval(approval_id, decision, response_message?)` —
  approves/rejects and emits the resume signal.
- REST equivalents under `/agents/approvals...`.

Required events:

- `AGENT_APPROVAL_REQUESTED`
- `AGENT_APPROVAL_RESOLVED`

Supervisor behavior:

1. Prefer structured hooks/events from CLIs when available.
2. Fall back to PTY/tmux capture for known prompt patterns.
3. Mark the agent session `waiting` while an approval is pending.
4. Write the human decision back to the CLI stdin (`y`, `n`, selected
   option, or adapter-specific response).
5. Log the prompt, decision, and resumed action in `agent_activities`.

UI behavior:

- Add an `Approvals` workbench tab with pending and recent decisions.
- Show project, task, agent, session, prompt text, command, and diff
  preview when available.
- Provide approve/reject controls with an optional note.
- Link each approval to the terminal session that is blocked.

### Single-command startup

The target developer command should start the whole local system:

```bash
python -m kanban_cli run
```

That command should:

1. Initialize/sync adapter registry.
2. Start the FastAPI server and WebSocket event bus.
3. Start the UI.
4. Start the role supervisor.
5. Start configured headless role agents in separate tmux sessions.
6. Print the local UI URL and active tmux session names.

The command must fail clearly when required local tools are missing
instead of silently degrading.

### Access and secrets rule

The local-first path must never create or store Kanban-issued API keys.
Provider credentials remain outside the app in the user's shell, OS
credential store, or the chosen CLI's own auth mechanism. GitHub/SCM
sync must use local CLI credentials where possible and must not add a
new Kanban token store.

---

## 3. The Core Pillars

### Pillar A — Adapter Registry (extensibility)

Every tool is described by a YAML file. Drop a new file, get a new
agent — no Python changes.

```
~/.kanban/
  preferences.yaml          # user choices
  agents/                   # adapter registry
    claude.yaml             # bundled
    gemini.yaml             # bundled
    opencode.yaml           # bundled
    codex.yaml              # bundled
    my-custom-tool.yaml     # user-added
```

Adapter spec (`~/.kanban/agents/<name>.yaml`):

```yaml
name: claude
display_name: "Claude (Anthropic)"
version: "1.0"

invoke:
  command: claude
  mcp_flag: "--mcp"
  info_flag: "--version"

capabilities: [code, review, planning, testing, writing]

models:
  - id: claude-sonnet-4-6
    context_window: 200000

protocol: mcp                  # mcp | websocket | webhook | stdio

auth:
  type: env_key
  env_var: ANTHROPIC_API_KEY   # the key the CLI tool needs in env

roles: [manager, worker]
modes: [supervised, auto, headless]

reporting:
  heartbeat_interval: 30       # seconds — workers must call report_status this often
```

User preferences (`~/.kanban/preferences.yaml`):

```yaml
manager:
  agent: claude                # adapter `name`, not display_name
  model: claude-sonnet-4-6
  mode: auto                   # supervised | auto | headless

workers:
  - agent: gemini
    roles: [analysis, summarization]
  - agent: opencode
    roles: [code, testing]

autonomy:
  require_approval_for: [project_create, agent_add]
  auto_approve: [task_move, task_assign, comment]
```

Loader behavior (`kanban_runtime/adapter_loader.py`):
- Scans `~/.kanban/agents/*.yaml` on startup
- Validates each via Pydantic
- Checks `shutil.which(spec.invoke.command)` — adapters whose CLI is
  not installed are still loaded but `Entity.is_active=False`
- Stores `Entity.name = spec.name` (not `display_name`); UI surfaces
  `display_name`

CLI:
```bash
python -m kanban_cli init           # interactive setup wizard
python -m kanban_cli agents list    # show installed adapters
python -m kanban_cli daemon         # start the manager daemon
```

### Pillar B — Agent Activity Visibility

Two tables so the manager (and humans) see what every worker is doing.

`agent_heartbeats` — current state, one row per agent (upserted):

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `agent_id` | FK entities (unique) | |
| `task_id` | FK tasks (nullable) | what the agent is currently on |
| `status_type` | enum | idle, thinking, working, blocked, waiting, done |
| `message` | text | human-readable description |
| `updated_at` | datetime | sweeper marks stale rows `idle` after 60s |

`agent_activities` — append-only log:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `agent_id` | FK entities | |
| `task_id` | FK tasks (nullable) | |
| `activity_type` | enum | thought, action, observation, result, error |
| `message` | text | |
| `created_at` | datetime | |

Event types in `event_bus.py`:
- `AGENT_STATUS_UPDATED`
- `AGENT_ACTIVITY_LOGGED`

MCP tools (`mcp_server.py`) — workers call these constantly:
- `report_status(message, task_id?, status_type?)`
- `log_activity(message, task_id?, activity_type?)`
- `get_agent_statuses()` — manager polls this
- `get_activity_feed(agent_id?, task_id?, limit?)`

REST endpoints (`routers/agent_activity.py`):
- `GET /agents/status`
- `GET /agents/activity?agent_id=&task_id=&limit=`
- `POST /agents/{id}/status` and `/activity` (server-side fallback)

UI: live activity sidebar on the kanban board, populated via WebSocket
on `AGENT_STATUS_UPDATED` and `AGENT_ACTIVITY_LOGGED`.

Background task (`main.py:_heartbeat_sweeper`): runs every 60s, marks
heartbeats older than the threshold as `idle`, emits
`AGENT_DISCONNECTED`.

### Pillar C — Headless Manager-Owned PM

The chosen manager agent runs as a daemon. It owns all routing
decisions via MCP tool calls. The server runs no orchestration logic.

What's gone:
- `routers/autopilot.py` — deleted
- `agent_reactor.py` — deleted
- Hardcoded tool list in `sync_agents.py` — replaced by adapter scanner

What's added:
- `kanban_runtime/adapter_loader.py` — scans YAMLs, upserts Entity rows
- `kanban_runtime/preferences.py` — loads `preferences.yaml`
- `kanban_runtime/manager_daemon.py` — spawns the manager CLI tool with
  restart loop (exponential backoff), signal handling, PID file
- `kanban_cli/` — `init`, `agents list`, `daemon` subcommands

### Pillar D — Workspace-Aware Agent Visibility

Projects are tied to a selectable local folder (`Project.path`) so
humans can see where agent work is happening. Agent work is recorded as
durable sessions and structured activity events:

`agent_sessions` — one row per CLI-agent run:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `agent_id` | FK entities | agent process/session owner |
| `project_id` | FK projects | project whose folder is being worked on |
| `task_id` | FK tasks (nullable) | current task, if scoped |
| `workspace_path` | text | folder/worktree path used by the agent |
| `status` | enum | starting, active, idle, blocked, done, error |
| `command` | text | CLI command or wrapper invocation |
| `model` | text | model used, if known |
| `mode` | text | supervised, auto, or headless |
| `started_at`, `ended_at`, `last_seen_at` | datetime | session lifecycle |

`agent_activities` now supports structured fields in addition to the
human-readable `message`:

| Column | Type | Notes |
|---|---|---|
| `session_id` | FK agent_sessions (nullable) | ties event to a CLI run |
| `project_id` | FK projects (nullable) | project-level feed |
| `source` | text | claude_hook, codex_event, gemini_stdout, mcp, etc. |
| `payload_json` | text | raw native event payload |
| `workspace_path` | text | folder/worktree where event occurred |
| `file_path` | text | file touched or inspected |
| `command` | text | command/tool call summary |

MCP tools:
- `start_agent_session(project_id, task_id?, workspace_path?, command?, model?, mode?)`
- `end_agent_session(session_id, status?, message?)`
- `log_activity(...)` with `project_id`, `session_id`, `source`,
  `workspace_path`, `file_path`, `command`, and `payload_json`
- `get_agent_sessions(agent_id?, project_id?, task_id?, active_only?, limit?)`
- `get_project_activity(project_id, limit?)`

REST endpoints:
- `GET /agents/sessions?project_id=&agent_id=&task_id=&active_only=`
- `POST /agents/{id}/sessions`
- `PATCH /agents/sessions/{session_id}`
- `GET /agents/activity?project_id=&session_id=&agent_id=&task_id=&limit=`

UI: project create/edit exposes the project folder. The kanban board
shows the folder, active agent sessions, and a project-scoped activity
feed so humans can inspect what is happening and where it is happening.

### Pillar E — Durable Coordination State

Manager decisions remain agent-owned, but the server now records the
coordination facts that humans and replacement managers need to recover
context:

- `project_workspaces` — one or more local folders/repos per project,
  with `is_primary`, allow patterns, and block patterns.
- `orchestration_decisions` — append-only manager rationale for task
  assignment, reassignment, splits, approvals, priority changes, and
  handoffs.
- `task_leases` — active work claims so assignment and current execution
  are separate concepts.
- `activity_summaries` — human-readable rollups over noisy activity
  streams.
- `user_contributions` — GitHub/SCM artifacts such as PRs, issues,
  reviews, and commits associated with a project and entity.

MCP tools:
- `record_decision(...)`
- `claim_task(task_id, session_id?, ttl_seconds?)`
- `release_task(lease_id)`
- `summarize_activity(...)`
- `log_contribution(...)`
- `get_project_context(project_id, limit?)`

REST/UI:
- Project workspaces, decisions, summaries, contributions, and leases
  are exposed under `/agents/projects/{project_id}/...`.
- `GET /agents/sessions/{session_id}/terminal` returns the session and
  ordered activity stream for terminal-style inspection.
- The kanban board sidebar is a tabbed workbench: Live, Terminal,
  Decisions, and GitHub.

---

## 4. Identity Model — Env-Based, NOT Key-Based

This is local-first. The user's CLI tools already authenticate to their
model providers via env vars (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`,
`OPENAI_API_KEY` …). **The Kanban server does not issue its own API
keys** — that would be duplication of secrets the user already
manages.

Identity to the Kanban server comes from process env vars set by the
manager daemon when spawning each agent:

| Env var | Set by | Read by | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` (etc.) | user's shell | the spawned CLI tool | Provider auth, declared in adapter YAML's `auth.env_var`, inherited via `os.environ.copy()` in the daemon |
| `KANBAN_AGENT_NAME` | manager daemon | MCP server | "Which adapter spawned this process?" — looked up against `Entity.name` |
| `KANBAN_AGENT_ROLE` | manager daemon | MCP server | `manager` or `worker` — applies RBAC at the tool layer |
| `KANBAN_API_BASE` | manager daemon | MCP/REST clients | Where the Kanban server lives (default `http://localhost:8000`) |

**Trust boundary**: the OS process. Only the local user can set env
vars on processes they own. No cryptographic secret needed.

**Humans** authenticating to the REST UI use `X-Entity-ID` (or session
cookie). The deprecated default-entity fallback in `auth.py:91` is now
restricted to safe `GET` requests with a logged warning — mutations
require explicit identity.

**For future remote/multi-user mode** (opt-in only):
- Add `KANBAN_AGENT_TOKEN` env var for cross-host identity
- The existing `Entity.api_key` field can be populated then
- Default local install never uses it

---

## 5. Implementation Status

### Done

| Pillar / Task | Status | Key files |
|---|---|---|
| Adapter registry + Pydantic validation | ✅ | `kanban_runtime/adapter_loader.py` |
| 4 bundled adapters | ✅ | `agents/*.yaml` |
| `shutil.which` gate (deactivates missing tools) | ✅ | `adapter_loader.py:145` |
| Preferences loader + `kanban init` wizard | ✅ | `kanban_runtime/preferences.py`, `kanban_cli/__init__.py` |
| Manager daemon: restart loop, signals, PID file | ✅ | `kanban_runtime/manager_daemon.py` |
| `Entity.name = spec.name` (UI uses display_name) | ✅ | `adapter_loader.py:143` |
| `AgentHeartbeat`, `AgentActivity` models | ✅ | `models.py:165-209` |
| `AGENT_STATUS_UPDATED`, `AGENT_ACTIVITY_LOGGED` events | ✅ | `event_bus.py:54-55` |
| 4 MCP activity tools | ✅ | `mcp_server.py:246-282, 781-895` |
| Activity REST router | ✅ | `routers/agent_activity.py` |
| UI activity sidebar + WS handlers | ✅ | `templates/kanban_board.html` |
| Workspace-aware sessions + project feed | ✅ | `models.py`, `routers/agent_activity.py`, `mcp_server.py`, `templates/kanban_board.html` |
| Heartbeat staleness sweeper | ✅ | `main.py:_heartbeat_sweeper` |
| RBAC: roles, `require_role`, task/project gates | ✅ | `auth.py:130-221` |
| Default-entity fallback restricted to GET-only | ✅ | `auth.py:93` |
| Server-side orchestration removed | ✅ | autopilot.py + agent_reactor.py deleted |
| MCP startup requires identity (no silent fallback) | ✅ | `mcp_server.py:72-79` |
| **Phase 6: Env-based MCP identity** | ✅ | No Kanban-issued API keys |

### Remaining Work

The original Phase 1-6 architecture is complete. The following
post-baseline requirements are now the active architecture backlog:

| Requirement | Status | Notes |
|---|---|---|
| Role-based agent assignment for Orchestrator/UI/Architecture/Worker/Test/Diff/Git PR | ✅ | `kanban_runtime/preferences.py` — `RoleConfig` with 7 roles, `RoleAssignment`, legacy migration. CLI `roles assign/list/start/stop/status`. |
| Standalone CLI role assignment | ✅ | `RoleAssignment.command` stores a role-bound CLI; `role_supervisor.py` builds an in-memory adapter at launch time. |
| Read-only popular CLI discovery | ✅ | `python -m kanban_cli agents discover` checks common CLI tools on PATH without registering agents or assuming credentials. |
| Clean local sheet | ✅ | `python -m kanban_cli sheet` prints projects, agents, and role assignments. |
| Headless role supervisor with separate worker sessions | ✅ | `kanban_runtime/role_supervisor.py` — tmux-backed per-role sessions, health monitoring, restart. |
| Single command `python -m kanban_cli run` | ✅ | Starts server + UI + role supervisor + agents. `--no-supervisor` flag available. |
| Chat-style task creation | ✅ | Board UI chat input bar, Enter-to-create, REST endpoint. |
| Critical diff review gate | ✅ | `DiffReview` model, MCP tools (`request_diff_review`, `review_diff`, `get_diff_reviews`), REST endpoints, Reviews tab in workbench. |
| Git PR Agent isolation | ✅ | `_is_git_pr_role()` check in PR sync endpoint. Only assigned Git PR Agent may sync. |
| Per-adapter heartbeat staleness | ✅ | Sweeper reads `reporting.heartbeat_interval` from adapter YAML, uses `2x interval` threshold (min 60s). |
| Assignment-to-execution bridge | ✅ | `kanban_runtime/assignment_launcher.py` subscribes to `TASK_ASSIGNED` events, creates the task session/lease/heartbeat/activity, and starts the assigned CLI in tmux. |
| CLI approval queue for headless prompts | ✅ | `AgentApproval` model, `AGENT_APPROVAL_REQUESTED/RESOLVED` events, REST endpoints under `/agents/approvals`, MCP tools `request_approval`/`get_pending_approvals`/`resolve_approval`, supervisor prompt capture (regex over `tmux capture-pane`) + stdin resume on resolution, Approvals workbench tab with approve/reject controls. |
| Project PR sync button for all associated PRs/issues/commits/reviews | ✅ | `gh pr/issue/search prs --reviewed-by/search commits` covered; falls back to `git log` for commit-only sync when `gh` is missing; `git config user.name` used as author fallback. |
| Per-task terminal execution view with tmux | ✅ | `GET /agents/tasks/{task_id}/active-session` resolves the active session for a task; each card has a 🖥️ Terminal button that jumps to the Terminal workbench tab for that session. |
| Intuitive local project access | ✅ | `POST /ui/api/open-workspace` invokes the platform-native opener (`xdg-open`/`open`/`explorer`) with a workspace whitelist; wired to "📂 Open Folder" buttons on the board header and projects list. |

**Phase 6 — Drop the Redundant Kanban-Side API Key** ✅ done

The MCP auth check now reads `KANBAN_AGENT_NAME` from env and looks up
`Entity.name`. No Kanban-side key is generated, stored, or transmitted.
The provider's key (`ANTHROPIC_API_KEY` etc.) is the only secret.

Concrete changes made:

1. ✅ **`mcp_server.py`** — replaced key-based `_resolve_caller` /
   `_authenticate` with name-based (reads `KANBAN_AGENT_NAME` env var).
2. ✅ **`kanban_cli/__init__.py`** — removed `generate_api_key()`,
   `_persist_manager_key` helper, and `~/.kanban/manager_key` file write.
3. ✅ **`kanban_runtime/manager_daemon.py`** — removed `MANAGER_KEY_PATH`,
   file-read block, and `KANBAN_MANAGER_KEY` env var. Kept
   `KANBAN_AGENT_NAME` and `KANBAN_AGENT_ROLE`.
4. ✅ **`auth.py`** — documented X-API-Key path as remote-mode only.
5. ✅ **`models.py`** — `Entity.api_key` field stays (for future remote
   mode), unused in default flow. No migration needed.
6. ✅ **`test_phase6.py`** — verifies MCP startup fails without
   `KANBAN_AGENT_NAME` and resolves identity correctly with it.
7. ✅ **Deleted** `~/.kanban/manager_key` references from all docs.

Done when: a fresh install can run `kanban init`, `kanban daemon`, and
the manager agent calls MCP tools successfully — all without
generating, storing, or transmitting any Kanban-issued API key. Only
the provider's env var is required.

---

## 6. Architectural Rules

These apply to anyone (human or agent) writing code in this repo.

1. **The server never decides.** Routing, assignment, prioritization,
   skill matching — all done by the manager agent via MCP tools, not
   by server-side Python. If you find yourself writing
   `for task in pending_tasks: assign_to(...)` in a router, stop.
2. **Adapters are data, not code.** Adding a new agent must never
   require editing `.py` files. If it does, the adapter spec is missing
   a field.
3. **Heartbeats are mandatory for workers.** Any agent that takes a
   task must call `report_status` at least every `heartbeat_interval`
   seconds (declared in its adapter). The sweeper marks missing
   heartbeats as `idle` and the manager can reassign.
4. **No new hardcoded tool names.** No `if agent.name == "claude"`
   branches anywhere. Capabilities live in the adapter YAML.
5. **No Kanban-issued secrets in the local-first path.** Provider keys
   live in env (declared in adapter YAML). Kanban identity is
   `KANBAN_AGENT_NAME`. Anything that generates a token for the local
   server is wrong unless it's behind a future `remote_mode=true` flag.
6. **Role behavior is data-driven.** Orchestrator, UI, Architecture,
   Worker, Test, Diff Review, and Git PR roles are preferences/config
   assignments, not Python branches for specific tool names.
7. **Critical code requires visible diff review.** Auth, identity,
   subprocess execution, tmux supervision, filesystem access, database
   migrations, and Git/PR automation require a recorded diff review
   before they are considered ready.
8. **Git/PR operations are isolated.** The assigned Git PR Agent owns
   branch, commit, push, and PR creation unless the human explicitly
   overrides the role boundary.
9. **Docs have one source of truth.** `AGENTS.md` owns architecture and
   workflow rules. `CLAUDE.md` must point here and must not duplicate or
   fork these rules.
10. **Headless CLI prompts must not be hidden.** Any CLI prompt that can
    block execution or authorize side effects must become a durable
    approval queue item visible in the UI. A tmux pane waiting for human
    input is not sufficient.

### Workflow rules

- Heavy code generation goes to the configured Worker Agent role. If
  Gemini is selected for Worker, use `gemini -p "..."`; otherwise use
  the selected worker adapter.
- Architecture-impacting changes go through the Architecture Agent.
- UI-impacting changes go through the UI Agent.
- Critical diffs go through the Diff Checker/Reviewer before handoff.
- Branch/commit/PR creation goes through the Git PR Agent.
- Never include Claude attribution in commits or PRs.
- Human is the sole orchestrator of the development workflow.

### Testing rules

- Every new MCP tool needs a smoke test before being considered done.
- Heartbeat/activity events must be verified end-to-end:
  agent → MCP → DB → event bus → WebSocket → UI.
- After Phase 6: a 401 test must verify that MCP startup with no
  `KANBAN_AGENT_NAME` fails immediately.

---

## 7. Open Questions

- In `supervised` mode, how should humans approve a manager-proposed
  assignment — UI modal, CLI prompt, or notification with a link?
- Adapter community registry — host on GitHub, or add a `kanban agents
  add <url>` that fetches from a configured registry list?
- Should the heartbeat staleness threshold be per-adapter
  (`reporting.heartbeat_interval × 2`) instead of a global 60s?
- Do we ever need pluggable protocols (gRPC, SSE) beyond MCP/WS/Webhook?
  Currently YAGNI.
- Should role preferences live beside the existing `manager/workers`
  shape for backward compatibility, or should `kanban init` migrate
  preferences to a new `roles` shape?
- How should the chat task creator expose the orchestrator's proposed
  decomposition before creating cards in fully headless mode?
- What exact signals make a code path "critical" enough to require the
  Diff Checker/Reviewer gate?
- Should PR sync rely only on `gh`, or should it also parse local git
  remotes and commit metadata when `gh` is unavailable?
- Which prompt-detection strategy should ship first: adapter-native
  structured hooks, PTY/pexpect wrapping, tmux `capture-pane` scanning,
  or a hybrid?
- What is the canonical response mapping per CLI for approve/reject
  prompts (`y/n`, numbered choice, typed phrase, or adapter-specific
  stdin payload)?

---

## 8. File Index

| Concern | File | Status |
|---|---|---|
| Adapter registry loader | `kanban_runtime/adapter_loader.py` | ✅ |
| Bundled adapters | `agents/*.yaml` | ✅ 8 adapters (7-role taxonomy) |
| Read-only CLI discovery | `kanban_runtime/adapter_loader.py`, `kanban_cli/__init__.py` | ✅ |
| Adapter sync entrypoint | `sync_agents.py` | ✅ thin wrapper |
| Preferences loader (7-role taxonomy + standalone CLI roles) | `kanban_runtime/preferences.py` | ✅ |
| Role supervisor (tmux-backed) | `kanban_runtime/role_supervisor.py` | ✅ |
| Assignment launcher | `kanban_runtime/assignment_launcher.py`, `main.py` | ✅ |
| CLI approval queue | `models.py`, `schemas.py`, `routers/agent_activity.py`, `mcp_server.py`, `kanban_runtime/role_supervisor.py`, `templates/kanban_board.html` | ✅ |
| Manager daemon | `kanban_runtime/manager_daemon.py` | ✅ |
| Wizard + CLI (run/roles) | `kanban_cli/__init__.py` | ✅ |
| Heartbeat sweeper (per-adapter) | `main.py:_heartbeat_sweeper` | ✅ |
| Heartbeat / activity models | `models.py:165-260` | ✅ |
| DiffReview model | `models.py:391-418` | ✅ |
| Activity events | `event_bus.py:54-65` | ✅ |
| Activity REST router | `routers/agent_activity.py` | ✅ |
| Diff review REST endpoints | `routers/agent_activity.py:826-936` | ✅ |
| Activity MCP tools | `mcp_server.py:246-282, 781-895` | ✅ |
| Diff review MCP tools | `mcp_server.py:500-580` | ✅ |
| Coordination state | `models.py`, `schemas.py`, `routers/agent_activity.py` | ✅ |
| Activity / terminal UI panel | `templates/kanban_board.html` | ✅ |
| Chat task creation UI | `templates/kanban_board.html` | ✅ |
| Diff reviews workbench tab | `templates/kanban_board.html` | ✅ |
| RBAC core | `auth.py:130-221` | ✅ |
| Default-entity fallback | `auth.py:93` | ✅ GET-only |
| MCP auth (name-based) | `mcp_server.py:66-89` | ✅ |
| Git PR role isolation | `routers/agent_activity.py:_is_git_pr_role` | ✅ |
| Server-side autopilot | — | ✅ deleted |
| Server-side reactor | — | ✅ deleted |
| MCP startup requires identity | `mcp_server.py:71-82` | ✅ |
| **Phase 6: Env-based MCP identity** | ✅ | `mcp_server.py:66-101, kanban_cli/__init__.py` — no Kanban-issued keys |
