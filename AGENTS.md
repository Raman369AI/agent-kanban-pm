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
  remotes. The Kanban app uses local GitHub CLI/session state in the
  local-first path.

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

### Access Rule

The local-first path uses local process identity. GitHub/SCM sync must use
the user's existing local CLI/session where possible and must not add a new
state store.

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
  type: none

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

## 4. Identity Model — Env-Based

This is local-first. The Kanban server identifies local agent processes by
metadata set when the runtime starts them.

Identity to the Kanban server comes from process env vars set by the
manager daemon when spawning each agent:

| Env var | Set by | Read by | Purpose |
|---|---|---|---|
| `KANBAN_AGENT_NAME` | manager daemon | MCP server | "Which adapter spawned this process?" — looked up against `Entity.name` |
| `KANBAN_AGENT_ROLE` | manager daemon | MCP server | `manager` or `worker` — applies RBAC at the tool layer |
| `KANBAN_API_BASE` | manager daemon | MCP/REST clients | Where the Kanban server lives (default `http://localhost:8000`) |

**Trust boundary**: the OS process. Only the local user can set env
vars on processes they own.

**Humans** authenticating to the REST UI use `X-Entity-ID` (or session
cookie). The deprecated default-entity fallback in `auth.py:91` is now
restricted to safe `GET` requests with a logged warning — mutations
require explicit identity.

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
| **Phase 6: Env-based MCP identity** | ✅ | Local process identity |

### Remaining Work

The original Phase 1-6 architecture is complete. The following
post-baseline requirements are now the active architecture backlog:

| Requirement | Status | Notes |
|---|---|---|
| Role-based agent assignment for Orchestrator/UI/Architecture/Worker/Test/Diff/Git PR | ✅ | `kanban_runtime/preferences.py` — `RoleConfig` with 7 roles, `RoleAssignment`, legacy migration. CLI `roles assign/list/start/stop/status`. |
| Standalone CLI role assignment | ✅ | `RoleAssignment.command` stores a role-bound CLI; `role_supervisor.py` builds an in-memory adapter at launch time. |
| Read-only popular CLI discovery | ✅ | `python -m kanban_cli agents discover` checks common CLI tools on PATH without registering agents. |
| Clean local sheet | ✅ | `python -m kanban_cli sheet` prints projects, agents, and role assignments. |
| Headless role supervisor with separate worker sessions | ✅ | `kanban_runtime/role_supervisor.py` — tmux-backed per-role sessions, health monitoring, restart. |
| Single command `python -m kanban_cli run` | ✅ | Starts server + UI + role supervisor + agents. `--no-supervisor` flag available. |
| Chat-style task creation | ✅ | Board UI chat input bar (regex fallback) and `kanban_cli chat <project>` REPL backed by the configured orchestrator adapter. See §10. |
| Critical diff review gate | ✅ | `DiffReview` model, MCP tools (`request_diff_review`, `review_diff`, `get_diff_reviews`), REST endpoints, Reviews tab in workbench. |
| Git PR Agent isolation | ✅ | `_is_git_pr_role()` check in PR sync endpoint. Only assigned Git PR Agent may sync. |
| Per-adapter heartbeat staleness | ✅ | Sweeper reads `reporting.heartbeat_interval` from adapter YAML, uses `2x interval` threshold (min 60s). |
| Assignment-to-execution bridge | ✅ | `kanban_runtime/assignment_launcher.py` subscribes to `TASK_ASSIGNED` events, creates the task session/lease/heartbeat/activity, and starts the assigned CLI in tmux. |
| CLI approval queue for headless prompts | ✅ | `AgentApproval` model, `AGENT_APPROVAL_REQUESTED/RESOLVED` events, REST endpoints under `/agents/approvals`, MCP tools `request_approval`/`get_pending_approvals`/`resolve_approval`, supervisor prompt capture (regex over `tmux capture-pane`) + stdin resume on resolution, Approvals workbench tab with approve/reject controls. |
| Project PR sync button for all associated PRs/issues/commits/reviews | ✅ | `gh pr/issue/search prs --reviewed-by/search commits` covered; falls back to `git log` for commit-only sync when `gh` is missing; `git config user.name` used as author fallback. |
| Per-task terminal execution view with tmux | ✅ | `GET /agents/tasks/{task_id}/active-session` resolves the active session for a task; each card has a 🖥️ Terminal button that jumps to the Terminal workbench tab for that session. |
| Intuitive local project access | ✅ | `POST /ui/api/open-workspace` invokes the platform-native opener (`xdg-open`/`open`/`explorer`) with a workspace whitelist; wired to "📂 Open Folder" buttons on the board header and projects list. |

