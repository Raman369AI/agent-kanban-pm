# Architecture

Agent Kanban PM is a local-first state store for human and CLI-agent project
work. The server records facts and broadcasts events; the selected
orchestrator decides what happens next.

## Runtime

```text
┌────────────────────────────────────────────────────────────┐
│ Local machine                                               │
│                                                            │
│  ┌───────────────┐      REST / WS / MCP      ┌───────────┐ │
│  │ Browser UI    │◄────────────────────────►│ FastAPI   │ │
│  └───────────────┘                           │ server    │ │
│                                              │ + SQLite  │ │
│  ┌───────────────┐       events/state        └─────┬─────┘ │
│  │ Orchestrator  │◄───────────────────────────────┘       │
│  │ CLI agent     │                                        │
│  └───────┬───────┘                                        │
│          │ assigns / comments / moves cards                │
│          ▼                                                 │
│  ┌───────────────────────────────────────────────────────┐ │
│  │ Role supervisor                                       │ │
│  │ tmux sessions per selected role/task                  │ │
│  └───────┬────────────┬────────────┬────────────┬────────┘ │
│          ▼            ▼            ▼            ▼          │
│       Worker         UI        Test/Review      Git        │
│       agent        agent          agents       agent       │
└────────────────────────────────────────────────────────────┘
```

## Data Flow

```text
Human request
  │
  ▼
Board chat / task API
  │
  ▼
Orchestrator agent plans work
  │
  ├─► creates or updates cards
  ├─► assigns a role/agent
  └─► records decisions
        │
        ▼
Assignment event
        │
        ▼
Supervisor starts task session in tmux
        │
        ├─► heartbeats and activity stream
        ├─► terminal output in UI
        └─► approval prompts become queue items
```

## Boundaries

```text
Server owns:
  state, events, API/MCP surfaces, sessions, approvals, terminal streams

Orchestrator owns:
  routing, task splitting, assignment, card movement, escalation

Specialist agents own:
  bounded implementation, UI work, testing, review, Git/PR actions

Human owns:
  project intent, approvals, final review, release decisions
```

## Package Layout

```text
kanban_cli/                 CLI entrypoint and local commands
kanban_runtime/             supervisor, adapter loading, handoff helpers
kanban_runtime/data/        packaged templates, static files, adapters, MCP configs
routers/                    REST/UI/WebSocket route modules
models.py / schemas.py      database and API contracts
mcp_server.py               MCP tool surface for local agents
```
