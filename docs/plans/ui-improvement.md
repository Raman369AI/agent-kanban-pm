# UI improvement plan

Status: Phases 1–5 implemented and validated (2026-09-12). Phase 1 was
committed as `ff8c81b` and Phase 2 as `22601a5`. Phases 3–5 are implemented
together with focused browser and route coverage.

Phase 1 verification: 227 tests passed, including 17 Chromium browser tests.
Navigation and creation controls were checked at 1440px, 1024px, 768px, and
390px; 200% phone zoom was emulated with a 195px CSS viewport and device pixel
ratio 2. Light, dark, blue, and rose themes, keyboard and touch actions, slow
and failed requests, and WebSocket reconnect were exercised. JavaScript syntax
checks and `git diff --check` pass.

## Goal

Make the next action clear at every step: create a project, configure an agent,
define work, start execution, respond to blockers, and review the result.
A first-time user should be able to complete this flow without reading the CLI
or API documentation.

This plan began with a source review of the templates, styles, JavaScript, and
relevant backend handlers. Automated browser checks cover the Phase 1 interactions
and navigation widths; the full usability walkthrough remains to be done.

## Scope and approach

Improve the existing Jinja, CSS, and JavaScript interface incrementally. Keep the
current task and execution APIs where they support the intended behavior. Verify
assignment and stage-transition semantics before choosing action labels; UI copy
must accurately explain whether an action starts execution or queues work.

Each phase should be independently reviewable and releasable. Use an isolated
sample project for browser checks so validation does not launch agents or change
real project work.

## Phase 1 — Fix confusing and broken interactions

Priority: Immediate.

Validation update (2026-09-12): Phase 1 acceptance criteria are met in the
isolated browser walkthrough. The mobile drawer shows its labels with either
saved sidebar setting, cannot take focus while closed, and clears its backdrop
when resized to desktop. Task creation remains usable at 1024px and at the
200% phone zoom equivalent; the board keeps intentional horizontal scrolling
inside its columns. The new browser checks cover all four supported themes,
touch actions, slow and failed requests, and refresh after reconnect.

Limitations: Chromium automation did not change the browser's own zoom setting;
it reproduced 200% zoom's effective CSS viewport and pixel density. No
pre-implementation screenshots are available in the repository, so that
retrospective baseline cannot be recovered from this workspace. The final
seven-step usability walkthrough spans features planned for Phases 2–5 and
remains a whole-plan release gate.

- Give task creation two explicit entry points: **New task** for a single task
  and **Plan work** for a request that may produce several tasks. Show the
  destination stage before submission.
- Remove the duplicate Enter handler on the header input. Prevent repeated
  submissions while a request is pending; keep input on failure and show a
  useful error beside it.
- Add a visible mobile menu button outside the sidebar. The existing stylesheet
  hides the sidebar below 992px, including its internal toggle.
- Rename the backlog **Approve** action to **Move to To Do**. Reserve approval
  language for actual approval requests.
- Replace raw status values with readable labels and correct the Stage Policy
  text to describe the automatic handoff behavior implemented by the runtime.
- Make task actions visible when focused and usable on touch screens. Put
  destructive actions in an accessible overflow menu.

Acceptance criteria:

- One Enter press produces one request; repeated clicks while pending do not
  create duplicate work.
- Creation failures retain entered text and allow retry.
- Global navigation remains reachable at desktop, tablet, and phone widths.
- Keyboard and touch users can discover and operate task actions.
- Action labels accurately describe their effects.

Primary files: `data/templates/base.html`, `data/templates/kanban_board.html`,
`data/static/js/board.js`, `data/static/css/style.css`, and
`data/static/css/board.css`, under `src/agent_kanban_pm/`.

## Phase 2 — Guide setup and unify navigation

Priority: High. Depends on Phase 1 navigation work.

Validation update (2026-09-12): Phase 2 acceptance criteria are met. The
four-step setup guide, unified project navigation, Settings page, empty
assignment action, and consolidated Agents & Roles page are implemented. The
guide checks the configured worker CLI and durable agent sessions, queues a
Backlog task before offering worker assignment, and links failed or blocked
sessions to Activity. Folder pickers use the live API and preserve user input
when browse responses arrive out of order. An isolated Chromium walkthrough
used a throwaway database, HOME, workspace, CLI stub, and tmux socket to choose
a folder, configure the worker, create and queue a task, assign the worker,
and verify both an ACTIVE durable session and a running CLI marker. The full
suite passes (230 tests, including 19 Chromium browser tests); JavaScript
syntax and diff checks pass.

