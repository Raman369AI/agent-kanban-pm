# MCP Setup Guide for Agent Kanban PM

This guide explains how to connect Claude Code, Codex, OpenCode, and Gemini CLI to the Kanban PM system via MCP.

## Prerequisites

1. The main FastAPI server should be running (or at least the database initialized):
   ```bash
   python main.py
   ```

2. Install MCP dependencies:
   ```bash
   pip install mcp
   ```

3. You need at least one entity registered in the system. The MCP server will auto-detect defaults, but it's good to have entities:
   ```bash
   # Register a human
   curl -X POST "http://localhost:8000/entities/register/human" \
     -H "Content-Type: application/json" \
     -d '{"name": "User", "entity_type": "human"}'

   # Register an agent
   curl -X POST "http://localhost:8000/entities/register/agent" \
     -H "Content-Type: application/json" \
     -d '{"name": "Claude", "entity_type": "agent", "skills": "coding"}'
   ```

## Important: No Authentication Needed

This system is **local-only**. The MCP server uses local process identity for creator tracking and self-assignment.

---

## Claude Code (Anthropic)

### Configuration

Claude Code uses a JSON config file. The location depends on your OS:

- **macOS**: `~/Library/Application Support/Claude/settings.json`
- **Linux**: `~/.config/claude/settings.json`
- **Windows**: `%APPDATA%\Claude\settings.json`

Add the MCP server to your settings:

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "DATABASE_URL": "sqlite+aiosqlite:///./kanban.db"
      }
    }
  }
}
```

Or copy the provided template:
```bash
cp mcp_configs/claude_code.json ~/.config/claude/settings.json
# EDIT THE FILE: replace the path with your absolute path
```

### Usage

After restarting Claude Code, you can ask:
- "List all projects"
- "Create a task called 'Fix bug #42' in project 1"
- "Show me my pending events"

---

## Codex (OpenAI)

### Configuration

Codex uses `~/.codex/config.json`:

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "DATABASE_URL": "sqlite+aiosqlite:///./kanban.db"
      }
    }
  }
}
```

Or copy the template:
```bash
cp mcp_configs/codex.json ~/.codex/config.json
# EDIT THE FILE: replace the path with your absolute path
```

---

## OpenCode

### Configuration

OpenCode uses `~/.opencode/config.json`:

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "DATABASE_URL": "sqlite+aiosqlite:///./kanban.db"
      }
    }
  }
}
```

Or copy the template:
```bash
cp mcp_configs/opencode.json ~/.opencode/config.json
# EDIT THE FILE: replace the path with your absolute path
```

---

## Gemini CLI (Google)

### Configuration

Gemini CLI uses `~/.gemini/config.json`:

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "DATABASE_URL": "sqlite+aiosqlite:///./kanban.db"
      }
    }
  }
}
```

Or copy the template:
```bash
cp mcp_configs/gemini_cli.json ~/.gemini/config.json
# EDIT THE FILE: replace the path with your absolute path
```

---

## Notes

### Absolute Paths Required
All config files must use **absolute paths** to `mcp_server.py`. Relative paths will fail.

### Database Path
If your working directory is not `/home/kronos/Desktop/agent-kanban-pm`, update the `DATABASE_URL` environment variable in the config to point to the correct `kanban.db` file:
```json
"env": {
  "DATABASE_URL": "sqlite+aiosqlite:///ABSOLUTE/PATH/TO/kanban.db"
}
```

### Polling for Events
MCP stdio servers **cannot push** events to CLI agents. To see what's new, agents must explicitly call:
- `get_pending_events(agent_id=YOUR_AGENT_ID)`

Register your subscriptions first with:
- `register_subscription(agent_id=YOUR_AGENT_ID, events=["task_created", "task_assigned"])`

Or use `["*"]` to subscribe to all events.

### Agent ID
When you register an agent via `POST /entities/register/agent`, the response includes `id`. Use this ID as `agent_id` in MCP tool calls. If omitted, the MCP server auto-detects the first agent.

### Troubleshooting

**"MCP library not available"**
- Run: `pip install mcp`

**"Database locked"**
- SQLite can have issues with concurrent access. Ensure the main FastAPI server is running, or switch to PostgreSQL.

**Tools don't appear in the CLI**
- Restart the CLI tool completely after editing the config.
- Check that the JSON config is valid (no trailing commas).
