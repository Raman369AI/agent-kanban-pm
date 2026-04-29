# How to Run Agent Kanban PM — Step by Step

This document is the definitive guide to getting the system running from a fresh clone.

---

## 1. Prerequisites Check

Before starting, verify you have:

```bash
python3 --version      # Should be 3.10+
pip --version
```

Optional but recommended — at least one AI CLI tool:

```bash
which claude      # Anthropic Claude Code
which gemini      # Google Gemini CLI
which opencode    # OpenCode
which codex       # OpenAI Codex
```

If none are installed, the server will still start but no agents will be marked `active`.

---

## 2. Install Dependencies

```bash
cd /home/kronos/Desktop/agent-kanban-pm
pip install -r requirements.txt
```

If you plan to use MCP with CLI tools, also install:

```bash
pip install mcp
```

---

## 3. Initialize the System

Run the setup wizard. This is required before the server can start.

```bash
python -m kanban_cli init
```

### What the wizard does:
1. Copies bundled adapter YAMLs to `~/.kanban/agents/`
2. Scans which CLI tools are actually installed (`shutil.which`)
3. Asks you to pick a manager agent (must have `manager` role)
4. Asks you to pick a mode (`supervised` / `auto` / `headless`)
5. Asks you to pick worker agents
6. Writes `~/.kanban/preferences.yaml`

### Example output:
```
============================================================
KANBAN INITIALIZATION WIZARD
============================================================

Available agents:
  1. Claude (Anthropic) (roles: manager, worker) [can be manager]
  2. Gemini (Google) (roles: worker)
  3. OpenCode (roles: worker)

Select manager agent (default: Claude (Anthropic)):

Manager mode:
  1. supervised
  2. auto
  3. headless
Select mode [2]:

Select worker agents (comma-separated numbers, or 'all'):

============================================================
SETUP COMPLETE
============================================================
Manager:     Claude (Anthropic)
Mode:        auto
Workers:     gemini, opencode
Config:      /home/kronos/.kanban/preferences.yaml

Then run: python -m kanban_cli daemon
```

### Verify the config:

```bash
cat ~/.kanban/preferences.yaml
```

Should look like:
```yaml
manager:
  agent: claude
  model: claude-sonnet-4-6
  mode: auto

workers:
  - agent: gemini
    roles: [worker]
  - agent: opencode
    roles: [worker]

autonomy:
  require_approval_for: []
  auto_approve: [task_move, task_assign, comment]
```

---

## 4. Start the Server

### Terminal 1 — Start the Kanban Server

```bash
cd /home/kronos/Desktop/agent-kanban-pm
python main.py
```

Expected output:
```
INFO:     Started server process [XXXXX]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

The server:
- Creates `kanban.db` (SQLite) in the project directory
- Auto-syncs adapter YAMLs to database entities
- Starts the heartbeat staleness sweeper (every 60s)
- Starts the event bus background worker

Leave this terminal running.

### Verify the server is up:

```bash
curl http://localhost:8000/health
# → {"status":"healthy"}
```

---

## 5. Register a Human User

You need at least one human entity to create projects. In a new terminal:

```bash
curl -X POST http://localhost:8000/entities/register/human \
  -H "Content-Type: application/json" \
  -d '{"name": "You", "entity_type": "human"}'
```

Save the returned `id`. For example:
```json
{"name": "You", "entity_type": "human", "id": 5, "role": "owner", ...}
```

Your human ID is `5`. Use it as `X-Entity-ID: 5` in all subsequent requests.

### Verify entities:

```bash
curl http://localhost:8000/entities
```

You should see:
- Your human entity (`role: owner`)
- Adapter entities (`claude`, `gemini`, etc.) loaded from YAMLs

---

## 6. Create and Approve a Project

Projects start as `PENDING`. Only `MANAGER` or `OWNER` can approve them.

```bash
# Create project (replace 5 with your human ID)
curl -X POST http://localhost:8000/projects \
  -H "Content-Type: application/json" \
  -H "x-entity-id: 5" \
  -d '{"name": "First Project", "description": "Getting started"}'
```

Response:
```json
{"id": 1, "name": "First Project", "approval_status": "PENDING", ...}
```

Approve it:
```bash
curl -X POST http://localhost:8000/projects/1/approve \
  -H "x-entity-id: 5"
```

Response:
```json
{"id": 1, "approval_status": "APPROVED", ...}
```

---

## 7. Create Tasks

```bash
curl -X POST http://localhost:8000/tasks \
  -H "Content-Type: application/json" \
  -H "x-entity-id: 5" \
  -d '{"title": "Set up CI/CD pipeline", "project_id": 1}'
