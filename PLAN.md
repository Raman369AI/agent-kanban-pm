# Low-effort agent-workbench improvements

- Date: 2026-09-20
- Status: Implemented through Phase 2 — Phase 3 deferred
- Basis: comparison of Cline Kanban with `agent-kanban-pm`

## Goal

Adopt the highest-value workbench conveniences from Cline Kanban without
weakening this project's durable, server-owned workflow, evidence gates, or
supervised execution defaults.

The first two product changes should feel immediate to a user: show what an
agent is doing directly on its task card, then let an eligible assigned task
start with one click. A Git/PR handoff shortcut can follow once those smaller
changes are stable.

## Guardrails

- Keep SQLite and server-side services authoritative. The browser remains a
  projection and controller, not the owner of lifecycle state.
- Reuse the existing task-move, activity, outbox, stage-entry, assignment, and
  launch-request mechanisms instead of adding parallel mutation paths.
- Preserve audit events, authorization checks, transition rules, and the
  completion evidence gate.
- Do not infer successful work from a clean working tree, an agent message, or
  a button click. Review and Done retain their current evidence requirements.
- Avoid per-card network requests and N+1 database queries on the board.
- Render activity as plain text. Strip terminal control sequences, bound the
  payload, and never inject agent output as HTML.
- Preserve keyboard access, responsive layout, light/dark themes, and current
  drag-and-drop behavior.
- Do not add a database migration for Phases 0-2 unless implementation proves
  it unavoidable.
- Leave the historical [`Plan.md`](Plan.md) unchanged.

## Delivery overview

| Order | Change | Estimate | Status |
| --- | --- | ---: | --- |
| 0 | Promote a zero-install `uvx` path | Less than 1 hour | Implemented |
| 1 | Show latest agent activity on task cards | 2-4 hours | Implemented |
| 2 | Add a one-click Start action | 2-4 hours | Implemented |
| 3 | Add a Request Git/PR handoff action | 0.5-1 day | Deferred |

Phases 0 and 1 can ship together. Phase 2 should be a separate, small change so
its lifecycle behavior is easy to review. Phase 3 is optional for the first
release and should follow only after the Start action has proven reliable.

## Phase 0: promote the zero-install workflow

### Scope

- Add the published-package `uvx` path to the README install/quick-start area:

  ```bash
  uvx --from agent-kanban-pm kanban init
  uvx --from agent-kanban-pm kanban run
  ```

- Add the same path to the marketing site while retaining the existing `pipx`
  installation option.
- Explain the distinction briefly: `uvx` runs an ephemeral environment;
  `pipx` installs the CLI for repeated local use.
- Use commands that match the release smoke test and current CLI names.

### Acceptance criteria

- A new user can start the released package without cloning the repository.
- README and site commands are identical and copyable.
- The documentation does not imply that `uvx` creates a persistent install.

## Phase 1: latest agent activity on every relevant card

### User experience

Show a compact, one- or two-line status below the task title or existing task
metadata, for example:

> Tool call · Running targeted board tests…

The preview is a summary of the newest durable activity for the task. It is not
a second terminal and must not expand the card as output grows. Existing
session chips, activity dots, and expanded Activity/Terminal tabs remain.

### Server work

- In the board route, load the newest meaningful `AgentActivity` for all tasks
  in one bounded query. Prefer a grouped `MAX(id)` subquery joined back to the
  activity row, scoped to the current project and visible task IDs.
- Prefer useful types such as `ERROR`, `HANDOFF`, `RESULT`, `TOOL_CALL`,
  `ACTION`, and `COMMAND`. Use raw `OBSERVATION` output only as a fallback so a
  terminal pane does not overwhelm higher-signal activity.
- Pass a `latest_activity_by_task` mapping to the board template with only the
  fields the card needs: activity ID, task ID, agent identity, type, normalized
  message, and timestamp.
- Normalize the message consistently: strip ANSI/control sequences, collapse
  whitespace, and truncate to roughly 160-200 characters.
- Reuse or extract the existing ANSI-removal behavior from the PTY/runtime code
  instead of maintaining incompatible regular expressions.

### Template and styling work

- Add a `.task-live-summary` element to task cards without changing their
  width or disrupting expanded-card controls.
- Use existing theme variables and a two-line ellipsis on narrow boards.
- Include an accessible text label for the activity type and agent. Avoid a
  noisy board-wide live region that announces every streaming update.
- Render the message as escaped text on initial load.

### Browser work

- Extend the existing `agent_activity_logged` WebSocket handler to update the
  correct card from the event's existing `message`, `type`, `agent_id`, and
  `task_id`; do not fetch the activity endpoint for every event.
- Assign updates with `textContent`, never `innerHTML`.
- Track the latest activity ID or timestamp on the element so delayed events do
  not overwrite newer activity.
- Keep current activity-dot and session-status behavior intact.
- Let a normal board reload restore the same preview from durable activity.

### Tests

- Route test: the board supplies the newest meaningful activity for each task.
- Isolation test: activity from another project or an inaccessible task never
  appears.
- Safety test: ANSI sequences are removed, markup is escaped, whitespace is
  normalized, and long messages are bounded.
- WebSocket/browser test: a new activity event updates only its task card
  without a reload.
- Durability test: reloading shows the same latest preview.
- Regression test: card expansion, drag/drop, keyboard movement, and session
  indicators continue to work.
- Visual check: desktop/mobile and light/dark modes remain readable.

### Acceptance criteria