- Add a project setup checklist: **Choose folder → Configure worker → Create
  task → Start work**. Derive completion from actual configuration and task
  state; explain missing prerequisites beside the relevant action.
- Add **Configure agents** to an empty assignment dialog and explain why an
  unavailable role cannot be selected.
- Consolidate the Team directory and role configuration into **Agents & roles**.
  Show configured role, tool, availability, and current work as distinct fields.
- Derive manager labels from role configuration; remove the name-based
  Antigravity manager badge.
- Use consistent project navigation on every project page:
  **Board · Activity · Changes · Settings**. Activity contains the current
  Workbench capabilities; Changes contains the current Git view.
- Preserve existing URLs and task/session deep links while changing labels.
- Show navigation labels by default for new users; retain saved preferences.

Acceptance criteria:

- A new user can configure an available worker from the UI and understand how
  to get a first task running.
- Empty states provide a direct next action instead of a dead end.
- Every project page exposes the same navigation and indicates the active page.
- Configured roles and actual execution state are represented consistently.

Primary files: `data/templates/projects.html`, `data/templates/users.html`,
`data/templates/base.html`, `data/templates/project_workbench.html`,
`data/templates/project_git.html`, `data/static/js/role-settings.js`, and
`routers/ui.py`.

## Phase 3 — Make task details the main workspace

Priority: High. Depends on the terminology decisions in Phases 1–2.

Validation update (2026-09-12): A task opens in a single side panel, with
Overview, execution, approval, activity, log, and review views. Deep links,
browser Back, focus restoration, keyboard movement, explicit stage selection,
refresh preservation, and edit conflict handling have browser coverage. Named
priority options retain nonstandard existing values until changed.

- Open a task in a side panel instead of expanding multiple panes inside a
  board column. Use a full-width panel on small screens.
- Default to **Overview**: description, owner, priority, stage, execution state,
  blocker, and the next available action. Keep activity, logs, and reviews in
  secondary tabs.
- Keep cards concise: title, owner, priority, and the most relevant execution
  or attention indicator. Distinguish board stage from session state.
- Provide an explicit stage control alongside existing drag and keyboard moves.
- Use named priority choices with documented mappings to the existing 0–10
  values. Preserve existing numeric values unless the user changes priority.
- Support direct task links and browser back behavior. Preserve board position
  and filters when the panel closes, and preserve unsaved input during refresh.
- Make approval requests accessible from the task panel with task context,
  requested action, and clear decision controls.

Acceptance criteria:

- Users can read a description, assign work, inspect a blocker, and review output
  without opening the edit dialog or losing their position on the board.
- Task panels and tabs work by keyboard, expose appropriate accessible names
  and states, and restore focus when closed.
- Live updates preserve the selected task and unsaved edits; concurrent changes
  are reported instead of silently overwriting user input.
- Existing movement rollback, approval handling, and terminal links still work.

Primary files: `data/templates/kanban_board.html`, `data/static/js/board.js`,
`data/static/css/board.css`, and shared modal/navigation helpers.

## Phase 4 — Surface work that needs attention

Priority: Medium. Depends on task deep links from Phase 3.

Validation update (2026-09-12): Board search and attention filters use task
status, latest durable session state, and pending approvals. Counts and filters
survive board refresh. The dashboard links approvals, failed sessions, and
review-ready tasks to their task panels; project cards show progress and
attention. The connection indicator returns to Live only after refresh.

- Add board search by title or task ID and filters for **Needs me**, **Blocked**,
  **Running**, **Unassigned**, agent, and priority.
- Define filter semantics from actual task/session/approval data. Needs me
  should identify unresolved decisions requiring the operator, and Running
  should reflect execution rather than merely a task's column.
- Show active filters, matching counts, and a clear reset action. Distinguish
  an empty project from a filter with no matches.
- Put approvals awaiting a response, failed sessions, and work ready for review
  ahead of aggregate counts on the dashboard. Link each item to its task.
- Show useful progress and attention counts on project cards; move Delete into
  the project action menu.
