# Lifecycle hardening: implementation and remaining work

The lifecycle proposal was applied with the user's approval. `lifecycle-hardening.patch` is retained as the original proposal, not an outstanding patch to apply. The source now also includes corrections found by behavioral regressions.

## What is implemented

- A completed Git implementation records its committed revision. Test, diff-review, and Git/PR sessions use that implementation's workspace and identity; reviewers sharing the workspace run serially.
- Queued review requests cannot silently switch to a different implementation handoff.
- A successful process exit is insufficient for completion. The runtime requires a durable, run-scoped handoff; a known nonzero exit remains a failure even if a handoff exists.
- Review completion requires the configured roles and named outputs on the current implementation revision. Missing outputs, changed Git work, stale review results, and stale approvals block automatic movement.
- Human/orchestrator stage requirements remain effective. Default policies still require explicit movement; fully automatic progression requires deliberately configured policies.
- Conditional critical-review policy checks current-revision approvals and critical flags. REST and MCP review creation record the server's implementation revision.
- Stage movement and next-role assignment intents commit together. A fresh dispatcher recovers those intents. Duplicate move publication was removed.
- The same configured CLI can execute successive worker, test, diff-review, and Git/PR roles. Launch prompts now state the applicable output requirements and the Git commit requirement.
- Browser validation also corrected competing approval-toast wording and a folder-picker response overwriting an edit before typing began.
- Git/PR completion stores a server-verified GitHub artifact and requires the PR to be merged at the reviewed implementation SHA before Done.
- Portable execution receipts expose fresh cross-host heartbeats and Windows process identity; the workbench exposes launch queue cancel, retry, and archival controls.

## Verification design

`tests/test_lifecycle_chain.py` uses real temporary Git repositories/worktrees and a deterministic Python subprocess in place of an agent CLI. The terminal backend is substituted; this proves application orchestration, revision checks, handoff ingestion, and durable dispatch, not a real model's compliance with instructions.

It covers the complete four-role chain, same-CLI reuse, serial reviewers, dispatcher replacement after a committed handoff, stale queued identity, changed or dirty code, missing output, nonzero reviewer exit, legacy NULL worker roles, unverified finalizer calls, human policy, stale/critical/duplicate approvals, and REST/MCP revision capture. Existing browser and process-runner tests cover their own boundaries separately. Upgrade tests now remove and recreate the migration 13-17 columns.

## Final validation

The final validation uses the repository virtual environment so child processes
retain the same installed dependencies when tests replace `HOME`.

- Non-browser suite: **298 passed**.
- Browser suite, including the isolated real-tmux/CLI launch smoke test, launch-queue controls, and Activity handoff refresh: **33 passed**.
- Total: **331 passed**.
- `flake8 src tests`, `git diff --check`, and package compilation: passed.
- Wheel and source distribution built successfully; both passed `twine check`.
- The working `kanban.db` retained its pre-validation modification timestamp. Tests used throwaway databases and workspaces.

The final regression pass also corrected completion-gate queries to use persisted workflow keys, restored durable task-completed events across manual and automatic transitions, removed duplicate task-creation audit entries, prevented cancelled launch requests from starting after workspace preparation, and refreshed Activity handoffs without allowing older session responses to overwrite the current selection.

## Completed follow-up: launch recovery and unified transition enforcement

Migration 14 stores immutable launch specifications and the authorizing assignment actor. Requests now distinguish reserved, starting, started, completed, failed, queued, and blocked states. Reconciliation checks an existing reservation before comparing the original queue stage, preserving an already-started task's identity. Project approval is checked both before reservation and immediately before unclaimed execution.

`runtime/run_guard.py` creates an atomic, permanent execution claim before starting the command. The receipt records host, guard and child process identities, periodic heartbeats, and the final exit code. Local recovery compares process creation identity so a recycled PID cannot validate a run; remote observers of a shared receipt recognize a fresh heartbeat and later the final outcome. The Windows path uses native process handles and creation times. Interrupted, stale, corrupt, or remote-uncertain claims fail closed and are never replayed. A surviving local child keeps the session active when its guard/terminal is gone. These are at-most-once process boundaries, not an exactly-once guarantee for external services or restoration of a lost PTY's interactive connection.

`runtime/review_gate.py` now provides the evidence/authority check used by manual task mutations and automatic handoffs. Task services normalize status-only moves, reject incompatible stage/status pairs, use stored authority rather than an agent name, and enforce unfinished predecessors even for human callers. Explicit human evidence overrides are audited consistently through REST, UI, and MCP. Execution starts use that same mutation service with the recorded assignment actor. `runtime/stage_entry.py` shares transactional Review/Done role intents across manual and automatic moves. Stable workflow keys, rather than editable display order, determine reopening.