**Phase 6 — Env-Based MCP Identity** ✅ done

The MCP auth check now reads `KANBAN_AGENT_NAME` from env and looks up
`Entity.name`.

Concrete changes made:

1. ✅ **`mcp_server.py`** — replaced key-based `_resolve_caller` /
   `_authenticate` with name-based (reads `KANBAN_AGENT_NAME` env var).
2. ✅ **`kanban_cli/__init__.py`** — uses local role preferences.
3. ✅ **`kanban_runtime/manager_daemon.py`** — keeps
   `KANBAN_AGENT_NAME` and `KANBAN_AGENT_ROLE`.
4. ✅ **`auth.py`** — local HTTP identity uses `X-Entity-ID`.
5. ✅ **`models.py`** — entity identity is name/role based.
6. ✅ **`test_phase6.py`** — verifies MCP startup fails without
   `KANBAN_AGENT_NAME` and resolves identity correctly with it.

Done when: a fresh install can run `kanban init`, `kanban daemon`, and
the manager agent calls MCP tools successfully with local process identity.

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
5. **Local-first identity.** Kanban identity is `KANBAN_AGENT_NAME`.
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
| Chat designer CLI | `kanban_cli/chat.py`, `kanban_cli/chat_designer.py`, `routers/ui.py:ui_create_chat_plan` | ✅ |
| Diff reviews workbench tab | `templates/kanban_board.html` | ✅ |
| RBAC core | `auth.py:130-221` | ✅ |
| Default-entity fallback | `auth.py:93` | ✅ GET-only |
| MCP auth (name-based) | `mcp_server.py:66-89` | ✅ |
| Git PR role isolation | `routers/agent_activity.py:_is_git_pr_role` | ✅ |
| Server-side autopilot | — | ✅ deleted |
| Server-side reactor | — | ✅ deleted |
| MCP startup requires identity | `mcp_server.py:71-82` | ✅ |
| **Phase 6: Env-based MCP identity** | ✅ | `mcp_server.py:66-101, kanban_cli/__init__.py` — no Kanban-issued keys |

---

## 9. UI Revamp Plan — In-Card Visibility & Layout Repair

Status: **implemented** (Phases 1-6 and 8; Phase 7 deferred). The UI Agent role owns delivery
(see §2 role table). This section is the source of truth for the
redesign; treat the existing `templates/kanban_board.html` layout as
legacy until the phases below land. Architecture-impacting subsections
must be reviewed by the Architecture Agent before phase work begins.

### 9.1 Problems We Are Fixing

Concrete defects in the current board (`templates/kanban_board.html`):

- **Terminal is decoupled from the card.** The 🖥️ Terminal button on
  each card jumps focus to the workbench sidebar's Terminal tab. The
  operator loses sight of the originating card and has to re-find it
  in the column.
- **Approvals are easy to miss.** A pending approval surfaces only as
  a tab badge in the workbench (`#approvals-badge`) and a transient
  toast. The originating card has no marker, so an unattended approval
  is invisible until someone opens the workbench tab.
- **The right sidebar is a 380 px guillotine.** The `board-shell`
  grid is `minmax(0,1fr) 380px`. The workbench eats column real estate
  even when idle. The GitHub/PRs pane is the single biggest consumer
  of that space yet is rarely needed during execution.