- Display connection/reconnecting state so stale data is recognizable.

Acceptance criteria:

- A user can find a named task or isolate work needing attention without
  inspecting every column.
- Counts and filters remain correct after live updates and task movement.
- Dashboard attention items open the relevant task or approval.
- Reconnection clears the stale-data indicator only after data is refreshed.

Primary files: board scripts/templates, `data/templates/dashboard.html`,
`data/templates/projects.html`, and relevant UI query handlers.

## Phase 5 — Simplify visual design and planning

Priority: Medium. Build on the established navigation and task panel.

Validation update (2026-09-12): Task and shared card text/surfaces are clearer,
the Board/List control is labeled, and appearance options are secondary.
Planning has a read-only preparation endpoint and an editable preview. Browser
coverage confirms cancellation creates nothing, a double confirmation creates
only selected proposals once, and API coverage confirms preview leaves tasks
and STATUS.md unchanged.

- Increase essential text sizes and improve spacing. Start with 14–16px task
  titles and 12–14px supporting text, then validate density in the browser.
- Reduce glass effects, gradients, and decorative emoji. Use consistent icons,
  neutral surfaces, and semantic colors with text labels for status.
- Standardize buttons, forms, menus, badges, empty states, and feedback across
  pages. Use clear primary actions such as **Create task** and **Save changes**.
- Replace the opaque layout icon with a labeled **Board / List** control. Place
  density and appearance options in a secondary view menu.
- Add a Plan work preview where users can edit, remove, or accept proposed tasks
  before creation. Inspect the existing planning endpoint first: preview must
  not create tasks or write workspace artifacts. Separate preparation from
  commit if the current API combines them.
- Consolidate shared styles and presentation helpers as screens are updated to
  prevent inconsistent copies across templates.

Acceptance criteria:

- Essential information is readable in every supported theme and density.
- Status and actions remain understandable without color or hover.
- Supported screens work at narrow widths and 200% zoom, with intentional
  scrolling for boards, tables, and logs.
- Plan preview has no creation side effects; accepting creates only the selected
  tasks once, and cancelling creates none.

## Validation and release gates

Before implementation, capture the current board, project list, task dialogs,
Workbench, and Team page using representative sample data. Include a fresh
project, populated board, blocked task, pending approval, and failed session.

For each phase, run relevant existing tests and add focused regression coverage
for changed behavior. Prioritize duplicate submission, responsive navigation,
assignment/setup recovery, task-panel focus and deep links, live-update state
preservation, filter correctness, and preview-versus-commit behavior. Use mock
agent/session data for browser tests; real agent execution is not required for
layout and interaction validation.

Complete a browser walkthrough at 1440px, 1024px, 768px, and 390px, including
keyboard-only interaction, 200% zoom, supported themes, slow requests, failed
requests, and reconnects. Record any remaining limitations before releasing.

Final usability walkthrough:

1. Create a project and select its folder.
2. Configure a worker and create a single task.
3. Assign work and identify whether it is queued, running, or blocked.
4. Find and resolve an approval request.
5. Inspect the result and understand the review handoff.
6. Find the task again using search or an attention filter.
7. Preview a plan and create only the chosen tasks.

Success means a first-time user can complete these steps without CLI/API
instructions, distinguish planning from task creation, and explain what the
primary action will do before clicking it.

## Final validation record (2026-09-12)

The isolated sample included a fresh project, a populated board, a blocked task,
a failed durable session, and a pending approval. Chromium screenshots were
captured before Phases 3–5 in `/tmp/agent-kanban-ui-baseline` and after them in
`/tmp/agent-kanban-ui-final`. The final board and task panel were inspected at
1440px, 1024px, 768px, and 390px; light, dark, blue, and rose themes were
inspected at phone width. A 195px CSS viewport with device pixel ratio 2 stood
in for 200% zoom, where the plan preview and its actions remained within the
viewport. Browser regressions cover keyboard and touch actions, slow and failed
requests, reconnect refresh, setup and worker launch, approval decisions, task
search, panel navigation, and selected-only planning. The complete suite passed
with 237 tests; JavaScript syntax and diff checks passed.

Limitation: Browser zoom was emulated through viewport and pixel ratio rather
than changing Chromium's own zoom setting. Screenshots are local validation
artifacts, not tracked release assets.
