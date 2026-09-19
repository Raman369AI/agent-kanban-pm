# Agent Kanban PM

Local-first Kanban project management for humans and headless CLI agents.

Status: stable release (`0.5.0`) for local, single-user development.
The local runtime, board UI, per-task agent sessions, and MCP surface work and
are covered by tests, including a database upgrade path. It is a single-operator
tool by design: one shared token guards the local server, so do not expose it to
an untrusted network or share an instance with people you would not give shell
access.

The version in this checkout can be newer than the latest published package;
use the matching Git tag when you need to reproduce a particular release.

The server stores state, starts assigned local agents, streams terminal output,
and advances cards through the standard execution/review handoff. The selected
orchestrator agent still owns planning, task splitting, assignment strategy,
and escalation decisions. See [ARCHITECTURE.md](ARCHITECTURE.md) for the
system diagram.

## Requirements

- Python ≥ 3.11
- Linux or macOS. On Windows, use WSL; the local process runtime relies on
  Unix PTY/process semantics.
- `git`
- Recommended: `tmux` for detachable terminal sessions. If `tmux` is not
  available, the runtime uses its native PTY subprocess fallback.
- At least one CLI agent (Claude Code, Antigravity CLI, Codex, OpenCode, Aider, etc.)
- Optional: `gh` for GitHub PR/issue/review sync

| Platform | Status |
|---|---|
| Linux | CI-tested on Python 3.11, 3.12, and 3.13 |
| macOS | CI-tested on Python 3.12 |
| Windows | Use WSL; native Windows is not supported |

## Install

```bash
pip install agent-kanban-pm
kanban init
```

For an isolated CLI installation:

```bash
pipx install agent-kanban-pm
# Or run without installing:
uvx --from agent-kanban-pm kanban --help
```

From source:

```bash
git clone https://github.com/Raman369AI/agent-kanban-pm.git
cd agent-kanban-pm
pip install -e ".[dev]"
kanban init
```

## Run

```bash
kanban run                    # server + UI + role supervisor
kanban run --no-supervisor    # server + UI only
```

- UI: `http://localhost:8000/ui/projects`
- API docs: `http://localhost:8000/docs`

## Using the UI

1. Open **Projects**, create a project, and choose its workspace folder in
   the setup guide.
2. Open **Agents & roles** to configure an available worker. Use **New task**
   to create one card, then **Move to To Do** and assign the worker to start
   execution. **Activity** shows whether the session is queued, running,
   blocked, or failed.
3. Open a task card to read its description, change its stage, inspect output,
   or answer a pending approval. The dashboard's **Needs attention** list and
   the board's **Needs me** filter take you directly to tasks waiting for a
   decision.
4. Use **Plan work** for a larger request. Review and edit the proposed cards,
   remove or deselect any you do not want, then choose **Create selected
   tasks**. Canceling the preview creates nothing.

## Board controls

Click a card to open its task panel. Its link includes the task ID, so it
can be shared or bookmarked; browser Back closes the panel. Overview shows the
current task and execution state, while the other tabs contain approvals,
activity, terminal output, logs, and reviews. The task's terminal preview and
the Activity workbench show a short, de-duplicated Focused view by default;
the workbench's **Raw output** button reveals the recent captured entries.
Live output keeps your scroll position while you read. An edit draft stays
intact during a live refresh; if someone else changes the task, saving reports
the conflict.

Search by task title, ID, or #ID. Filters for **Needs me**, **Blocked**,
**Running**, **Unassigned**, agent, and priority use pending approvals and the
latest durable agent session where relevant. Matching counts and **Clear
filters** distinguish an empty project from a filter with no results. The
connection label shows when data is refreshing or reconnecting and returns to
**Live** after a successful refresh.

Cards can be moved by drag-and-drop, the stage selector in the task panel,
or keyboard. Press `Tab` until a card is focused, then use `Left Arrow` or
`Right Arrow` to move it to the adjacent stage. Dialogs keep focus inside,
close with `Escape`, and restore focus to their opener.

**Board view** and **List view** are remembered across reloads. Choose
**White** or **Night** and set density under **Appearance**. A rejected move
returns the card to its original stage and shows the server's reason.

## CLI