- New activity becomes visible on the correct card within about one second.
- The preview survives a reload because its source is durable activity.
- Board rendering adds at most one bounded activity query, not one query or
  request per card.
- Agent output cannot inject markup or produce unbounded card content.
- Existing task movement and workbench controls behave unchanged.

## Phase 2: one-click Start for eligible tasks

### User experience

Show a **Start** action on Backlog cards when the board has a configured To Do
stage and the task has exactly one active, launchable agent assignee. Starting
the task moves it through the existing transition path; entry into To Do then
drives the existing durable assignment and launch flow.

Use conservative behavior for ambiguous cases:

- No active agent assignee: open the existing Assign UI and explain that an
  agent must be selected.
- Human-only assignment: open Assign; never treat a human as launchable.
- Multiple active agent assignees: open the assignment chooser; never start all
  of them silently.
- No configured To Do stage: hide or disable Start with an explanation; do not
  guess an arbitrary destination.
- Existing active or queued execution: show its state instead of another Start
  action.

### Implementation

- Reuse the board's existing `todo_stage_id` and `moveTaskCard`/
  `PATCH /ui/tasks/{id}/move` flow. Do not introduce a second task-transition
  endpoint.
- Let the server continue to enforce transition rules, update task status,
  write audit history, and call the To Do stage-entry policy.
- Let the existing `TASK_ASSIGNED` outbox event and scheduler create the
  durable launch request. The browser must not launch a process directly.
- Load any launch/queued state needed by the board in a single bounded query or
  existing board payload; do not poll once per card.
- Disable the button while the move is pending, stop click propagation, and
  prevent double submission.
- Reuse the current optimistic-move rollback and toast/error behavior if the
  transition fails.
- Give the button an explicit accessible label containing the task title.

### Tests

- Exactly one eligible agent assignee: one click moves the task to To Do and
  produces exactly one durable launch request.
- No agent or a human-only assignment: Assign opens and no move occurs.
- Multiple agent assignees: explicit selection is required.
- Double click, retry, or WebSocket echo does not create duplicate launches.
- A rejected transition restores the original card and reports the error.
- Existing active/queued execution suppresses a second Start action.
- Keyboard activation works and the control does not trigger card expansion or
  dragging.

### Acceptance criteria

- An unambiguous, assigned Backlog task starts through one user action.
- The action uses existing lifecycle, audit, and outbox semantics.
- Repeated input cannot create duplicate execution.
- Ambiguous assignment always requires an explicit user choice.

## Phase 3: Request Git/PR handoff in Review

> Deferred for the current release. The standard Review stage policy already
> queues the `git_pr` role through the durable assignment scheduler, and the
> Activity launch queue already supports blocked-state inspection and retry.
> Add a separate button only if projects with nonstandard policies need an
> explicit manual trigger; do not duplicate the default automatic handoff.

### User experience

Add **Request Git/PR** to eligible Review cards. The wording is deliberate: the
button requests work from the configured Git/PR agent; it does not claim that a
branch, pull request, merge, or completion already exists.

Show the resulting state on the card: queued, active, blocked, or completed,
with a link to the existing workbench/session details when available.

### Implementation

- Add a small authenticated service/endpoint that requests the configured
  `git_pr` role for the current task and revision.
- Reuse the existing durable `TASK_ASSIGNED` event, stage-entry conventions,
  launch-request scheduler, and session model.
- Make the request idempotent. If an open launch request or active matching
  session exists, return that state instead of creating another.
- Preserve the implementation session as the source context when available so
  the Git/PR agent can inspect the relevant work.
- Do not move the task to Done. The existing completion gate remains
  responsible for verifying required PR/merge evidence.

### Tests and acceptance criteria

- Authorization and project isolation are enforced.
- Missing or inactive `git_pr` policy produces a clear, non-mutating error.
- Repeated requests deduplicate against queued and active work.
- The request is tied to the current task revision.
- Successful agent output alone does not bypass merged-PR or other configured
  completion evidence.

## Explicitly deferred

These ideas are useful but are not low-effort adaptations and should not be
folded into the work above:

- A real dependency DAG with cycle detection, automatic unblocking, dependency
  visualization, and scheduler semantics.
- A `blocked by` card badge based only on `sequence_order`. The current field is
  not a trustworthy general dependency model, so presenting it as one would be
  misleading.
- Per-turn Git checkpoints, inline checkpoint comments, or automatic commits.
- A native SDK/runtime integration that replaces the current supervised
  process/session boundary.
- Automatic branch, commit, PR, or merge creation based solely on agent output.
- Browser-owned automation or lifecycle state.
- Remote multi-user collaboration as part of these UI changes.

## Cross-cutting validation

Run the normal quality gates after each phase, plus targeted board tests:

```bash
.venv/bin/flake8 src tests
.venv/bin/pytest -q --timeout=60 --timeout-method=thread
.venv/bin/pytest tests/e2e/test_board_workflows.py -q
```

If the end-to-end environment uses a narrower command or marker, use the
project's documented equivalent. Before release, also verify the published
package's `uvx` commands in a clean temporary environment.

## Definition of done

A phase is complete only when:

- its acceptance criteria and targeted regression tests pass;
- no N+1 query or per-card polling was introduced;
- activity and error text are safely bounded and escaped;
- keyboard, mobile, and both theme variants have been checked;
- user-facing behavior is documented and the changelog is updated;
- lifecycle events remain durable, auditable, and server-owned; and
- the release notes identify any intentionally deferred follow-up.