- **Columns do not scroll independently.** `.kanban-drop-zone` has no
  `overflow-y`. Long columns push the page; the column header and
  "Add Task" footer drift offscreen.
- **Cards are visually static.** No per-card indicator for: active
  session running, pending approval, blocked-by-review, recent activity.
  Operators must cross-reference the Live tab with cards manually.

### 9.2 Goals & Non-Goals

**Goals**

- Each card is the primary surface for its own state: terminal,
  approvals, activity, latest diff review.
- Approval requests become impossible to miss: card badge, pulse,
  optional OS notification, header bell, existing toast.
- Reclaim horizontal space when the workbench is idle; expand it on
  demand.
- Columns scroll independently with sticky headers and sticky
  "Add Task" footers.
- Preserve existing REST/MCP contracts. The revamp is
  template/JS/CSS-only unless a new endpoint is justified in §9.9.

**Non-goals**

- No change to the orchestrator/server decision boundary — the server
  still does not decide (architectural rule §6.1).
- No new auth or identity primitives. `X-Entity-ID` and
  `KANBAN_AGENT_NAME` stay as-is.
- No mobile-first redesign. Target is desktop ≥ 1280 px; below that
  collapses gracefully (existing media query at 1280 already does this).

### 9.3 New Layout Model

Replace the fixed two-column `board-shell` with a three-zone shell:

```
┌─────────────────────────────────────────────────────────────────┐
│ Header (project, path, GitHub link, role config, bell)          │
├─────────────────────────────────────────────────────────────────┤
│ Board (full width when workbench dock collapsed)                │
│  ─ each column scrolls independently                            │
│  ─ sticky column header + sticky "Add Task" footer              │
│  ─ cards expand inline, pushing siblings down                   │
├─────────────────────────────────────────────────────────────────┤
│ Workbench dock (bottom, collapsible, resizable)                 │
│  collapsed: 40 px strip with aggregate counters                 │
│  expanded: tabs Live · Terminal · Approvals · Reviews · Decisions│
│  GitHub moves to a separate route /projects/{id}/git            │
└─────────────────────────────────────────────────────────────────┘
```

- The workbench becomes a **bottom dock**, not a right sidebar. Default
  state: collapsed to a 40 px strip showing counters
  (`● 3 active`, `🛂 1 pending`, `📝 2 reviews`). User-toggleable;
  height persisted in `localStorage` keyed by `project_id`.
- The GitHub tab is removed from the dock and promoted to a dedicated
  route `/projects/{id}/git` so PR/issue/commit lists can use the full
  viewport. The header gets a `🔗 GitHub` button that routes there.
- When the dock is collapsed the `kanban-container` fills 100% of
  the available width.

### 9.4 Card Anatomy — Compact + Expandable

Each `.kanban-task` becomes two layers:

1. **Compact face (always visible)** — title, status pill, priority,
   assignee chips, state indicators (§9.5). Target height 96–120 px.
   Description is hidden in compact view (it appears in the expanded
   Activity tab).
2. **Inline expansion panel (revealed on click outside the action row)**
   — tabbed body inside the card. Three tabs:
   - **Terminal** — live tail of the active session for this task.
     Calls `GET /agents/tasks/{task_id}/active-session` to resolve the
     session, then streams `/agents/sessions/{id}/terminal`. Includes a
     "Pop out" button (full-screen overlay with the same content) and a
     "Send to dock" button for users who prefer the legacy behaviour.
   - **Approvals** — list of pending approvals scoped to this task.
     Reuses `/agents/approvals?task_id={id}&status_filter=pending`
     (verify the filter exists; if not, see §9.9). Inline approve/reject
     with optional note. Approving from the card uses the same
     `PATCH /agents/approvals/{id}/resolve` endpoint as the dock so the
     two views stay consistent.
   - **Activity** — last N entries from
     `/agents/activity?task_id={id}&limit=50` and any
     `DiffReview` rows for the task (collapsed by default). Card
     description renders here, full-length.

