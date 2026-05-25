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
- `git`
- Recommended: `tmux` for detachable terminal sessions. If `tmux` is not
  available, the runtime uses its native PTY subprocess fallback.
- At least one CLI agent (Claude Code, Gemini CLI, Codex, OpenCode, Aider, etc.)
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
kanban roles assign worker gemini --mode headless  # assign a role
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

## Auto-approval

Bundled adapters launch their CLI in auto-approval mode by default
(`claude --permission-mode bypassPermissions`, `gemini --approval-mode yolo`,
`codex --full-auto`, `aider --yes-always`). Combined with the per-task
worktree, the blast radius is scoped to that worktree, and risky actions are
expected to be recorded in `STATUS.md` rather than queued for human approval.
Critical review and approval records can still be created through the REST/MCP
surfaces when an agent or human needs an explicit audit gate.
To roll back to supervised execution, edit the corresponding YAML in
`kanban_runtime/data/agents/` (or your `~/.kanban/agents/` override).

## MCP Identity

CLI agents connect through `mcp_server.py` using local process identity:

```bash
KANBAN_AGENT_NAME=codex KANBAN_AGENT_ROLE=worker python mcp_server.py
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

Package data is served from `kanban_runtime/data/`; the historical root-level
`agents/`, `mcp_configs/`, `static/`, and `templates/` folders are not part of
the packaged runtime.

## License

MIT
