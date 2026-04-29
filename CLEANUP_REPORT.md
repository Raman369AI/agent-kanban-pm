# Cleanup Report

Generated: 2026-04-29

Scope: root workspace `/home/kronos/Desktop/agent-kanban-pm`.

This report identifies files that appear unrelated, stale, generated, or risky
to keep in source control. It does not delete anything.

## Executive Summary

The workspace has four different classes of cleanup candidates:

1. Files already removed by the current architecture and safe to keep deleted:
   `agent_reactor.py`, `routers/autopilot.py`.
2. Local runtime artifacts that should not be tracked or reviewed:
   logs, pid files, SQLite DB files, shell/editor caches, and one-off JSON
   request files.
3. Old demo/debug/helper scripts that are not part of the role-based headless
   PM model and should either move to `tools/legacy/` or be deleted after a
   final reference check.
4. Active-but-questionable legacy protocol files (`a2a.py`, `adapters.py`) that
   are still imported by `main.py`; do not delete them without an architecture
   decision and router cleanup.

The previous stuck-task worker report was generated from an isolated git
worktree that did not include many untracked live files. In the live workspace,
the newer runtime files such as `kanban_cli/`, `kanban_runtime/adapter_loader.py`,
`kanban_runtime/assignment_launcher.py`, and `agents/*.yaml` do exist and should
not be treated as missing.

## Safe Cleanup: Runtime Artifacts

These are local/generated files and should not be committed. Most logs and DBs
are already ignored, but some pid/json/config artifacts are not.

| Path | Reason | Recommendation |
|---|---|---|
| `kanban.db` | Local SQLite state | Keep ignored; do not commit |
| `*.log` files | Runtime/debug output | Keep ignored; remove from workspace when not needed |
| `server.pid` | Runtime PID artifact, currently tracked/modified | Remove from git tracking and add `*.pid` to `.gitignore` |
| `claude_daemon.pid` | Runtime PID artifact | Add `*.pid` to `.gitignore` |
| `.Rhistory` | Local shell/R history | Add `.Rhistory` to `.gitignore` or delete locally |
| `.mcp.json` | Local MCP config | Add to `.gitignore` unless intentionally shared |
| `.aider.chat.history.md` | Local assistant history | Already ignored by `.aider*`; safe local cleanup |
| `.aider.input.history` | Local assistant history | Already ignored by `.aider*`; safe local cleanup |
| `.aider.tags.cache.v4/` | Local assistant cache | Already ignored by `.aider*`; safe local cleanup |
| `.claude/settings.local.json` | Local Claude settings | Add `.claude/` to `.gitignore` if it must stay local |
| `.pytest_cache/` | Test cache | Already ignored |
| `__pycache__/`, package `__pycache__/` | Python cache | Already ignored |
| `.venv/`, `venv/` | Local virtualenvs | Already ignored |

One-off JSON files:

| Path | Reason | Recommendation |
|---|---|---|
| `new_task.json` | Local API request scratch file | Delete or move to `examples/requests/` if useful |
| `task_22_verify.json` | Local verification artifact | Delete |
| `tasks_project_2.json` | Local export/scratch artifact | Delete or move to `examples/fixtures/` |
| `agent_registration.json` | Old registration payload; tracked and modified | Likely remove from tracked source unless used by docs/tests |
| `gemini_agent_registration.json` | Old registration payload | Delete or move to fixtures |

## Already Deleted Architecture Files

`AGENTS.md` states server-side orchestration has been removed. Git status agrees
these files are deleted in the current working tree:

| Path | Status | Recommendation |
|---|---|---|
| `agent_reactor.py` | Deleted | Keep deleted |
| `routers/autopilot.py` | Deleted | Keep deleted |

There are still UI/doc references to "autopilot" strings in
`templates/kanban_board.html`, `README.md`, and `CHANGELOG.md`. Those should be
renamed or removed separately so terminology matches the manager-owned model.

## Likely Obsolete Demo, Debug, Or Repair Scripts