```bash
kanban roles list                                 # show role assignments
kanban roles assign worker opencode --mode headless # assign a role
kanban agents discover                            # find local CLIs
kanban sheet                                      # compact status
kanban audit                                      # what agents ran, and how
kanban audit --auto --commands                    # only unsupervised runs
kanban handoff status --workspace .               # inspect worktree state
```

## Per-task execution

Each Kanban task that's assigned to an agent runs in its own background process
session. `tmux` is used when available; otherwise the runtime falls back to a
native PTY subprocess manager. Terminal output is captured into `AgentActivity`
so the board workbench can show live progress without attaching to the shell.

For git projects, each task runs in an isolated worktree under
`~/.kanban/worktrees/project-{id}/task-{id}-{agent}` on a branch named
`kanban/task-{id}-{agent}`. The branch is started from the project's detected
base ref (`origin/HEAD`, then `origin/main`/`origin/master`, then a local
`main`/`master`).

Before each session starts the launcher:

1. Fetches the base ref when an `origin` remote exists.
2. Rebases the task branch onto the base so parallel tasks don't drift from
   mainline.
3. Records the result (`rebased onto ...`, `skipped (uncommitted changes)`,
   `aborted (conflicts)`, etc.) as an `AgentActivity` you can audit from the
   board.

If the project directory is not a git worktree, the agent runs in the project
folder directly with no git isolation.

## Stage handoff

The default board stages are Backlog, To Do, In Progress, Review, and Done.
Assignments and stage handoffs are durable database transactions:

1. An assignment records who authorized it. A queued worker starts from To Do
   or In Progress only while the project remains approved, the assignment is
   current, and its transition policy permits the move.
2. Completion requires a run-scoped handoff through
   `POST /agents/sessions/{id}/handoff` or a verified `STATUS.md`. A zero exit
   alone is an error, not a completion signal. Git implementations must be
   committed before handoff; launch prompts state the expected output labels.
3. Entering Review, by a permitted manual move or an automatic handoff, records
   configured `test`, `diff_review`, and `git_pr` assignments in the same
   transaction. Review roles run serially on the recorded implementation
   workspace/revision.
4. Automatic completion waits for every configured review role. Built-in outputs
   are checked against server-observed session role, revision, exit status, and
   summary; a formal diff-review decision must match the server-generated Git
   snapshot and its digest. A newer pending, rejected, or changes-requested
   decision supersedes an older approval for the same revision.
5. The task enters Done only after the Git/PR role submits one GitHub pull
   request artifact and the server verifies through `gh pr view` that it is
   merged and its head SHA is the reviewed implementation revision. Moving a
   card still never implicitly merges or publishes code.

REST, UI, MCP, and runtime execution starts share task-transition validation.
Stage-only and status-only moves cross the same policy boundary. Agent authority
comes from its stored role, not its name. Explicit human moves may override
missing workflow evidence, with the reason recorded in the task audit; they
cannot bypass authorization, project references, status/stage consistency, or
unfinished predecessors. Required output names describe evidence produced in
the stage being left, not work that must already exist before entering it.

Queued launches reserve an immutable command, workspace, and runner name tied
to the run token. A portable execution guard atomically records a durable claim
under `~/.kanban/runs/` before invoking the CLI. Receipts include host, guard,
child process identity, exit status, and a heartbeat. A server on the owning
host validates the live process without trusting a recycled PID; a server
reading a shared receipt from another host can recognize a fresh heartbeat and
later observe the recorded exit. Unknown or stale remote claims remain
fail-closed and are never replayed. The same receipt implementation supports
native Windows process identity, although the full application still requires
WSL because other terminal paths use Unix PTY semantics. This is at-most-once
execution per run token, not a guarantee that arbitrary external side effects
completed exactly once. Renewable database claim leases prevent multiple
dispatcher loops from concurrently owning the same event or launch request.
Preserve execution receipts while their sessions may be recovered.

Managers can inspect, cancel, retry, and archive launch requests from the
Activity workbench's **Launch queue** tab or through
`GET /agents/launch-requests` and the corresponding action endpoints. Cleanup
is non-destructive: archived requests remain available with
`include_archived=true`.

Task worktrees remain available after sessions end, including failed runs with
uncommitted changes. Automatic handoffs require committed implementation work;
the human review preview can still inspect unfinished work. Automatic cleanup
never removes a dirty worktree or an unowned manual worktree.

