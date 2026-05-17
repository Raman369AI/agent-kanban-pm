# Agent Kanban PM

Local-first Kanban project management for humans and headless CLI agents.

Status: alpha (`0.3.0a1`). The core local runtime works, but the CLI-agent
workflow, approval capture, and packaging surface should still be treated as
early and subject to change.

The server stores state and exposes REST/WebSocket/MCP interfaces. The
selected orchestrator agent owns routing and task decisions. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the system diagram.

## Requirements

- Python ≥ 3.12
- `tmux`, `git`
- At least one CLI agent (Claude Code, Gemini CLI, Codex, OpenCode, Aider, etc.)
- Optional: `gh` for GitHub PR/issue sync

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

Each Kanban task that's assigned to an agent runs in its own git worktree
under `~/.kanban/worktrees/project-{id}/task-{id}-{agent}` on a branch named
`kanban/task-{id}-{agent}`. The branch is started from the project's detected
base ref (`origin/HEAD`, then `origin/main`/`origin/master`, then a local
`main`/`master`).

Before each session starts the launcher:

1. Fetches the base ref (when an `origin` remote exists).
2. Rebases the task branch onto the base so parallel tasks don't drift from
   mainline.
3. Records the result (`rebased onto …`, `skipped (uncommitted changes)`,
   `aborted (conflicts)`, …) as an `AgentActivity` you can audit from the
   board.

If the project directory isn't a git worktree, the agent runs in the project
folder directly with no isolation.

## Auto-approval

Bundled adapters launch their CLI in auto-approval mode by default
(`claude --permission-mode bypassPermissions`, `gemini --approval-mode yolo`,
`codex --full-auto`, `aider --yes-always`). Combined with the per-task
worktree, the blast radius is scoped to that worktree, and risky actions are
expected to be recorded in `STATUS.md` rather than queued for human approval.
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

## Adapter Registry

Adapters are YAML files in `~/.kanban/agents/`. Adding a tool requires no
Python changes.

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
