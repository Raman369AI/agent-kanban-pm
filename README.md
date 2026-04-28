# Agent Kanban PM

A local-first, agent-owned project management system. The server is a dumb state store; your chosen manager agent owns all routing decisions.

**Local-only. YAML-driven. No hardcoded tool names. No Kanban-issued API keys.**

---

## What This Is

A Kanban board + MCP server that lets AI CLI agents (Claude Code, Codex, OpenCode, Gemini CLI) collaborate with you on projects. The server tracks state. The manager agent decides who does what.

```
┌─────────────────────────────────────────┐
│  Kanban Server (dumb state store)       │
│  ─ SQLite                               │
│  ─ REST + WebSocket + MCP               │
│  ─ Event bus (publish only, no logic)   │
│  ─ Adapter registry loader              │
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

---

## Prerequisites

- Python 3.10+
- One or more AI CLI tools installed (Claude Code, Gemini CLI, OpenCode, or Codex)
- Linux/macOS (Windows may work but is untested)

---

## Step-by-Step: Run the System

### Step 1 — Install Dependencies

```bash
cd /home/kronos/Desktop/agent-kanban-pm
pip install -r requirements.txt
```

### Step 2 — Initialize Configuration

Run the interactive wizard to pick your manager agent, mode, and workers:

```bash
python -m kanban_cli init
```

You will be prompted to:
1. **Pick a manager agent** (e.g., Claude) — must have `manager` role
2. **Pick a mode** — `supervised` (human approves), `auto` (manager acts), `headless` (fully autonomous)
3. **Pick worker agents** — any combination of installed tools

This creates:
- `~/.kanban/preferences.yaml` — your manager/worker configuration
- `~/.kanban/agents/*.yaml` — bundled adapter specs (auto-copied)

**No Kanban-issued API key is generated.** The daemon inherits your provider's
env var (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, etc.) directly.

### Step 3 — Start the Server

```bash
python main.py
```

Server runs at `http://localhost:8000`

- **Kanban UI:** http://localhost:8000/ui/projects
- **API Docs:** http://localhost:8000/docs

### Step 4 — Register Yourself (Human Owner)

```bash
curl -X POST http://localhost:8000/entities/register/human \
  -H "Content-Type: application/json" \
  -d '{"name": "You", "entity_type": "human"}'
```

Save the returned `id` — you will use it as `X-Entity-ID` in future requests.

### Step 5 — Create and Approve a Project

```bash
# Create project (replace 1 with your human entity id)
curl -X POST http://localhost:8000/projects \
  -H "Content-Type: application/json" \
  -H "x-entity-id: 1" \
  -d '{"name": "My Project", "description": "First project"}'

# Approve project (managers/owners only)
curl -X POST http://localhost:8000/projects/1/approve \
  -H "x-entity-id: 1"
```

### Step 6 — Create Tasks

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -H "x-entity-id: 1" \
  -d '{"title": "Set up CI/CD", "project_id": 1, "required_skills": "devops"}'
```

### Step 7 — (Optional) Start the Manager Daemon

If you want the manager agent to actively route work:

```bash
# Make sure your provider key is set in the shell
export ANTHROPIC_API_KEY="your-key"

# Start the daemon
python -m kanban_cli daemon
```

The daemon will:
- Spawn your chosen manager CLI tool
- Set `KANBAN_AGENT_NAME` and `KANBAN_AGENT_ROLE` env vars
- Restart automatically if the subprocess exits
- Write its PID to `~/.kanban/daemon.pid`

Check status:
```bash
python -m kanban_cli daemon status
```

Stop:
```bash
python -m kanban_cli daemon stop
```

---

## Workflows

### Browser-First Workflow

1. Open http://localhost:8000/ui/projects
2. Create a project, drag tasks across the Kanban board
3. The **Agent Activity sidebar** shows live status of all agents
4. When a task is moved to "In Progress", start working on it
5. When done, move it to "Done"

### MCP Workflow (Claude Code, Codex, etc.)

1. Start the manager daemon (Step 7 above)
2. The daemon auto-generates `~/.kanban/mcp/kanban_mcp.json`
3. Point your CLI tool at that MCP config
4. Ask: **"What tasks are assigned to me?"** — uses `get_my_tasks`
5. Say: **"Create a task called 'Fix bug' in project 1"**
6. Say: **"Mark task 54 as completed"**

### CLI / Curl Workflow

```bash
# Create a project
curl -X POST http://localhost:8000/projects \
  -H "x-entity-id: 1" \
  -d '{"name": "API Test", "description": "Created via curl"}'

# Approve project
curl -X POST http://localhost:8000/projects/1/approve \
  -H "x-entity-id: 1"

# Create a task
curl -X POST http://localhost:8000/tasks \
  -H "x-entity-id: 1" \
  -d '{"title": "Fix login bug", "project_id": 1, "required_skills": "python"}'

# List tasks
curl http://localhost:8000/tasks

# Update task status
curl -X PATCH http://localhost:8000/tasks/1 \
  -H "x-entity-id: 1" \
  -d '{"status": "in_progress", "version": 0}'
```

### Folder-as-Project Workflow

```bash
# Register current folder as a project
python open_project.py

# Register a specific folder
python open_project.py /path/to/project

# List all projects and their paths
python open_project.py --list
```

---

## MCP Setup

### 1. Install MCP dependency

```bash
pip install mcp
```

### 2. Configure your CLI tool

The manager daemon auto-generates an MCP config at `~/.kanban/mcp/kanban_mcp.json`.

For manual setup, copy a pre-made config and edit the path:

```bash
# Claude Code (Linux)
cp mcp_configs/claude_code.json ~/.config/claude/settings.json

# Codex
cp mcp_configs/codex.json ~/.codex/config.json

# OpenCode
cp mcp_configs/opencode.json ~/.opencode/config.json

# Gemini CLI
cp mcp_configs/gemini_cli.json ~/.gemini/config.json
```

### 3. Environment Variables

MCP tools authenticate via `KANBAN_AGENT_NAME` (set automatically by the daemon):

```bash
export KANBAN_API_BASE="http://localhost:8000"
# KANBAN_AGENT_NAME is set automatically by the daemon
```

Provider keys (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, etc.) are inherited
from your shell — no additional Kanban-issued key is needed.

### 4. Available MCP Tools

| Tool | Description | Who |
|---|---|---|
| `create_project` | Create a new project | Manager+ |
| `get_projects` | List all projects | Anyone |
| `get_project_details` | Get project with stages and tasks | Anyone |
| `create_task` | Create a task in a project | Worker+ |
| `get_tasks` | List tasks with filters | Anyone |
| `get_task_details` | Task details with comments and logs | Anyone |
| `approve_project` | Approve a pending project | Manager+ |
| `move_task` | Move task to a different stage | Worker+ |
| `assign_task` | Assign an entity to a task | Worker+ |
| `add_comment` | Add a comment to a task | Worker+ |
| `get_my_tasks` | Get tasks assigned to caller | Anyone |
| `get_pending_events` | Poll for recent events | Anyone |
| `register_subscription` | Subscribe to event types | Anyone |
| `list_agents` | List all registered agents | Anyone |
| `list_entities` | List all entities | Anyone |
| `report_status` | Report agent heartbeat status | Anyone |
| `log_activity` | Log an activity entry | Anyone |
| `get_agent_statuses` | Get all agent heartbeats | Manager+ |
| `get_activity_feed` | Get recent activity feed | Anyone |
| `request_diff_review` | Request a critical-path diff review | Anyone |
| `review_diff` | Approve / reject / request changes on a diff review | Manager+ |
| `get_diff_reviews` | List pending or recent diff reviews | Anyone |
| `request_approval` | Bubble a CLI permission prompt into the durable approval queue | Anyone |
| `get_pending_approvals` | List pending approvals (filterable by project/agent/task) | Anyone |
| `resolve_approval` | Approve / reject / cancel a pending approval | Manager+ (cancel: requester) |

---

## Adapter Registry

Every agent tool is described by a YAML file. No Python changes needed to add a new agent.

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

### Manage adapters

```bash
python -m kanban_cli agents list     # Show installed adapters
```

### Example adapter spec (`agents/claude.yaml`)

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

protocol: mcp

auth:
  type: env_key
  env_var: ANTHROPIC_API_KEY

roles: [manager, worker]
modes: [supervised, auto, headless]

reporting:
  heartbeat_interval: 30
```

---

## Authentication & RBAC

Local-first identity — no Kanban-issued secrets:

| Header / Env | Used By | Purpose |
|---|---|---|
| `X-Entity-ID` | Humans | Local-first entity resolution |
| `KANBAN_AGENT_NAME` | MCP agents | Adapter name looked up against `Entity.name` |
| `X-API-Key` | (Remote mode) | Optional API key auth (`Entity.api_key`) |

`X-API-Key` is reserved for future remote/multi-user mode. The default
local install never uses it.

### Roles

| Role | Permissions |
|---|---|
| `OWNER` | Full control, user management, project approval |
| `MANAGER` | Project approval, task assignment, stage management |
| `WORKER` | Task creation (approved projects only), self-assignment, updates |
| `VIEWER` | Read-only |

### Scope (API-key tied)

`Entity.scope` (`OWNER`/`MANAGER`/`WORKER`/`READONLY`) restricts permissions even if the entity's `Role` is higher. A `MANAGER` entity with `WORKER` scope behaves as `WORKER`.

---

## Agent Activity Visibility

The Kanban board includes a live **Agent Activity sidebar** showing:
- Current status of all agents (idle, thinking, working, blocked, waiting, done)
- Current task each agent is working on
- Last message from each agent
- "x seconds ago" timestamp
- Collapsible activity log (thoughts, actions, observations, results, errors)

Agents report status via MCP:
```bash
report_status(agent_id=1, status_type="working", message="Analyzing requirements", task_id=54)
```

Or via REST:
```bash
POST /agents/1/status
{"status_type": "working", "message": "Analyzing requirements", "task_id": 54}
```

---

## API Endpoints

### Projects
- `POST /projects` — Create project (requires auth)
- `GET /projects` — List projects
- `GET /projects/{id}` — Get project details
- `PATCH /projects/{id}` — Update project
- `DELETE /projects/{id}` — Delete project
- `POST /projects/{id}/approve` — Approve pending project (manager+)
- `POST /projects/{id}/reject` — Reject pending project (manager+)

### Stages
- `POST /projects/{id}/stages` — Add stage (manager+)
- `PATCH /stages/{id}` — Update stage (manager+)
- `DELETE /stages/{id}` — Delete stage (manager+)

### Tasks
- `POST /tasks` — Create task (worker+, approved projects only)
- `GET /tasks` — List tasks
- `GET /tasks/available` — Get open tasks
- `GET /tasks/{id}` — Get task details
- `PATCH /tasks/{id}` — Update task (creator or assignee)
- `DELETE /tasks/{id}` — Delete task
- `POST /tasks/{id}/assign` — Assign task to entity
- `POST /tasks/{id}/self-assign` — Self-assign task
- `DELETE /tasks/{id}/unassign/{entity_id}` — Unassign task

### Comments
- `POST /comments` — Add comment
- `GET /tasks/{id}/comments` — Get comments

### Entities
- `POST /entities/register/human` — Register a human
- `POST /entities/register/agent` — Register an agent
- `GET /entities` — List all entities
- `GET /entities/me` — Get current entity
- `DELETE /entities/{id}` — Delete entity

### Agent Activity
- `GET /agents/status` — Get all agent heartbeats
- `GET /agents/activity` — Get activity feed
- `POST /agents/{id}/status` — Update agent heartbeat
- `POST /agents/{id}/activity` — Log agent activity
- `GET /agents/sessions` — List CLI agent sessions (filter by project/task/agent)
- `GET /agents/sessions/{session_id}/terminal` — Session terminal stream
- `GET /agents/tasks/{task_id}/active-session` — Active agent session bound to a task

### Diff Reviews
- `GET /agents/projects/{project_id}/diff-reviews` — List diff reviews for a project
- `POST /agents/projects/{project_id}/diff-reviews` — Request a diff review
- `PATCH /agents/diff-reviews/{review_id}` — Approve / reject / request changes

### Approval Queue
- `POST /agents/approvals` — Request human approval for a CLI prompt (shell command, file write, network access, git push, PR create, tool call, …)
- `GET /agents/approvals` — List approvals (filter by `project_id`, `agent_id`, `task_id`, `session_id`, `status_filter`)
- `PATCH /agents/approvals/{approval_id}/resolve` — Approve / reject / cancel; flips the blocked agent session back to active

### Project PR Sync
- `POST /agents/projects/{project_id}/contributions/sync/github` — Sync PRs, issues, reviews, and commits for the project workspace via `gh` (with a `git log` fallback for unpushed commits)

### Workspace Open
- `POST /ui/api/open-workspace` — Open a project's workspace folder via the platform-native opener (`xdg-open` / `open` / `explorer`); restricted to registered project paths

### A2A (Agent-to-Agent)
- `GET /a2a/agents` — List agents
- `POST /a2a/handoff/{task_id}` — Hand off task to another agent
- `POST /a2a/delegate` — Create and delegate new task
- `POST /a2a/message` — Send message to agent
- `GET /a2a/messages?agent_id={id}` — Get pending messages

### Settings
- `GET /ui/api/settings` — Get current manager/worker config

---

## File Structure

```
agent-kanban-pm/
├── main.py                      # FastAPI app
├── mcp_server.py                # MCP stdio server for CLI tools
├── open_project.py              # Register folders as projects
├── agent_inbox.py               # Terminal notifier daemon
├── event_bus.py                 # Async pub/sub with persistent fallback
├── adapters.py                  # WebSocket + Webhook broadcast adapters
├── a2a.py                       # Agent registry and handoff
├── auth.py                      # RBAC: roles, scopes, require_role()
├── database.py                  # SQLAlchemy setup + migrations
├── models.py                    # DB models (Entity, Project, Task, etc.)
├── schemas.py                   # Pydantic schemas
├── websocket_manager.py         # WebSocket connections
├── sync_agents.py               # Adapter registry sync entrypoint
├── requirements.txt
├── .env
├── README.md                    # This file
├── CHANGELOG.md                 # Release history
├── AGENTS.md                    # Architecture spec (source of truth)
├── MCP_SETUP.md                 # MCP setup guide
├── agents/                      # Bundled adapter YAMLs
│   ├── claude.yaml
│   ├── gemini.yaml
│   ├── opencode.yaml
│   └── codex.yaml
├── kanban_runtime/              # Runtime modules
│   ├── adapter_loader.py        # YAML scanner + DB sync
│   ├── preferences.py           # preferences.yaml loader
│   └── manager_daemon.py        # Manager daemon with restart loop
├── kanban_cli/                  # CLI package
│   ├── __init__.py              # init, agents list, daemon commands
│   └── __main__.py              # Entry point
├── routers/                     # API routes
│   ├── auth.py
│   ├── entities.py
│   ├── projects.py
│   ├── tasks.py
│   ├── stages.py
│   ├── websockets.py
│   ├── ui.py
│   ├── agent_connections.py
│   └── agent_activity.py
├── templates/                   # Jinja2 templates
│   └── kanban_board.html        # Kanban UI with agent activity sidebar
└── mcp_configs/                 # Pre-made MCP configs
```

---

## Architecture Rules

1. **The server never decides.** Routing, assignment, prioritization, skill matching — all done by the manager agent via MCP tools, not by server-side Python.
2. **Adapters are data, not code.** Adding a new agent never requires editing `.py` files. If it does, the adapter spec is missing a field.
3. **Heartbeats are mandatory for workers.** Any agent that takes a task must call `report_status` at least every `heartbeat_interval` seconds (declared in its adapter). Stale heartbeats = manager reassigns.
4. **No new hardcoded tool names.** No `if agent.name == "claude"` branches anywhere. Capabilities live in the adapter YAML.
5. **No Kanban-issued secrets in the local-first path.** Provider keys live in env (declared in adapter YAML). Kanban identity is `KANBAN_AGENT_NAME`. Anything that generates a token for the local server is wrong unless it's behind a future `remote_mode=true` flag.

---

## Honest Limitations

1. **The Kanban board doesn't write code.** It tracks tasks. You (or Claude Code that you run) do the work.
2. **MCP agents must poll.** CLI tools are ephemeral — they can't receive pushes. Use `get_pending_events` or `get_my_tasks`.
3. **A2A messages are in-memory.** Lost on server restart.
4. **Local only.** Not designed for remote access. The auth model assumes OS-level process isolation.
5. **SQLite.** Suitable for local use. Switch to PostgreSQL for multi-user:
   ```
   DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname
   ```

---

## License

MIT License
