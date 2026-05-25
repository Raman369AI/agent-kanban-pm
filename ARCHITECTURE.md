# Architecture

Agent Kanban PM is a local-first coordination runtime for human and CLI-agent
project work. The server records durable state, exposes REST/WebSocket/MCP
interfaces, starts assigned local agents, streams their terminal output, and
applies deterministic lifecycle rules such as session completion and standard
stage handoff. The orchestrator agent owns planning, task splitting, assignment
strategy, and escalation decisions.

## Runtime

```text
┌──────────────────────────────────────────────────────────────────┐
│ Local machine                                                     │
│                                                                  │
│  ┌───────────────┐        REST / WS          ┌─────────────────┐ │
│  │ Browser UI    │◄────────────────────────►│ FastAPI server   │ │
│  └───────────────┘                           │ + SQLite        │ │
│                                              └───────┬─────────┘ │
│  ┌───────────────┐          MCP / REST               │ events    │
│  │ Orchestrator  │◄──────────────────────────────────┘ state     │
│  │ CLI agent     │                                               │
│  └───────┬───────┘                                               │
│          │ creates cards, assigns roles, records decisions        │
│          ▼                                                       │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │ Assignment launcher + session streamer                      │ │
│  │ tmux when available, native PTY fallback otherwise           │ │
│  └───────┬────────────┬───────────────┬──────────────┬────────┘ │
│          ▼            ▼               ▼              ▼          │
│       Worker          UI          Test/Review       Git/PR       │
│       agent         agent            agents         agent        │
│          │            │               │              │          │
│          └──── per-task git worktrees + STATUS.md handoff ──────┘
└──────────────────────────────────────────────────────────────────┘
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
TASK_ASSIGNED event
        │
        ▼
Assignment launcher starts task session
  │
  ├─► creates/reuses per-task git worktree and branch
  ├─► rebases from detected base ref when possible
  ├─► starts CLI in tmux or PTY fallback
  └─► writes/updates STATUS.md handoff file
        │
        ▼
Session streamer watches terminal + STATUS.md
  │
  ├─► writes terminal output to AgentActivity
  ├─► updates AgentHeartbeat / AgentCheckpoint
  ├─► captures approval prompts into AgentApproval
  └─► on handoff_ready=true:
        ├─ worker complete: move In Progress/To Do -> Review
        ├─ assign Review-stage roles (test, diff_review) when configured
        ├─ review complete: move Review -> Done
        └─ assign Done-stage roles (git_pr) when configured
```

## State Model

```text
Project
  ├─ Stage: Backlog, To Do, In Progress, Review, Done
  ├─ Task: status, stage, assignees, optimistic version
  ├─ StagePolicy: expected roles, outputs, review mode
  └─ ProjectWorkspace: local repo/workspace roots

Agent session
  ├─ AgentSession: durable process/session row
  ├─ AgentHeartbeat: current agent state
  ├─ AgentActivity: append-only terminal/tool/activity feed
  ├─ AgentCheckpoint: restart context and terminal tail
  ├─ AgentApproval: durable approval prompt queue
  └─ STATUS.md: worktree-local handoff summary and completion signal
```

## Boundaries

```text
Server/runtime owns:
  state, events, API/MCP surfaces, sessions, terminal streams, approval queue,
  per-task worktree setup, launch/rebase bookkeeping, and deterministic stage
  handoff after a session reports completion

Orchestrator owns:
  routing strategy, task splitting, role/agent assignment, prioritization,
  escalation, and non-standard card movement decisions

Specialist agents own:
  bounded implementation, UI work, testing, review, Git/PR actions, and accurate
  STATUS.md handoff updates

Human owns:
  project intent, role configuration, sensitive approvals, final review, and
  release decisions
```

## Package Layout

```text
kanban_cli/                 CLI entrypoint and local commands
kanban_runtime/             launcher, streamer, supervisor, adapters, handoff helpers
kanban_runtime/data/        packaged templates, static files, adapters, MCP configs
routers/                    REST/UI/WebSocket route modules
models.py / schemas.py      database and API contracts
mcp_server.py               MCP tool surface for local agents
```
