# Fixes — Architecture Review Findings

Comprehensive list of issues identified during the architecture review,
organized by priority. Each issue includes affected files, line numbers,
impact assessment, recommended remediation, and **current status**.

**Design note:** This system is local-first. Humans don't need authentication —
the human operator is the authority. Agents are identified by `KANBAN_AGENT_NAME`
env vars set by the user. The `/ui/` OWNER fallback is intentional by design:
the local user owns the board and should be able to do everything without auth
friction. Auth issues are reclassified accordingly.

---

## P0 — Critical

### P0-1: `/ui/` Mutation Auth — By Design

**Severity:** N/A (by design)
**Status:** Intentional. Local-first system; the human operator is the authority.
**Files:** `auth.py:92-124`

The `/ui/` fallback resolves to the human OWNER for all HTTP methods. This is
the intended design: the local user running the board should be able to perform
all operations without providing headers on every request. Agents authenticate
via `KANBAN_AGENT_NAME`; the human authenticates by being the local user.

No remediation needed. The current behavior — OWNER fallback for `/ui/` routes,
entity resolution for REST/MCP routes — is correct for the local-first model.

---

### P0-2: Transition Validation Defaults Bypass Output/Review Gates

**Severity:** Critical
**Status:** Fixed. `gather_transition_context()` added to `stage_policy.py` — queries real `has_diff_review` and `is_critical` from task activity data. All three transition call sites updated. `ui_edit_task` now passes `to_policy` from the current stage.
**Files:** `kanban_runtime/stage_policy.py`, `mcp_server.py`, `routers/tasks.py`, `routers/ui.py`

**What changed:**

Added `gather_transition_context(db, task_id, project_id)` to `stage_policy.py`:
- `has_diff_review`: queries `DiffReview` for an `APPROVED` review on the task
- `is_critical`: queries recent `AgentActivity.file_path` entries against critical patterns (auth.py, mcp_server.py, session_streamer, role_supervisor, database.py, .env, etc.)

All call sites updated:

| Call site | Before | After |
|-----------|--------|-------|
| `mcp_server._handle_move_task` | `has_diff_review` from DB, `has_required_outputs=True`, `is_critical=False` | Uses `gather_transition_context()` for `has_diff_review` and `is_critical` |
| `routers/tasks.PATCH /tasks/{id}` | `has_diff_review` from DB, `has_required_outputs=True`, `is_critical=False` | Uses `gather_transition_context()` for `has_diff_review` and `is_critical` |
| `routers/ui.ui_move_task` | `has_diff_review` from DB | Uses `gather_transition_context()` for all values |
| `routers/ui.ui_edit_task` | `to_policy=None`, `has_diff_review` from DB | Passes `to_policy` from current stage, uses `gather_transition_context()` |

**Remaining note:** `has_required_outputs` is still `True` because determining whether a task's outputs match a stage's `required_outputs_json` list requires activity-type-specific heuristics. The `check_required_outputs()` helper is available for future use.

---

### P0-3: Approval Race Condition — Streamer Bypasses CAS

**Severity:** High
**Status:** Fixed. All three resolution paths now use atomic compare-and-swap on `update_version`.
**Files:** `routers/agent_activity.py:1379-1396`, `mcp_server.py:1894-1909`, `kanban_runtime/session_streamer.py:277-299`, `models.py:450`

**What changed:**
- `AgentApproval` has `update_version` column.
- REST and MCP use `sa_update(...).where(update_version == expected_version)` with 409 on conflict.
- Session streamer now uses atomic CAS for approval cancellation instead of bare ORM assignment. If `rowcount == 0`, the approval was already resolved and is skipped with a debug log.
- Malformed events in `get_pending_events` are preserved, not deleted.

---

## P1 — High

### P1-1: Orphaned Agent Sessions

**Status:** Fixed. `_orphaned_session_sweeper` in `main.py:205-258`.

---

### P1-2: Hardcoded Agent Profiles

**Status:** Fixed. `DEFAULT_AGENT_PROFILES` and `canonical_agent_name()` removed.
`profile_for_agent()` uses exact `spec.name` matching from adapter YAML.
All 8 bundled YAMLs have `owns` and `review_only` fields.
`RoleAssignment` now has `owns` and `review_only` fields so
preference-based roles can customize path ownership.
`profile_for_agent()` resolves preferences first, then adapter YAML,
then falls back.

---