Expansion pushes siblings down inside the column; no overlay, no modal.
Multiple cards may be expanded at once. Expansion state is stored in
`localStorage` keyed by `(project_id, task_id)` so a refresh keeps
panels open.

### 9.5 Card State Indicators

Indicators render on the compact face so an operator can scan a column
and immediately see what needs attention.

| Indicator | Trigger | Visual |
|---|---|---|
| Active session | task has an active row in `agent_sessions` | green left-border + small `▶︎ live` chip |
| Pending approval | one or more `agent_approvals` rows for this task with `status=pending` | amber pulsing dot + `🛂 N` badge in title row + amber outline on the whole card |
| Blocked by review | task has a `DiffReview` with `status=changes_requested` | red badge `🚧 review` |
| Recent activity | `agent_activities` entry in last 60 s | small dot in card corner that fades over 30 s |
| Stale heartbeat | session `last_seen_at` older than `2× heartbeat_interval` | session chip greys to `idle` |

The pending-approval indicator is the load-bearing one; everything else
is supplemental. It must be driven by the existing
`AGENT_APPROVAL_REQUESTED` / `AGENT_APPROVAL_RESOLVED` WebSocket events
so the card flips state without a refresh.

### 9.6 Notifications for Approvals

Three layers, all firing on the existing
`agent_approval_requested` WebSocket event:

1. **Card badge** as in §9.5. Persists until the approval is resolved.
2. **Toast** (already exists at `templates/kanban_board.html:1926-1930`).
   Add an "Open card" action that scrolls to and expands the originating
   card.
3. **OS-level notification** via `Notification.requestPermission()` on
   first project load. Opt-in only, persisted under
   `localStorage["kanban.notifications.enabled"]`. Sound disabled by
   default with a settings toggle to enable. Body = approval title +
   agent + truncated command. Clicking focuses the tab and triggers the
   same "Open card" behaviour as the toast.

A small **bell icon in the header** opens a dropdown of recent
unresolved approvals for the project — equivalent to the dock's
Approvals tab but always reachable without expanding the dock.

### 9.7 Column Scrolling & Layout Fixes

- `.kanban-column` becomes a vertical flex container: header
  (sticky top), drop zone (`flex: 1; overflow-y: auto`), footer
  "Add Task" (sticky bottom).
- Column max-height = `calc(100vh - <header> - <dock if visible>)`.
- Inline-expanded cards stay inside the column's scroll container;
  expanding does not break sticky headers.
- `.kanban-container` keeps `overflow-x: auto` for many-column boards
  but each column scrolls vertically inside itself.
- Drop zones get a min-height so empty columns still accept drops.

### 9.8 GitHub / PR Page

Move the contributions/PR feed out of the dock into a project-scoped
route `/projects/{id}/git`. The new route renders:

- Top strip: project workspace path, last sync timestamp, sync button,
  `gh` availability indicator.
- Two-column layout: PRs (open / merged / closed filter) on the left;
  issues + recent commits on the right.
- Each PR/issue links back to its source task on the board via the
  `user_contributions` rows that already exist.

The dock keeps a small "PR sync" status line in the collapsed strip
but no list. The header's `🔗 GitHub` button is the entry point.
Sync still runs through the existing
`/agents/projects/{project_id}/contributions/sync/github` endpoint and
remains gated by `_is_git_pr_role` (architectural rule §6.8).

### 9.9 Backend / API Notes

The redesign should be doable with current endpoints. Two small
additions are reasonable if the in-card UX needs them; each requires
justification before being added:

- **Per-task approval filter.** The Approvals tab in the card needs
  to filter by `task_id`. Verify whether `/agents/approvals` already
  accepts `task_id=` (it currently accepts `project_id`, `agent_id`,
  `status_filter`). If not, add the filter — it is a straightforward
  query-arg addition, no schema change.
- **Session terminal stream.** Current
  `/agents/sessions/{id}/terminal` is poll-based. If polling at ~2 s
  causes load with several cards expanded, add a
  `/ws/sessions/{id}/terminal` subscription that reuses the existing
  event bus. Build only after measuring.

