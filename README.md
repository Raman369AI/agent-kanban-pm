# Agent Kanban PM

Local-first Kanban project management for humans and headless CLI agents.

Status: alpha (`0.3.0a1`). The local runtime, board UI, per-task agent
sessions, and MCP surface work, but the CLI-agent workflow and packaging
surface should still be treated as early and subject to change.

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

## Install

Alpha builds may require pre-release resolution:

```bash
pip install --pre agent-kanban-pm
kanban init
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

## CLI

```bash
kanban roles list                                 # show role assignments
kanban roles assign worker opencode --mode headless # assign a role
kanban agents discover                            # find local CLIs
kanban sheet                                      # compact status
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
| `gemini` | `gemini` | **Retired upstream** — see below |
| `goose`, `crush`, `continue` | — | Stubs; invocation not yet verified |

### Gemini CLI is retired

Google shut Gemini CLI down for consumer accounts on 2026-06-18 and replaced
it with **Antigravity CLI** (`agy`). The `gemini` adapter is still shipped and
still works for anyone with a Gemini Code Assist Standard/Enterprise licence
or a paid API key, but it is hidden from `kanban init` and `kanban agents
discover`, and warns when assigned.

To move a role across:

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

## Development

```bash
pytest                       # run tests
python -m build              # build package
twine check dist/*           # verify artifacts
```

Package data is served from `agent_kanban_pm/data/`; the historical root-level
`agents/`, `mcp_configs/`, `static/`, and `templates/` folders are not part of
the packaged runtime.

## Roadmap

See [PLAN.md](PLAN.md) for the full roadmap to a standalone, fully available
release. Each phase is releasable on its own.

- [x] **Phase 0 — Stabilize** (done): failing UI test fixed, CI installs the
  package and smoke-tests the built wheel, single-sourced dependencies,
  `.env.example` documents real env vars, dev-artifact name heuristics
  replaced with a `Project.is_demo` flag.
- [x] **Phase 1 — Packaging correctness**: `src/` layout, declared `mcp`
  dependency, install-safe data home.
- [x] **Phase 2 — Security hardening**: `/ui` mutations require the token,
  HttpOnly cookie + CSRF header, Host-header validation, token file is
  `0600`, supervised-by-default autonomy with explicit `auto` opt-in,
  WebSocket token verification.
- [ ] **Phase 3 — Runtime correctness**: async subprocess work, service
  layer, Alembic, MCP identity freshness.
- [ ] **Phase 4 — Product surface & docs**: README landing page, community
  scaffolding, mkdocs site.
- [ ] **Phase 5 — Release & distribution**: PyPI trusted publishing, version
  tags, alternative install paths.

## License

MIT