These are not part of the `python -m kanban_cli run` path and are not referenced
by the role-based runtime. They look like old experiments, manual repair tools,
or smoke/debug helpers.

High-confidence delete or archive candidates:

| Path | Evidence | Recommendation |
|---|---|---|
| `debug_ui.py` | Debug-only script | Delete or move to `tools/debug/` |
| `debug_ui_testclient.py` | Debug-only script | Delete or move to `tools/debug/` |
| `debug_ui_v2.py` | Debug-only script | Delete or move to `tools/debug/` |
| `diagnose_ui.py` | Manual diagnostic helper | Move to `tools/debug/` if still useful |
| `fix_db.py` | One-off database repair | Delete unless there is a documented migration use |
| `fix_status.py` | One-off status repair | Delete unless there is a documented migration use |
| `check_tasks.py` | Manual task inspection script with hardcoded names | Delete or rewrite as CLI command |
| `reproduce_assign.py` | Reproduction script | Delete or move to `tools/repro/` |
| `init_demo.py` | Demo initializer, not target init path | Delete or move to `examples/` |
| `demo_mcp_agent.py` | Demo script referenced by old docs | Move to `examples/` if docs still need it |
| `example_agent_client.py` | Old client example referenced by old docs | Move to `examples/` or update to current MCP/role model |
| `setup.sh` | Old setup helper not in current run path | Delete if `RUN.md` is authoritative |

Medium-confidence candidates, because tests/docs still mention them:

| Path | Evidence | Recommendation |
|---|---|---|
| `agent_daemon.py` | Old daemon model; `test_daemon_real.py` still invokes it | Decide whether this test path is legacy; archive together if obsolete |
| `agent_inbox.py` | README/CHANGELOG mention terminal notifier | Keep only if notifier is still a supported feature |
| `watch_all.py` | Spawns old `agent_daemon.py` per adapter | Archive/delete if role supervisor replaces it |
| `gemini_event_listener.py` | Old Gemini event listener | Delete if role supervisor/session streamer supersedes it |
| `gemini_worker_agent.py` | Old Gemini worker implementation | Delete if role supervisor/session streamer supersedes it |
| `register_real_agent.py` | Old manual registration helper | Delete or replace with `kanban_cli agents/roles` docs |
| `quick_task.py` | Convenience CLI for task creation | Either keep as supported CLI or fold into `kanban_cli` |
| `user_profile.py` | Convenience/local profile helper | Either keep as supported CLI or fold into `kanban_cli` |

## Active But Needs Architecture Review

Do not delete these without code changes. They are still imported or mounted by
the running app:

| Path | Evidence | Recommendation |
|---|---|---|
| `a2a.py` | `main.py` imports and mounts `a2a.router` | Keep for now, or remove only with API/docs/router cleanup |
| `adapters.py` | `main.py` imports `register_adapters()` | Keep for now, or replace event delivery explicitly |
| `websocket_manager.py` | Used by `adapters.py` and websocket routes | Keep while adapters/websockets remain |
| `routers/agent_connections.py` | Mounted by `main.py`; protocol registry | Keep unless legacy protocols are removed |

Notes:

- `a2a.py` is described by `FEASIBILITY_REPORT.md` as a custom message router,
  not a full A2A implementation. If the target architecture no longer includes
  A2A, remove the router from `main.py`, delete/update README API docs, and add
  tests covering route removal.
- `adapters.py` is still the bridge from `event_bus` to websocket/webhook
  connections. It is old in concept, but active in code.

## Documentation Cleanup Candidates

`AGENTS.md` is the source of truth. Several older docs duplicate or describe
earlier architecture. They should either be removed, moved to `docs/archive/`,
or rewritten to point to `AGENTS.md`.

Archive candidates:

