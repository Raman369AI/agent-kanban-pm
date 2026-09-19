# Reliability and workflow follow-up plan

Date: 2026-09-19

Reviewed implementation: `259f8ec31558641fba002aed510e949624ba7120`

Release preparation: `0.6.0`

## Scope and evidence

This plan records the seven remaining flaws identified after the lifecycle and
UI changes. Every item below remains open; documenting it does not mean it has
been fixed. The subsequent upstream merge `ea13868` changed only README text.

The application started successfully on `http://localhost:8000/ui/projects`.
Health, dashboard, agent settings, and project Board, Activity, Settings, and
Changes pages returned HTTP 200. The existing database was backed up before
startup. The standalone role supervisor was disabled; the task scheduler
remained active.

The prior implementation validation passed 298 backend tests and 33 browser
tests, plus lint and distribution checks. The findings below came from static
source review, not runtime reproductions. Threadline and a connected browser
were unavailable for this review. Static review does not observe execution or
prove correctness; each fix needs a regression covering its reported trigger.

Source references below describe the reviewed snapshot and may move as fixes
are implemented.

## 1. High: preserve assignment delivery during shutdown

- [ ] Fix and verify.

**Trigger and impact:** Assign work and shut down before the outbox delivers
the assignment. Shutdown removes the launch subscriber before draining events,
so the event can be marked delivered without creating a launch request. Startup
recovery treats the recorded assignment event as already known and skips it;
the assigned task never launches.

**Evidence:** `src/agent_kanban_pm/app.py:318`,
`src/agent_kanban_pm/events.py:119`, and
`src/agent_kanban_pm/runtime/scheduler.py:151`.

**Proposed fix:** Retain the launch subscriber until pending events are safely
drained, or leave those events pending for restart. Reconcile assignments using
durable launch-delivery evidence rather than the mere existence of an outbox row.

**Acceptance:** A committed assignment followed immediately by shutdown and
restart produces exactly one durable launch request and does not lose work.

## 2. High: preserve reserved-session ownership through recovery errors

- [ ] Fix and verify.

**Trigger and impact:** Revoke project approval after a session is reserved,
allow recovery to fail, then cancel its request. The scheduler clears
`LaunchRequest.session_id` while the reserved session remains open. Cancellation
can leave an orphaned STARTING session that blocks capacity; a later retry can
reuse it and revive the original cancelled request.

**Evidence:** `src/agent_kanban_pm/runtime/scheduler.py:119`,
`src/agent_kanban_pm/routers/agent_activity.py:1485`,
`src/agent_kanban_pm/runtime/assignment_launcher.py:577`, and
`src/agent_kanban_pm/runtime/assignment_launcher.py:981`.

**Proposed fix:** Preserve the existing session link when recovery fails.
Resolve cancellation against the session's durable request relationship, and
recheck request state and ownership before resuming an unstarted process.

**Acceptance:** A reservation followed by approval revocation, recovery failure,
cancellation, and later reapproval cannot restart the cancelled run. The session
and queue record stay consistent, and abandoned reservations release capacity.

## 3. High: bind review requester identity to the authenticated caller

- [ ] Fix and verify.

**Trigger and impact:** A worker creates a review with themselves as reviewer
and another entity as requester, then approves it. The REST endpoint accepts
the supplied requester, while the independent-review check trusts that stored
identity. This bypasses the intended separation between requester and reviewer.

**Evidence:** `src/agent_kanban_pm/routers/agent_activity.py:1369` and
`src/agent_kanban_pm/services/coordination.py:35`.

**Proposed fix:** Derive requester identity from authentication and reject
conflicting caller-supplied values. Define and enforce reviewer independence
against the actual requester and, where required, the implementation author.
Keep REST and MCP behavior consistent while retaining explicit manager authority.

**Acceptance:** A worker cannot create an apparently independent approval by
substituting another requester. A legitimate independent reviewer still works,
and stored review identity agrees with the event and audit records.

## 4. High: verify the PR destination as well as its commit

- [ ] Fix and verify.

