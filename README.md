# Agent Kanban PM

Local-first Kanban project management for humans and headless CLI agents.

The server stores state and exposes REST/WebSocket/MCP interfaces. The
selected orchestrator agent owns routing and task decisions. See
[AGENTS.md](AGENTS.md) for the full architecture.

## Requirements

- Python ≥ 3.12
- `tmux`, `git`
- At least one CLI agent (Claude Code, Gemini CLI, Codex, OpenCode, Aider, etc.)
- Optional: `gh` for GitHub PR/issue sync

## Install

```bash
pip install agent-kanban-pm
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

## License

MIT