No new tables. No new MCP tools. No new auth. Anything beyond this
must update §6 architectural rules first.

### 9.10 Phasing

Each phase lands as its own PR via the assigned Git PR Agent. The
Diff Checker/Reviewer must review phases 1 and 4 (template +
auth-adjacent JS) before merge.

| Phase | Scope | Acceptance | Status |
|---|---|---|---|
| 1. Layout shell | Replace `board-shell` grid with header + board + bottom dock; collapse/expand state persisted in `localStorage` | Board fills width when dock collapsed; dock height resizable; no regression in existing workbench tabs | ✅ done |
| 2. Column scrolling | Per-column `overflow-y`; sticky column header and footer | Long columns scroll inside the column; "Add Task" stays visible | ✅ done |
| 3. Card expansion + Terminal tab | Inline expansion panel; Terminal tab wired to `active-session` + terminal endpoints; pop-out overlay | Clicking a card with an active session streams its terminal inside the card without leaving the board | ✅ done |
| 4. Approval indicator + in-card Approvals tab | Card badge, amber outline, pulse animation; in-card approve/reject; header bell dropdown | Triggering an approval visibly marks the card; resolving from card or dock keeps both views consistent | ✅ done |
| 5. OS notifications | `Notification` permission flow, settings toggle, click-to-focus card | Approval request fires a desktop notification when permission granted; clicking opens the card | ✅ done |
| 6. Activity tab + diff review surface in card | Wire `/agents/activity` and `/agents/diff-reviews` per task | Card shows last activity entries and any open diff reviews | ✅ done |
| 7. GitHub route extraction | Move contributions UI to `/projects/{id}/git`; replace dock tab with collapsed status line + header link | Full-width PR/issue list; dock no longer dedicates a tab to GitHub | deferred (GitHub tab kept in dock for now) |
| 8. Polish | Empty states, loading states, accessibility (ARIA tabs, focus trap on pop-out, keyboard expand) | axe scan passes; Tab / Enter expand cards; Esc collapses | ✅ done |

### 9.11 Acceptance Tests (UI Agent + Test Writer)

Each test must pass end-to-end before its phase is considered done.

- **Approval visibility** — fire `request_approval` for a task; assert
  the card shows the amber indicator within 1 s of the WS event; assert
  the dock badge increments; assert the toast appears.
- **In-card resolve** — approve from the card; assert the queue entry
  transitions to `approved`, the card indicator clears, and the agent
  session resumes (the existing supervisor stdin-write path).
- **Terminal in card** — start an `agent_session` for a task; expand the
  card; assert the in-card terminal streams the same content as the
  dock's Terminal tab for that session.
- **Column scroll** — create 30 cards in one stage; assert column
  scroll works and the column header stays pinned.
- **Layout** — collapse the dock; assert
  `document.querySelector('.kanban-container').clientWidth` equals the
  viewport width minus board padding.

### 9.12 Open Questions

- In-card terminal — default to **last 200 lines** (current dock
  behaviour) or **last 50** to keep cards compact? Likely 50 with a
  "Load more" control; benchmark before locking in.
- Pop-out terminal — modal over the board, or a separate
  `/sessions/{id}` route? Modal preserves board context; a route allows
  multi-monitor.
- OS notification scope — per project or global across the running
  Kanban session? Global is simpler but louder.
- Should approving from inside a card require a confirmation step for
  `git_push` / `pr_create` approval types? Diff Checker concern, not
  pure UI.
- Header bell dropdown — does it duplicate the dock's Approvals tab
  enough to be worth the surface area? Could be deferred past phase 4
  if operators do not ask for it.

---

## 10. Chat Designer — CLI Conversation → Backlog Cards

The browser chat bar uses a regex fallback (`_plan_items_from_chat`)
that produces boilerplate cards without an LLM. The Chat Designer is
the LLM-backed counterpart that runs in a terminal: the human chats
with the configured orchestrator adapter, sees a structured plan, and
commits the cards to the project backlog after confirmation.