**Trigger and impact:** Submit a PR merged into a personal fork or the wrong
branch at the same implementation SHA. Current verification checks merged state
and head SHA but does not bind the destination repository or base branch to the
project, so unrelated integration can satisfy completion evidence.

**Evidence:** `src/agent_kanban_pm/runtime/integration_evidence.py:44` and
`src/agent_kanban_pm/runtime/integration_evidence.py:58`.

**Proposed fix:** Record the project's intended integration repository and base
branch, verify both against provider metadata, and retain that destination in
the verified artifact. Make any alternate destination policy explicit.

**Acceptance:** Reject a merged PR with the correct SHA but the wrong repository
or branch. Accept a merged PR matching the configured destination and reviewed
revision. Missing destination metadata must not silently satisfy the gate.

## 5. Medium: make nested workflow overrides visible and keyboard-accessible

- [ ] Fix and verify.

**Trigger and impact:** Submit a gated task edit or role assignment. The override
dialog opens while its originating modal remains visible. Equal stacking order
and DOM order put the originating modal above it, and keyboard handling targets
that original modal. The required override cannot be completed directly.

**Evidence:** `src/agent_kanban_pm/data/static/js/board.js:1393`,
`src/agent_kanban_pm/data/static/js/board.js:1481`,
`src/agent_kanban_pm/data/templates/kanban_board.html:471`, and
`src/agent_kanban_pm/data/static/js/main.js:146`.

**Proposed fix:** Suspend the originating modal or manage an explicit modal
stack with correct stacking order, focus trapping, Escape handling, and focus
restoration. Preserve the originating form's draft when the override is cancelled.

**Acceptance:** Mouse and keyboard users can complete or cancel required
overrides from both Edit and Assign. Cancellation preserves entered data; a
confirmed override submits once and records its reason.

## 6. Medium: keep the reviewed diff base immutable

- [ ] Fix and verify.

**Trigger and impact:** Approve an implementation, merge it, then fetch the
updated default branch. Diff generation recalculates the merge base against
that moving branch. The diff can become empty while the implementation HEAD is
unchanged; conditional review policy then rejects the changed digest and blocks
Done during an otherwise valid merge-before-completion workflow.

**Evidence:** `src/agent_kanban_pm/services/git_diff.py:74` and
`src/agent_kanban_pm/runtime/review_gate.py:84`.

**Proposed fix:** Persist the reviewed base commit with the implementation or
review evidence and use the same base for subsequent digest verification. Treat
a real implementation change as a new review, not a baseline refresh.

**Acceptance:** A real-Git regression approves a diff, merges its commit into the
default branch, fetches, and verifies that unchanged reviewed work remains valid.
Changing the implementation revision or content must still invalidate approval.

## 7. Medium: preserve Activity approval-note drafts during refresh

- [ ] Fix and verify.

**Trigger and impact:** Type an approval or rejection explanation while another
approval event arrives. Refresh rebuilds all pending cards using empty note
inputs and discards the unsaved explanation.

**Evidence:** `src/agent_kanban_pm/data/static/js/project-workbench.js:303`,
`src/agent_kanban_pm/data/static/js/project-workbench.js:323`, and
`src/agent_kanban_pm/data/static/js/project-workbench.js:429`.

**Proposed fix:** Keep drafts keyed by approval ID or update cards without
replacing active inputs. Handle remotely resolved approvals explicitly and
preserve focus and text selection during unrelated refreshes.

**Acceptance:** An unrelated approval event preserves the note, focus, and
selection. Submitting sends the preserved note once, and a remotely resolved
approval cannot be submitted again.

## Delivery and verification

1. Address items 1-4 first: durable execution and evidence provenance.
2. Address item 5 to unblock the affected manual workflow, then items 6-7.
3. Add focused behavioral regressions for each trigger before marking it done.
4. Run the relevant backend and browser suites, then the release checks in
   [RELEASING.md](RELEASING.md). Keep tests on throwaway databases and workspaces.
5. Update this plan and release notes with actual outcomes and remaining limits.

The documented explicit human override is intentional and is not one of these
defects. This plan does not authorize implementing the fixes as part of release
tag preparation; the open items remain follow-up work.
