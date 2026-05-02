# MCP Setup Guide for Agent Kanban PM

This guide explains how to connect Claude Code, Codex, OpenCode, and Gemini CLI to the Kanban PM system via MCP.

## Prerequisites

1. Start the Kanban server:
   ```bash
   python -m kanban_cli run
   ```
   Or start it directly:
   ```bash
   python main.py
   ```

2. Initialize your project and role assignments (first time):
   ```bash
   python -m kanban_cli init
   ```

3. Install MCP dependencies:
   ```bash
   pip install mcp
   ```

## Identity: Environment Variables

The MCP server uses **local process identity** via environment variables — no Kanban-issued keys or manual entity registration required.

| Env var | Purpose | Example |
|---|---|---|
| `KANBAN_AGENT_NAME` | **Required.** Identifies which adapter Entity this process acts as. Matched against `Entity.name`. | `claude`, `gemini`, `opencode` |
| `KANBAN_AGENT_ROLE` | Optional. The role this agent is filling. | `manager`, `worker` |
| `KANBAN_API_BASE` | Optional. Where the Kanban server lives. Defaults to `http://localhost:8000`. | `http://localhost:8000` |

**The MCP server will fail to start without `KANBAN_AGENT_NAME`.** Set it in the `env` block of every MCP config.

Entities are created automatically when you run `kanban init` or when the adapter loader syncs `~/.kanban/agents/*.yaml`. You do not need to register agents manually via curl.

---

## Claude Code (Anthropic)

Add the MCP server to your settings file (`~/Library/Application Support/Claude/settings.json` on macOS, `~/.config/claude/settings.json` on Linux):

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "KANBAN_AGENT_NAME": "claude",
        "KANBAN_AGENT_ROLE": "manager"
      }
    }
  }
}
```

---

## Codex (OpenAI)

Edit `~/.codex/config.json`:

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "KANBAN_AGENT_NAME": "codex",
        "KANBAN_AGENT_ROLE": "worker"
      }
    }
  }
}
```

---

## OpenCode

Edit `~/.opencode/config.json`:

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "KANBAN_AGENT_NAME": "opencode",
        "KANBAN_AGENT_ROLE": "worker"
      }
    }
  }
}
```

---

## Gemini CLI (Google)

Edit `~/.gemini/config.json`:

```json
{
  "mcpServers": {
    "kanban-pm": {
      "command": "python3",
      "args": [
        "/ABSOLUTE/PATH/TO/agent-kanban-pm/mcp_server.py"
      ],
      "env": {
        "KANBAN_AGENT_NAME": "gemini",
        "KANBAN_AGENT_ROLE": "worker"
      }
    }
  }
}
```

---

## Notes

### Absolute Paths Required
All config files must use **absolute paths** to `mcp_server.py`. Relative paths will fail.

### Polling for Events
MCP stdio servers **cannot push** events to CLI agents. To see what's new, agents must explicitly call:
- `get_pending_events(agent_id=YOUR_AGENT_ID)`

Register your subscriptions first with:
- `register_subscription(agent_id=YOUR_AGENT_ID, events=["task_created", "task_assigned"])`

Or use `["*"]` to subscribe to all events.

### Role Assignments
Use `python -m kanban_cli roles list` to see which adapters are assigned to which roles, and `python -m kanban_cli roles assign <role> --agent <name>` to change assignments. See `AGENTS.md` §2 for the full role taxonomy.

### Discovering Available CLI Tools
```bash
python -m kanban_cli agents discover
```
This lists popular CLI agents found on your PATH without registering them.

### Troubleshooting

**"MCP library not available"**
- Run: `pip install mcp`

**"Identity not found" / startup fails**
- Ensure `KANBAN_AGENT_NAME` is set in the MCP config's `env` block.
- The name must match an adapter in `~/.kanban/agents/` or be registered via `kanban init`.

**"Database locked"**
- Ensure the main FastAPI server is running, or use PostgreSQL for concurrent access.

**Tools don't appear in the CLI**
- Restart the CLI tool completely after editing the config.
- Check that the JSON config is valid (no trailing commas).