### 10.1 Goals & non-goals

**Goals**

- One terminal command (`python -m kanban_cli chat <project>`) for
  natural-language → backlog. Multi-turn refine/edit/drop/commit.
- Adapter-agnostic. Works with any CLI assigned to the `orchestrator`
  role (Claude / Gemini / Codex / standalone).
- Reuses existing infrastructure: orchestrator role assignment,
  `/ui/tasks/chat-plan`, `OrchestrationDecision`, `TASK_CREATED` /
  `CHAT_TASK_CREATED` events, project-local `AGENTS.md` plan append.
- Conforms to §6 rules: no hardcoded tool names, no Kanban-issued
  secrets, server stays a state store.

**Non-goals**

- Does not replace the browser chat bar; the regex path stays as a
  no-LLM fallback.
- Does not auto-assign agents. Cards land in backlog; assignment flows
  through the existing `TASK_ASSIGNED` path.
- Not a long-lived daemon. Each `chat` invocation is a foreground REPL.

### 10.2 Component model

```
┌──────────────────────────────────────────────────────────────────┐
│ kanban_cli/chat.py            (foreground REPL)                  │
│  ─ resolves project + orchestrator RoleAssignment                │
│  ─ builds DesignerPrompt (system + history + user turn)          │
│  ─ delegates to ChatDesigner.design(prompt) → PlanV1             │
│  ─ renders plan, runs y/n/edit/refine loop                       │
│  ─ on commit: POST /ui/tasks/chat-plan {items, transcript}       │
│  ─ on quit:   writes draft to ~/.kanban/chat/drafts/             │
└──────────────────────────┬───────────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ kanban_cli/chat_designer.py                                      │
│  ─ resolves orchestrator command from preferences + adapter      │
│  ─ subprocess.run([cmd, *flags], stdin=prompt or args, env=...)  │
│  ─ env adds KANBAN_AGENT_NAME, KANBAN_AGENT_ROLE=orchestrator,   │
│     KANBAN_CHAT_MODE=1; provider key inherited from os.environ   │
│  ─ parses last <plan>{...}</plan> block, validates PlanV1        │
│  ─ retries once with strict-JSON system prompt on parse failure  │
└──────────────────────────┬───────────────────────────────────────┘
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│ routers/ui.py: POST /ui/tasks/chat-plan (extended)               │
│  ─ if items: create tasks from items verbatim                    │
│  ─ else:     existing _plan_items_from_chat regex fallback       │
│  ─ records OrchestrationDecision with transcript                 │
│  ─ appends plan section to project AGENTS.md                     │
│  ─ publishes TASK_CREATED + CHAT_TASK_CREATED                    │
└──────────────────────────────────────────────────────────────────┘
```

### 10.3 Data contract — `PlanV1`

The orchestrator must respond with one JSON block enclosed in
`<plan>...</plan>`. The CLI strips conversational text around the
block. Schema:

```json
{
  "version": 1,
  "summary": "Human-readable summary of the requested work.",
  "questions": [
    "Open clarification 1?",
    "Open clarification 2?"
  ],
  "tasks": [
    {
      "title": "Short imperative title (≤ 255 chars)",
      "description": "Multi-line description with context and intent.",
      "acceptance": ["criterion 1", "criterion 2"],
      "priority": 7,
      "role_hint": "worker",
      "depends_on": []
    }
  ]
}
```

Validation rules (Pydantic, in `kanban_cli/chat_designer.py`):

- `title`: non-empty, ≤ 255 chars.
- `priority`: integer in `[0, 10]`.
- `role_hint`: one of `orchestrator|ui|architecture|worker|test|diff_review|git_pr` or null.
- `depends_on`: list of integer indices into `tasks` (local refs;
  resolved to task IDs server-side after creation).
- `tasks`: hard-cap 20 entries; CLI warns and trims if more arrive.
- If `questions` is non-empty the REPL surfaces them and refuses to
  commit until the human answers or types `ignore questions`.

