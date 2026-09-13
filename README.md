# Agent Kanban PM

Local-first Kanban project management for humans and headless CLI agents.

Status: release candidate (`0.4.0rc9`) for local, single-user development.
The local runtime, board UI, per-task agent sessions, and MCP surface work and
are covered by tests, including a database upgrade path. It is a single-operator
tool by design: one shared token guards the local server, so do not expose it to
an untrusted network or share an instance with people you would not give shell
access.

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

Release candidates need pre-release resolution:

```bash
pip install --pre agent-kanban-pm
kanban init
```

For an isolated CLI installation:

```bash
pipx install --pip-args="--pre" agent-kanban-pm
# Or run without installing:
uvx --prerelease allow --from agent-kanban-pm kanban --help
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
activity, terminal output, logs, and reviews. An edit draft stays intact
during a live refresh; if someone else changes the task, saving reports the
conflict.

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

**Board view** and **List view** are remembered across reloads. Theme and
density live under **Appearance**. A rejected move returns the card to its
original stage and shows the server's reason.

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
The server does not choose which agent should do new work, but it does keep the
standard role handoff moving once an assigned session finishes:

1. A worker assignment starts from To Do or In Progress.
2. When the agent marks `STATUS.md` with `handoff_ready: true` and `state: done`,
   `completed`, or `review`, the session streamer marks the session done and
   moves the card to Review.
3. Review-stage policy roles, normally `test` and `diff_review`, are assigned
   and launched from Review when configured in `~/.kanban/preferences.yaml`.
4. When review/test sessions complete, the card moves to Done.
5. Done-stage policy roles, normally `git_pr`, may launch from Done to prepare
   PR or git contribution work.

Handoff follows each stage's `workflow_key` (`backlog`, `to_do`,
`in_progress`, `review`, `done`), not its label, so stages can be renamed
freely: a Done column relabelled "Shipped" still completes the tasks moved into
it. The key is derived from the name when a stage is created and is kept when it
is renamed; pass `workflow_key` when creating a custom stage to give it workflow
meaning. Stages with no recognised key leave a moved task's status unchanged.

The handoff source of truth is each worktree's `STATUS.md`. If an agent exits
without updating it, the card may stay where it is because the runtime cannot
reliably tell whether the work is ready for review.

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
Kanban approval queue for a human or the orchestrator.

Auto mode is an explicit per-role opt-in. Set `autonomy: auto` on a role in
`~/.kanban/preferences.yaml` (or answer `y` at the autonomy prompt in
`kanban init`, or pass `--autonomy auto` to `kanban roles assign`). The
launcher then appends the adapter's bypass flags — `claude
--permission-mode bypassPermissions`, `agy --dangerously-skip-permissions`,
`codex --full-auto`, `opencode --auto`, `aider --yes-always`, declared as
`task_command.auto_args` in the adapter YAML — so the agent never pauses to
ask. Combined with the per-task worktree, the blast radius is scoped to that
worktree, and risky actions are expected to be recorded in `STATUS.md`.
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

This release candidate completes the guided UI workflow and browser
coverage for setup, execution, approvals, review, search, and planning
preview. Before a stable 0.4.0 release, the remaining priorities are runtime
service extraction and migrations, fuller documentation, and release
validation across supported platforms.

## Security

See [SECURITY.md](SECURITY.md) for the supported threat model and private vulnerability reporting.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Release maintainers should also follow [RELEASING.md](RELEASING.md).

## License

MIT