| Path | Reason |
|---|---|
| `BUILD_SUMMARY.md` | Old implementation summary |
| `DEMO_RESULTS.md` | Demo artifact |
| `INTEGRATION_SUMMARY.md` | Old integration summary |
| `PROJECT_STRUCTURE.md` | File structure is stale |
| `QUICKSTART.md` | May conflict with `RUN.md` and current role runtime |
| `UI_DEMO_COMPLETE.md` | Demo artifact |
| `STATUS_REPORT.md` | Old status snapshot |
| `implementation.md` | Old implementation plan, references old A2A/adapters model |
| `walkthrough.md` | Old event adapter/autopilot walkthrough |
| `FEASIBILITY_REPORT.md` | Useful history, but archive if no longer active guidance |

Keep/update:

| Path | Reason |
|---|---|
| `AGENTS.md` | Source of truth |
| `CLAUDE.md` | Correctly points to `AGENTS.md` instead of forking rules |
| `RUN.md` | Operational runbook |
| `README.md` | Public overview, but still contains A2A and old file-structure references |
| `MCP_SETUP.md` | Keep if updated for env-based identity |
| `CHANGELOG.md` | Keep as historical release record |
| `GITHUB_DEPLOYMENT.md` | Keep only if deployment path remains supported |

## Do Not Delete: Current Runtime Implementation

These are untracked in git status but are core to the current architecture in
`AGENTS.md` and are used by the running app:

| Path | Reason |
|---|---|
| `agents/*.yaml` | Bundled adapter registry specs |
| `kanban_cli/` | `init`, `run`, `roles`, daemon commands |
| `kanban_runtime/adapter_loader.py` | Adapter registry loader |
| `kanban_runtime/preferences.py` | Preferences and role assignment loader |
| `kanban_runtime/manager_daemon.py` | Manager daemon |
| `kanban_runtime/assignment_launcher.py` | Assignment-to-execution bridge |
| `kanban_runtime/role_supervisor.py` | Headless role supervisor |
| `kanban_runtime/session_streamer.py` | Per-task tmux terminal streamer and completion detector |
| `sync_agents.py` | Adapter registry sync entrypoint imported by `main.py` |
| `open_project.py` | Folder-as-project helper |
| `mcp_configs/` | CLI MCP config examples |
| `templates/project_git.html` | Project Git/PR UI |
| `templates/project_workbench.html` | Project workbench UI |
| `test_phase6.py`, `test_roles_and_reviews.py`, `test_ui_task_creation.py`, etc. | Current regression tests |

These should be added to version control if this branch is intended to preserve
the current working system.

## Recommended Cleanup Order

1. Add ignore rules:
   - `*.pid`
   - `.Rhistory`
   - `.mcp.json`
   - `.claude/`
   - Optional: `*_registration.json`, `task_*_verify.json`, `tasks_project_*.json`
2. Remove tracked runtime artifacts from git:
   - `server.pid`
   - `agent_registration.json`, if not intentionally kept as a fixture
3. Keep the architecture deletions:
   - `agent_reactor.py`
   - `routers/autopilot.py`
4. Archive or delete high-confidence debug/demo scripts.
5. Decide whether `a2a.py` and `adapters.py` are supported features. If not,
   remove them through a focused architecture change and update tests/docs.
6. Move stale docs to `docs/archive/` or delete them after confirming
   `README.md`, `RUN.md`, `MCP_SETUP.md`, and `AGENTS.md` cover the current
   workflow.
7. Add current core runtime files to git before any broad deletion pass, so the
   app can be restored from source control.

## Suggested First Delete/Archive Set

If you want a conservative first pass, start with only these:

```text
.Rhistory
new_task.json
task_22_verify.json
tasks_project_2.json
gemini_agent_registration.json
debug_ui.py
debug_ui_testclient.py
debug_ui_v2.py
diagnose_ui.py
fix_db.py
fix_status.py
check_tasks.py
reproduce_assign.py
```

Then review these before deleting:

```text
agent_daemon.py
agent_inbox.py
watch_all.py
gemini_event_listener.py
gemini_worker_agent.py
register_real_agent.py
quick_task.py
user_profile.py
demo_mcp_agent.py
example_agent_client.py
init_demo.py
setup.sh
```