`tests/test_run_recovery.py` injects crashes before and after spawn, interrupts the real guard while its fake child survives, checks uncertain outcomes and duplicate suppression, and revokes project approval before/after reservation. `tests/test_transition_parity.py` runs equivalent mutations through REST/UI/MCP, checks status-only completion, authority spoofing, human audit records, predecessors, execution admission, and destination review policy with reordered columns. A former source-string assertion is now a behavioral role-routing check. Migration tests exercise versions 13-17.

Remaining recovery limits: receipts must be retained; cross-host observation requires a shared receipt directory; a stale remote heartbeat deliberately remains uncertain; legacy sessions lacking launch specifications are not reconstructed; and a lost interactive PTY cannot be reattached. The scheduler and dispatcher now coordinate through renewable database claims rather than process-local ownership. The legacy hypothetical MCP policy-preview tool still accepts caller-supplied flags; actual mutations use server evidence and do not trust that preview.

## Completed follow-up: evidence, delivery, queue control, and integration ordering

Migrations 15-17 add server-generated review digests, dispatcher and scheduler claim leases, pending-event deduplication, launch-request archival, and the corrected default policy that runs Git/PR work in Review. The latest decision for a revision is authoritative, so a newer pending, rejected, or changes-requested review revokes an older approval. Built-in output labels are accepted only when the recorded role, revision, successful exit, and summary support them; custom outputs must resolve to files inside the recorded workspace.

Outbox delivery now records a receipt per callback/channel and external adapter connection, so a failed consumer does not replay consumers that already succeeded. Renewable database leases prevent concurrent dispatcher or scheduler ownership during slow work. All in-process mutation producers write domain state and notification rows in one transaction. Managers can list, cancel, retry, and non-destructively archive launch requests from both the REST API and the workbench. Migration 17 stores verified handoff artifacts; Git/PR completion independently queries GitHub and requires a merged PR at the reviewed head revision.

## Remaining engineering debt, in recommended order

### 1. Evidence portability and workspace isolation (medium/high priority)

Git review text, changed paths, critical classification, and SHA-256 digest come
from a server snapshot bound to the implementation revision. Built-in outputs
are tied to successful recorded sessions, and custom outputs must name files
inside the workspace. Git/PR completion now additionally requires a
server-observed merged GitHub PR whose head SHA equals that revision.

Non-Git workspaces still lack a stable content identity, and shared review
workspaces remain writable; revision checks detect mutation after execution
rather than preventing it. Runtime-owned STATUS.md is excluded from revision
cleanliness and review snapshots. GitHub is the only verified remote provider,
and projects cannot yet choose whether Done means merged, deployed, or another
provider-specific state.

Needed: content identities for non-Git work, read-only reviewer worktrees or
filesystem enforcement, optional report digests/command provenance, additional
SCM providers, and a project-level integration policy.

### 2. Retry, retention, and dead-letter policy (medium priority)

Every in-process domain mutation producer now writes its outbox event in the
same transaction. Per-channel and per-adapter-connection receipts isolate
retries; renewable database claims coordinate concurrent dispatcher and
scheduler loops. Webhooks remain at-least-once across the final remote
acceptance/receipt commit boundary, so receivers must deduplicate by
`event_id`.

The workbench exposes launch request inspection, cancel, retry, and archival.
Capacity retries still lack permanent-error classification, repeated capacity
checks can write repeated messages, and retained outbox events, receipts,
execution receipts, and workspaces need bounded retention.

Needed: dead-letter state and UI, bounded retry classes, deduplicated capacity
reporting, receipt retention policy, and indexed legacy task/agent recovery.

### 3. Testing and module boundaries (medium priority)

Behavioral tests cover lifecycle, provider evidence, crash recovery,
cross-interface policies, outbox delivery, and browser queue controls. Launcher
and streamer functions still combine database state, subprocess control, policy
decisions, prompting, and event creation. Some older tests still inspect source
strings.

Needed: extract admission, workspace preparation, process recovery, integration
evidence, and transition decisions into explicit services. Replace remaining
source-text assertions and add a true two-host/shared-filesystem integration
test plus remote API contract tests.

## Scope and rollout

This work changes local source and tests. It does not deploy or migrate the user's working database. Back up the database before adopting migrations 13-17 in a running installation. Usage accounting and quota-based model routing remain outside this patch.