### 10.4 Adapter contract

Optional `chat_designer` block in adapter YAML. Defaults work for
adapters that accept `-p "<prompt>"` (the bundled four):

```yaml
chat_designer:
  prompt_flag: "-p"          # how to pass a one-shot prompt
  stdin: false                # if true, prompt goes via stdin instead
  output_format: "stdout"     # stdout | json_block | wrapper_script
  timeout_seconds: 120
```

`RoleAssignment` gains an optional `prompt_flag` (and `chat_stdin`
boolean) so standalone CLI roles can declare invocation style without
a YAML adapter file. `roles assign` accepts `--prompt-flag` and
`--chat-stdin`.

Resolution in `ChatDesigner`:

1. Read orchestrator `RoleAssignment` → adapter spec (if any).
2. If adapter has `chat_designer`, use it.
3. Else fall back to `prompt_flag: "-p"`, `stdin: false`,
   `timeout_seconds: 120`.
4. For standalone CLI roles (`RoleAssignment.command` set), use
   `RoleAssignment.prompt_flag` / `chat_stdin` if present, else the
   defaults.

No `if agent.name == "claude"` branches — keeps §6.4.

### 10.5 REPL UX

```
$ python -m kanban_cli chat 4
Project: agent-kanban-pm  (id 4, /home/.../agent-kanban-pm)
Orchestrator: claude (claude-sonnet-4-6, headless)

You> Add OAuth login (Google + GitHub) and migrate existing sessions.
[claude is thinking… 4s]

Plan
  Summary: Add OAuth login (Google + GitHub) and migrate existing sessions.

  Questions:
    1. Should existing password-auth users be force-migrated or opt-in?

  Tasks (5):
    [9] architecture  Design OAuth provider abstraction
    [7] worker        Implement OAuth callback route             (depends on 1)
    [7] worker        Wire frontend Sign-in-with-Google button   (depends on 1)
    [6] test          Add OAuth e2e tests
    [5] worker        Migration plan for existing sessions

(c)ommit  (r)efine  (e)dit n  (d)rop n  (q)uit  >
```

Commands:

| Key | Behavior |
|---|---|
| `c` | POST plan to `/ui/tasks/chat-plan`; print created task IDs and the AGENTS.md plan path; exit. |
| `r` | Read a follow-up turn; ChatDesigner re-emits a full plan (full re-emit avoids delta drift). |
| `e <n>` | Open `$EDITOR` on task n's title/description/priority/role_hint as YAML. |
| `d <n>` | Drop task n; remaining indices renumber after confirmation. |
| `q` | Save draft to `~/.kanban/chat/drafts/<project>-<timestamp>.json` and exit. |

The full transcript (turns + final plan) is stored in
`OrchestrationDecision.rationale` so it surfaces in the workbench
Decisions tab.

### 10.6 Server-side changes

Single endpoint extension. **No new tables, no new MCP tools, no new
auth.** `POST /ui/tasks/chat-plan` body adds optional `items` and
`transcript`:

```python
class ChatPlanItem(BaseModel):
    title: str
    description: str
    priority: int = 5
    role_hint: Optional[str] = None
    acceptance: list[str] = []
    depends_on: list[int] = []

class ChatPlanRequest(BaseModel):
    project_id: int
    message: str
    items: Optional[list[ChatPlanItem]] = None
    transcript: Optional[str] = None
```

Behavior:

- If `items` is present, the handler creates Tasks from the items and
  skips `_plan_items_from_chat`.
- `acceptance` is rendered into the description as a Markdown
  checklist.
- `depends_on` (item indices) is resolved post-create into a
  "Depends on: #<id>" line appended to the description, until/unless a
  dedicated task-dependency table is introduced.
- `transcript` is stored on `OrchestrationDecision.rationale` so the
  Decisions tab shows the conversation that produced the cards.
- Existing `TASK_CREATED` + `CHAT_TASK_CREATED` events fire unchanged.

