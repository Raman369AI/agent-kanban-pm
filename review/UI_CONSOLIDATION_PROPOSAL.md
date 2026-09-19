# UI consolidation proposal

## Recommendation

Keep the existing top-level pages and routes. The current navigation is small, and each page has a distinct job:

| Page | Decision | Reason |
| --- | --- | --- |
| Dashboard (/) | Keep | Cross-project triage: decisions, failures, totals, and recent work. |
| Projects (/ui/projects) | Keep | Project inventory and project-level actions. |
| Board (/ui/projects/{id}/board) | Keep | Primary planning and task movement surface. |
| Activity (/ui/projects/{id}/workbench) | Keep | Live runtime operations, approvals, handoffs, and launch recovery. |
| Changes (/ui/projects/{id}/git) | Keep | Source-control history and GitHub synchronization have different data and filtering needs from runtime activity. |
| Settings (/ui/projects/{id}/settings) | Keep | Project name, description, workspace, completion policy, and destructive actions belong outside daily task work. |
| Agents & Roles (/ui/users) | Keep | This is the canonical global agent and role configuration page. |

Do not merge Dashboard with Projects, Board with Activity, or Activity with Changes. Those combinations would create denser pages with competing primary actions and different refresh behavior.

## Consolidations to make

### 1. Make Agents & Roles the only full role editor

Project Settings currently describes its role editor as project-specific, but it edits the same global role preferences used by the Agents & Roles page and the Board modal. That is misleading and creates three editing surfaces for one configuration.

- Keep the complete editor on Agents & Roles.
- Replace the Project Settings editor with a compact read-only role summary and a **Manage global roles** link.
- Keep the Board setup action because it is needed during onboarding, but label it **Configure global worker role** and reuse the same shared component.
- Support a return URL and focused role, for example /ui/users?focus=worker&return=/ui/projects/3/board, so setup does not become a dead end.

### 2. Merge the first three Activity tabs into Sessions

Live, Terminal, and Handoff all operate on the same session list. Requiring users to select the same session in three tabs fragments one investigation.

Replace the six tabs with four:

1. **Sessions** — live agent summary and session table; selecting a session opens its terminal output and durable handoff in one detail pane.
2. **Approvals** — pending actions and resolved approvals.
3. **Decisions** — orchestration audit history.
4. **Launch queue** — queued, blocked, failed, and completed launch attempts.

This is an internal merge within Activity. It does not change the route or remove any information.

### 3. Remove duplicate project actions

The Board already has project subnavigation, but its header and Advanced section repeat Activity, Changes, GitHub sync, and role controls.

- Remove the second Activity button from the Board header.
- Remove **Git view** from Advanced because Changes is already in project navigation.
- Keep GitHub synchronization on Changes as its canonical action.
- Keep setup-specific role configuration in the checklist; remove the duplicate generic Team roles action from Advanced.
- Keep explicit links on project cards for keyboard and assistive-technology users even though clicking the card body opens the Board.

## Highest-priority UI issues

### Completion is invisible

A Review card does not explain why it has not moved to Done. The task drawer only reports a generic blocker such as None, even though completion can be waiting for tests, diff approval, a Git/PR handoff, or an exact merged revision.

Add a **Completion gates** section to Review tasks:

- Implementation handoff and reviewed commit
- Test result
- Diff review decision
- Human decision when the change is critical
- Pull request supplied
- Pull request merged at the reviewed SHA

Each gate should show Complete, Waiting, or Blocked, with the latest concrete reason. The card itself should show a compact value such as **Review 2/5**.

### Manual Done overrides are too quiet

Dragging a task to Done as a human currently bypasses missing workflow evidence and writes an audit log, but the UI sends an empty summary and only shows a generic success toast.

Before a manual override:

- Show the specific unsatisfied gates.
- Require an override reason.
- State that the move will be recorded in the audit trail.
- Use an explicit **Move to Done anyway** action.

Ordinary moves with all evidence satisfied should remain one step.

### Page and control names are inconsistent

The project navigation says **Activity**, the HTML title says **Workbench**, and the page repeats a Workbench badge. Use **Activity** everywhere or rename it everywhere to **Operations**. The simpler change is to use **Activity** consistently and remove the redundant badge.

The Board/List view switch also displays the current view while its accessible label describes the next action. Label it with the action: **Switch to list** while in Board view and **Switch to board** while in List view.

### Dashboard task cards do not navigate

Recent Projects are linked, but Recent Tasks are static. Make each recent task open its project Board with the task drawer selected.

### Feedback patterns are inconsistent

Some pages use toasts, others use alert(), and deletes use native confirm(). Use the existing modal and toast system for save errors, destructive confirmation, and completion overrides. Errors should remain visible long enough to act on and should never be inserted as raw HTML.

## Maintainability issues behind the UI defects

- The folder picker exists in three separate implementations. The broken folder navigation came from two copies rendering JavaScript inside HTML attributes. Extract one folder-picker partial and one shared JavaScript module.
- Project subnavigation is duplicated across four templates. Extract a shared partial so labels, active state, and new destinations cannot drift.
- Project pages contain large blocks of inline CSS and JavaScript. Move page behavior into static modules and use DOM APIs for external data.
- Project Settings and Agents & Roles insert some error messages with innerHTML; render those with textContent.
- Changes places a contribution URL directly into generated markup. Validate allowed URL schemes and build the link with DOM properties.
- The clickable project card is mouse-friendly but its card body is not a semantic link. Make the title or a full-card overlay a real link while keeping action buttons above it.

## Delivery order

### Phase 1: clarity and safety

1. Add Completion gates to the task drawer and Review cards.
2. Add the explicit, reasoned human override dialog.
3. Consolidate global role editing and correct its labels.
4. Remove duplicate Board actions and normalize Activity naming.
5. Make Dashboard recent tasks navigable and make project cards semantic links.

### Phase 2: Activity consolidation

1. Build the Sessions master-detail view.
2. Move Terminal and Handoff into the selected-session detail.
3. Preserve deep links such as #terminal:task:123 by mapping them to the Sessions view.
4. Retain Approvals, Decisions, and Launch queue as separate tabs.

### Phase 3: shared UI components

1. Extract the folder picker and project subnavigation.
2. Standardize dialogs, toasts, empty states, loading states, and error rendering.
3. Move inline page styles and scripts into versioned static assets.

## Acceptance checks

- Existing top-level URLs remain valid.
- No feature disappears from Activity after the tab consolidation.
- A user can understand exactly why any Review task is not Done without opening logs.
- A manual Done override cannot be submitted without a reason.
- Role changes clearly state that they apply globally.
- Folder selection behaves identically from project creation, Board setup, and Settings.
- Project cards and Dashboard recent tasks work with mouse and keyboard.
- Desktop and mobile browser workflows pass, including deep links and back navigation.