Open a task's **Reviews** tab to inspect its Git changes file by file. The
preview includes committed, staged, unstaged, and untracked text changes from
the task worktree. If the worktree is gone, it shows committed task-branch
changes or a saved review snapshot when available. An empty task branch cannot
reconstruct uncommitted changes from a removed worktree. Pending saved reviews
have **Approve** and **Reject** actions with optional notes. A decision closes
the review record; it does not apply the patch, move the task, commit, or merge.

Handoff follows each stage's `workflow_key` (`backlog`, `to_do`,
`in_progress`, `review`, `done`), not its label, so stages can be renamed
freely: a Done column relabelled "Shipped" still completes the tasks moved into
it. The key is derived from the name when a stage is created and is kept when it
is renamed; pass `workflow_key` when creating a custom stage to give it workflow
meaning. Stages with no recognised key leave a moved task's status unchanged.

The database session is the durable handoff source of truth. `STATUS.md` is
an optional input mechanism and may be deleted after its verified contents are
recorded. A known nonzero exit prevents success even if a handoff exists. A
surviving child remains active if its guard or terminal transport is lost, so
its workspace cannot be handed to another agent. Legacy sessions without a
stored launch specification are not automatically reconstructed. Task sessions
sharing a non-Git workspace are serialized.

Chat planning records its decisions and cards in the database without writing
to `STATUS.md`.

An explicit handoff uses `POST /agents/sessions/{id}/handoff` with
`project_id`, `task_id`, the session's `run_token`, `state`, and a
non-empty `summary`, plus the named `outputs` produced by the run. The session
agent or an owner/manager may submit it.

## Bundled agent adapters

Each adapter is a YAML file describing how to launch one CLI. `kanban init`
copies the bundled set into `~/.kanban/agents/`, and you can drop your own
file there without touching Python.

| Adapter | Command | Status |
|---|---|---|
| `claude` | `claude` | Supported |
| `antigravity` | `agy` | Supported — Google's current CLI |
| `codex` | `codex` | Supported |
| `opencode` | `opencode` | Supported |
| `aider` | `aider` | Supported |
| `goose`, `crush`, `continue` | — | Stubs; invocation not yet verified |

### Gemini CLI is retired

Google shut Gemini CLI down for consumer accounts on 2026-06-18 and replaced
it with **Antigravity CLI** (`agy`). The `gemini` adapter has been removed;
if a role still names it, reassign that role:

```bash
kanban roles assign worker antigravity --mode headless
```

One behaviour worth knowing: `agy --print` writes nothing when its stdout is a
pipe, so the orchestrator chat runs it on a pseudo-terminal
(`chat_designer.requires_tty: true` in the adapter). Task sessions were
already unaffected, since they run under tmux or a PTY.

## Autonomy & approval

Agents run **supervised** by default: the CLI keeps its approval prompts, and
risky actions (file writes, shell commands, git, network) surface in the
Kanban approval queue for a human or the orchestrator. Selectable CLI menus
also enter the queue. For Claude's optional browser setup, **Approve** opens
the extension installation page; **Reject** continues without browser tools.
The agent stays blocked until the request is resolved.