The browser chat bar continues to post `{project_id, message}` and
hits the regex fallback. Backwards compatible.

### 10.7 Identity & security

- The CLI inherits the human's shell env (provider keys live there per
  §4).
- The subprocess gets `KANBAN_AGENT_NAME=<orchestrator name>`,
  `KANBAN_AGENT_ROLE=orchestrator`, `KANBAN_CHAT_MODE=1`. These let
  the orchestrator branch on chat-vs-headless.
- The HTTP call to `/ui/tasks/chat-plan` uses the human's existing
  session auth (`X-Entity-ID`). The orchestrator subprocess does not
  call the API directly — only the user's CLI does. The trust boundary
  stays in the human's hands.
- No new secret store, no new token. §6.5 satisfied.

### 10.8 Failure modes

| Failure | Behavior |
|---|---|
| Orchestrator CLI not on PATH | Fail fast at REPL start; suggest `kanban_cli agents discover`. |
| Provider key missing | Fail fast with the adapter's `auth.env_var` name. |
| Subprocess returns malformed JSON | Retry once with stricter system prompt; on second failure show raw output and offer `(r)etry` / `(q)uit`. |
| Subprocess hangs > `timeout_seconds` | Kill, show partial output, offer retry. |
| Plan has > 20 tasks | Hard-cap to 20 with a warning; ask user to refine. |
| HTTP 401 on commit | Run interactive login flow (existing pattern). |
| HTTP 4xx with details | Print details, keep draft, allow fix-and-retry. |
| Network down | Save draft locally, exit code 2. |

### 10.9 Phasing

| Phase | Scope | Status |
|---|---|---|
| 1. Endpoint extension | `items` and `transcript` on `/ui/tasks/chat-plan`; preserve regex fallback. | ✅ |
| 2. ChatDesigner module | `kanban_cli/chat_designer.py` with adapter resolution, subprocess shell-out, JSON-block parser, retry. | ✅ |
| 3. CLI subcommand + REPL | `kanban_cli chat <project>` with refine/edit/drop/commit/quit and draft save/resume. | ✅ |
| 4. Adapter blocks + standalone prompt-flag | Optional `chat_designer:` on bundled adapter YAMLs; `--prompt-flag` / `--chat-stdin` on `roles assign`. | ✅ |
| 5. Tests | Unit tests for parser/validator/retry; integration test that POSTs `items` and verifies cards land in backlog. | ✅ |
| 6. Polish (deferred) | Acceptance checklist rendering refinements; transcript surfacing in Decisions tab; web-bar parity (optional). | open |

### 10.10 Files

| File | Status |
|---|---|
| `routers/ui.py` (`ChatPlanRequest`, item-aware branch) | ✅ |
| `schemas.py` (`ChatPlanItem`, `ChatPlanRequest`) | ✅ |
| `kanban_cli/__init__.py` (`chat` subparser) | ✅ |
| `kanban_cli/chat.py` (REPL) | ✅ |
| `kanban_cli/chat_designer.py` (subprocess + parser) | ✅ |
| `agents/*.yaml` (`chat_designer:` blocks) | ✅ |
| `kanban_runtime/preferences.py` (`prompt_flag`, `chat_stdin`) | ✅ |
| `test_chat_designer.py` (unit + integration) | ✅ |

### 10.11 Open questions

- Refine = full re-emit vs. delta patch. Phase 1 ships full re-emit
  (simpler, no delta drift). Reconsider if token cost becomes an
  issue.
- Tool-call mode (orchestrator calls `create_task` directly via MCP
  with an approval-queue gate) vs. the JSON-block contract used here.
  Block is universal and cheaper; tool-call is more powerful but
  introduces a new trust path.
- Acceptance criteria — inline in description today; a future
  `Task.acceptance_criteria` column would map cleanly.
- Should the browser chat bar share the same designer pipeline (via
  WebSocket) so the web UI gets LLM-quality plans too? Out of scope
  here; could be a phase 7.
