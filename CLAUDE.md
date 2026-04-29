# CLAUDE.md

Read `AGENTS.md` first. It is the single source of truth for this repo's
architecture, agent roles, workflow rules, testing expectations, and
security boundaries.

Do not duplicate architecture rules here. When behavior changes, update
`AGENTS.md` and keep this file as a pointer so Claude-specific context
cannot drift from the shared agent spec.

Current non-negotiables from `AGENTS.md`:

- The server is a state store and event bus; the orchestrator/manager
  agent owns routing decisions.
- Local-first identity uses `KANBAN_AGENT_NAME`, not Kanban-issued API
  keys.
- Role assignments are data-driven and user-selected.
- Heavy code goes to the configured Worker Agent role.
- Critical code requires visible diff review before handoff.
- Git branch/commit/PR work belongs to the assigned Git PR Agent.
- Never include Claude attribution in commits or PRs.
