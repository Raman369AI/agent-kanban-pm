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

## Trust and Security Model

The statements in this section and the following consistency/recovery sections
are required architectural invariants. Where the current alpha does not yet
enforce one, the gap is tracked explicitly in `PLAN.md`; these are not claims
that every hardening item is already implemented.

The supported topology is a single-user process on one trusted local machine.
The HTTP server should bind to loopback by default. The browser, server, and
locally launched agent processes are separate trust boundaries even when they
share an OS account:

- REST, UI mutation, and WebSocket access require the per-instance token.
- Human identity headers select an application identity; they are not a
  substitute for transport authentication.
- MCP agent identity comes from launcher-controlled environment variables and
  must be revalidated against active database state on every privileged call.
- Worktrees isolate Git branches, not the host filesystem, credentials,
  network, or arbitrary shell commands. Supervised agent permissions are the
  safe default; automatic approval must be an explicit user choice.
- Host-header validation and CSRF protection are required before treating the
  browser UI as hardened against hostile webpages or DNS rebinding.

Remote multi-user hosting, untrusted OS users, and multiple server replicas are
not supported by the current SQLite/local-process architecture.

## Consistency and Event Semantics

SQLite is the durable source of truth. In-process events are notifications of
committed state, not an independent source of truth. Event handlers must be
idempotent because delivery can be repeated during retries or recovery.

- State changes and their audit records should commit in one transaction.
- Events should be published only after the corresponding commit succeeds.
- Assignment admission must atomically enforce active lease/session invariants;
  check-then-create logic alone is insufficient under concurrent events.
- Task optimistic versions protect conflicting card edits. Session and lease
  uniqueness constraints protect duplicate launches.
- Terminal activity is append-only and may be sampled; task, session, lease,
  checkpoint, and handoff state determine lifecycle decisions.

The current runtime is designed for one server process. Supporting multiple
processes or hosts would require a durable event/outbox mechanism, distributed
locking, and a process runner that is not held only in local memory.

## Failure and Recovery

On startup and periodically while running, the supervisor reconciles durable
session state with tmux/native PTY processes and worktree handoff files.
Recovery must be safe to repeat:

- A missing process with an active session becomes stopped or failed; it must
  not be silently treated as completed.
- A stale heartbeat or lease is released only after confirming that its runner
  is gone or its expiry policy has been met.
- `STATUS.md` advances a task only when it is valid, belongs to the expected
  task/session, and explicitly reports a handoff-ready terminal state.
- Git fetch/rebase conflicts are recorded as activity and leave the task in a
  recoverable blocked state; destructive conflict resolution is never implied.
- Launch retries reuse or reconcile the existing task worktree and session
  identity rather than creating parallel duplicates.
- Database engines, streamers, and PTY/tmux watchers are closed during
  application shutdown before the event loop exits.

If reconciliation cannot determine whether work completed, the task remains in
its current stage and is surfaced for human/orchestrator review.

## Operational Constraints

- Deployment: one FastAPI server, one SQLite database, one local process-runner
  host, and any number of bounded agent sessions within configured limits.
- Supported runtime: Unix-like systems with Git; tmux is preferred and native
  PTY is the fallback. Windows requires WSL until a native runner exists.
- Git and tmux/PTY operations must not block the asyncio event loop or execute
  while a long-lived database transaction is open.
- SQLite WAL and a busy timeout reduce contention but do not replace explicit
  transaction boundaries and uniqueness constraints.
- The database and instance configuration belong in a user data directory in
  installed mode. Source-checkout-relative state is a development behavior.

## Architectural Evolution

The next structural step is a single `agent_kanban_pm` package with shared
service modules for tasks, projects, sessions, approvals, and transitions.
REST and MCP adapters should call these services rather than maintaining
parallel business rules. Database schema evolution should use Alembic, while
subprocess execution should use async subprocesses or worker threads outside
database transactions.

## Package Layout

```text
src/agent_kanban_pm/cli/     CLI entrypoint and local commands
src/agent_kanban_pm/runtime/ launcher, streamer, supervisor, adapters, handoff helpers
src/agent_kanban_pm/data/    packaged templates, static files, adapters, MCP configs
src/agent_kanban_pm/routers/ REST/UI/WebSocket route modules
src/agent_kanban_pm/models.py / schemas.py database and API contracts
src/agent_kanban_pm/mcp/server.py MCP tool surface for local agents
```