### P1-3: Event Data Destruction

**Status:** Fixed. `PendingEvent` has `consumed_at` column. `get_pending_events`
soft-deletes via `consumed_at` instead of hard-deleting. Background sweeper
cleans up events older than 6 hours.

---

### P1-4: Redundant Approval Polling

**Status:** Fixed. `kanban_board.html` and `project_workbench.html` use
WebSocket events only for approvals. Initial `loadApprovals()` on
DOMContentLoaded remains; `loadLive` still polls at 5s (correct for
status).

---

### P1-5: Terminal Tab Missing

**Status:** Fixed. In-card expansion has Terminal tab with active-session
resolution and pop-out button.

---

### P1-6: Monolithic `mcp_server.py`

**Status:** Not fixed. 2067 lines, no modular split.
**Remediation:** Split into domain modules with a `MCPModule` protocol.

---

### P1-7: Auto-Move Cards

**Status:** Fixed. `_mark_task_started` is report-only — writes `TaskLog`
with `log_type="handoff"`, no status/stage mutation.

---

## P2 — Medium

### P2-1: Module-Level Singletons

**Status:** Mostly fixed. `EventBus.reset()` and `reset_streamer()` exist.
`AssignmentLauncher.reset()` is a documented no-op.

---

### P2-2: Schema Migrations

**Status:** Fixed. Redundant `CREATE TABLE` and `CREATE INDEX` DDL for `agent_checkpoints` and `stage_policies` removed from migration v4. Tables are now created solely by `create_all()` via their SQLAlchemy models, eliminating the schema drift risk (notably `review_mode` was unconstrained `VARCHAR(50)` in the DDL vs `SQLEnum(ReviewMode)` in the model). `schema_migrations` table DDL retained (no model). All ALTER TABLE migrations retained with `_column_exists` guards. Migration rules documented in `_migrate_db_schema()` docstring.

---

### P2-3: WebSocket Reconnect Backoff

**Status:** Fixed. Both templates use exponential backoff (1s start, 2x, 30s max).

---

### P2-4: Silently Swallowed Exceptions

**Status:** Fixed. All previously silent handlers now log at WARNING+.
`policy_outputs_if_available` upgraded from DEBUG to WARNING.

---

## P3 — Low

### P3-1: Approval Pagination

**Status:** Fixed. `offset: int = 0` parameter added.

---

### P3-2: Stage Policy Event

**Status:** Fixed. `_notify_stage_policy_for_todo` publishes
`STAGE_POLICY_CREATED` via `event_bus.publish()`.

---

### P3-3: Test Helper SQL

**Status:** Fixed. Replaced all f-string SQL with SQLAlchemy ORM `delete()`/`update()`/`select()` using `.in_()` filters. Fixed invalid `TaskAssignment` model reference to `task_assignments` association Table.

---

## Removed from Issue List (by design)

| Original ID | Issue | Reason |
|-------------|-------|--------|
| P0-1 (original) | `/ui/` auth bypass on mutations | Local-first design: human operator owns the board, no auth friction needed |
| P0-4 (original) | Unauthenticated endpoints | Local-first: all endpoints now have auth but the human OWNER fallback is intentional |

## Status Summary

| ID | Issue | Status |
|----|-------|-------|
| P0-1 | Auth bypass (by design) | **Closed** — local-first, human is the authority |
| P0-2 | Transition validation defaults | **Fixed** — `gather_transition_context()` provides real `has_diff_review` and `is_critical`; `ui_edit_task` passes `to_policy` |
| P0-3 | Streamer approval race | **Fixed** — CAS pattern on all three paths including session streamer |
| P1-1 | Orphaned sessions | **Fixed** |
| P1-2 | Hardcoded profiles | **Fixed** — `RoleAssignment` now has `owns`/`review_only`; `profile_for_agent` resolves preferences first |
| P1-4 | Redundant polling | **Fixed** — workbench uses WS events only for approvals |
| P2-2 | Schema migrations | **Fixed** — redundant CREATE TABLE/INDEX DDL removed from v4; models are single source of truth |
| P2-3 | WS backoff | **Fixed** |
| P2-4 | Swallowed exceptions | **Fixed** |
| P3-1 | Pagination | **Fixed** |
| P3-2 | Stage policy event | **Fixed** |
| P3-3 | Test SQL | **Fixed** — parameterized SQLAlchemy ORM statements; fixed TaskAssignment import |