```

Response:
```json
{"id": 1, "title": "Set up CI/CD pipeline", "status": "pending", ...}
```

---

## 8. View the Kanban Board

Open your browser:

```
http://localhost:8000/ui/projects
```

Or view a specific project:

```
http://localhost:8000/ui/projects/1
```

You should see:
- Kanban columns: Backlog, To Do, In Progress, Review, Done
- Your task in "Backlog"
- Agent Activity sidebar (empty until agents report status)

---

## 9. Report Agent Status (Optional Demo)

Simulate an agent reporting its status via REST:

```bash
# Find an active agent ID from step 5
curl -X POST http://localhost:8000/agents/2/status \
  -H "Content-Type: application/json" \
  -H "x-entity-id: 5" \
  -d '{"status_type": "working", "message": "Analyzing requirements", "task_id": 1}'
```

Now refresh the Kanban board. The Agent Activity sidebar should show:
- Agent name
- Status badge: `WORKING`
- Message: "Analyzing requirements"
- Link to Task #1

---

## 10. (Optional) Start the Manager Daemon

If you want the manager agent to actively route work via MCP:

### Terminal 2 — Start daemon

```bash
# Start the daemon
python -m kanban_cli daemon
```

The daemon will:
1. Read `~/.kanban/preferences.yaml`
2. Resolve the manager's CLI command (e.g., `claude`)
3. Spawn the manager process with env vars:
   - `KANBAN_AGENT_NAME=claude`
   - `KANBAN_AGENT_ROLE=manager`
   - `KANBAN_API_BASE=http://localhost:8000`
4. Auto-generate `~/.kanban/mcp/kanban_mcp.json`
5. Enter a restart loop (exponential backoff on crash)

### Check daemon status:

```bash
python -m kanban_cli daemon status
```

### Stop daemon:

```bash
python -m kanban_cli daemon stop
```

---

## 11. Using MCP from Your CLI Tool

Once the daemon is running, your CLI tool (e.g., Claude Code) can call MCP tools.

Example conversation:

**You:** List all projects

**Claude:** *calls `get_projects`* → Shows "First Project" (APPROVED)

**You:** Create a task "Write tests" in project 1

**Claude:** *calls `create_task`* → Task #2 created

**You:** What tasks are assigned to me?

**Claude:** *calls `get_my_tasks`* → Shows your tasks

**You:** Mark task 2 as completed

**Claude:** *calls `move_task`* → Task moved to Done

---

## 12. Common Issues

### Port 8000 already in use

```bash
lsof -i :8000   # Find process
kill -9 <PID>   # Kill it
```

### No adapters found

```bash
# Re-run init
python -m kanban_cli init

# Check if YAMLs exist
ls ~/.kanban/agents/
```

### 401 Authentication Required

You forgot `X-Entity-ID` header on a mutation endpoint:

```bash
# Wrong
curl -X POST http://localhost:8000/projects -d '{...}'

# Right
curl -X POST http://localhost:8000/projects \
  -H "x-entity-id: 5" \
  -d '{...}'
```

### Agent shows as inactive

The adapter loader checks `shutil.which(command)` — if the CLI tool isn't in your PATH, the entity is marked `is_active=False`. Install the tool or add it to PATH.

### Database locked (SQLite)

SQLite doesn't handle concurrent writes well. If you see "database is locked", wait a moment and retry. For heavy use, switch to PostgreSQL:

```bash
export DATABASE_URL="postgresql+asyncpg://user:pass@localhost/dbname"
```

---

## 13. Testing

Run the test suite:

```bash
# RBAC tests
python test_rbac.py

# Phase 1: Activity visibility
python test_phase1.py

# Phase 2: Adapter registry
python test_phase2.py

# Full integration
python test_integration.py

# Phase 6: Env-based identity
python test_phase6.py

# Live smoke test (starts/stops server automatically)
python smoke_test.py
```

---

## 14. Shutdown

1. Stop the manager daemon (if running):
   ```bash
   python -m kanban_cli daemon stop
   ```

2. Stop the Kanban server:
   ```bash
   # Press Ctrl+C in Terminal 1
   ```

3. Clean up (optional):
   ```bash
   rm kanban.db        # Delete database
   rm -rf ~/.kanban/   # Delete config
   ```

---

## Summary of Commands

```bash
# One-time setup
pip install -r requirements.txt
python -m kanban_cli init

# Start server (Terminal 1)
python main.py

# Register yourself (Terminal 2)
curl -X POST http://localhost:8000/entities/register/human \
  -d '{"name": "You", "entity_type": "human"}'

# Create project
curl -X POST http://localhost:8000/projects \
  -H "x-entity-id: 5" \
  -d '{"name": "My Project"}'

# Approve project
curl -X POST http://localhost:8000/projects/1/approve \
  -H "x-entity-id: 5"

# Create task
curl -X POST http://localhost:8000/tasks \
  -H "x-entity-id: 5" \
  -d '{"title": "Do the thing", "project_id": 1}'

# Open browser
# http://localhost:8000/ui/projects

# Start manager daemon (optional)
python -m kanban_cli daemon
```