Auto mode is an explicit per-role opt-in. Set `autonomy: auto` on a role in
`~/.kanban/preferences.yaml` (or answer `y` at the autonomy prompt in
`kanban init`, or pass `--autonomy auto` to `kanban roles assign`). The
launcher then appends the adapter's bypass flags — `claude
--permission-mode bypassPermissions`, `agy --dangerously-skip-permissions`,
`codex --dangerously-bypass-approvals-and-sandbox`, `opencode --auto`,
`aider --yes-always`, declared as `task_command.auto_args` in the adapter YAML
— so the agent never pauses to ask. A per-task worktree isolates the task's
repository files; the database activity log records the launched command and
session, while a verified `STATUS.md` can supply a handoff summary.
Critical review and approval records can still be created through the
REST/MCP surfaces when an agent or human needs an explicit audit gate.

## Auditing what agents did

Every agent start is written to an append-only activity log with the resolved
command line, the workspace and branch, and the autonomy the session ran under.
When an agent does something surprising, that record is what tells you what
actually executed.

```bash
kanban audit                        # recent activity, oldest first
kanban audit --task 42              # one task's trail
kanban audit --auto                 # only sessions that ran with approvals off
kanban audit --commands             # only entries that recorded a command
kanban audit --since 24 --json      # last 24h, machine readable
```

`kanban audit` reads the database directly, so it still works when the server
is not running. The same data is available over HTTP at `GET /agents/activity`
(filters: `agent_id`, `project_id`, `task_id`, `session_id`, `activity_type`,
`has_command`, `limit`), which requires the Kanban token like every other API
route.

## UI & token security

The server binds to loopback and authenticates every non-page request with a
per-instance token (`~/.kanban/token`, owner-only `0600`). Browser pages
receive the token as an `HttpOnly`, `SameSite=strict` cookie; mutations
authenticated by that cookie must also send the `X-CSRF-Token` header
embedded in each page (wired automatically via a `fetch` wrapper in
`base.html`). CLI and supervisor processes use the `X-Kanban-Token` header,
which does not need the CSRF token. Requests with a non-loopback `Host`
header are rejected before routing (DNS-rebinding defense); extend with
`KANBAN_ALLOWED_HOSTS` if you deliberately serve a LAN hostname.

## MCP Identity

CLI agents connect through `agent_kanban_pm.mcp.server` using local process identity:

```bash
KANBAN_AGENT_NAME=codex KANBAN_AGENT_ROLE=worker kanban-mcp
```

`KANBAN_AGENT_NAME` must match an adapter entity loaded from
`~/.kanban/agents/`.

## Identity

- Humans: `X-Entity-ID` header
- Agents: `KANBAN_AGENT_NAME` env var
- Local-first, single-user. No auth server.

## Roles and adapters

Adapters are YAML files in `~/.kanban/agents/`. Adding a tool requires no
Python changes. Role assignments live in `~/.kanban/preferences.yaml`; the
standard roles are `orchestrator`, `ui`, `architecture`, `worker`, `test`,
`diff_review`, and `git_pr`.

Roles can also be edited from the board under **Advanced → Team roles**: each
role, custom roles included, has its own agent, model, session mode, and
approval setting, and saving one role leaves the others' unsaved edits alone.
Options the editor does not show, such as `owns` or `prompt_flag`, are kept on
save. The chosen model is passed to task and role sessions through the
adapter's `model_flag`; `default` and `<adapter>-default` mean the CLI's own
default, so no flag is sent.

## Development

Install the regular development tools and run the fast checks:

```bash
pip install -e ".[dev]"
flake8 src tests
pytest
python -m build
twine check dist/*
```

Browser regressions are an optional extra. They start an isolated local server
and cover drag-and-drop, keyboard movement, task editing, detailed error toasts,
dialog focus, approval resolution, board layout preferences, renamed stages, and
the role editor in Chromium:

```bash
pip install -e ".[dev,e2e]"
python -m playwright install chromium
pytest tests/e2e
```

Task creation and lifecycle updates shared by REST, browser UI, and MCP belong
in `agent_kanban_pm/services/tasks.py`. Routers should retain only transport
concerns such as authentication, response formatting, commits, and event
publication. This keeps stage/parent validation and transition rules consistent
across every interface.

Board behaviour and styling live in `data/static/js/board.js`,
`data/static/js/role-settings.js`, and `data/static/css/board.css`; the
`kanban_board.html` template holds markup only.

Package data is served from `agent_kanban_pm/data/`; the historical root-level
`agents/`, `mcp_configs/`, `static/`, and `templates/` folders are not part of
the packaged runtime.

## Roadmap

Version `0.5.0` includes the guided UI workflow, durable session handoffs,
review controls, and browser coverage. The next architectural work is to review
service boundaries for projects, sessions, approvals, and transitions, and to
decide when the tested versioned SQLite upgrades should move to Alembic.

Usage accounting, quota-aware model routing, and cross-CLI task continuation
are planned separately in [USAGE_ROUTING_PLAN.md](USAGE_ROUTING_PLAN.md).

## Security

See [SECURITY.md](SECURITY.md) for the supported threat model and private vulnerability reporting.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Release maintainers should also follow [RELEASING.md](RELEASING.md).

## License

MIT
