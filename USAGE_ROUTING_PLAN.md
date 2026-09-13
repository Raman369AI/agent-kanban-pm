# Plan: Usage Tracking and Agent/Model Switching

Goal: know what every CLI session consumed and how much headroom each CLI has
left, then use that to pick the CLI and model for each launch, and to hand a
task to another CLI when the current one hits a limit.

Non-goals: proxying API traffic, reading credentials, scraping prices online,
or switching agents because a task "looks hard". The runtime stays local and
single-operator.

---

## What exists today, and what gets in the way

| Area | Current behaviour | Consequence for this plan |
|------|-------------------|---------------------------|
| Session record | `AgentSession` stores `command`, requested `model`, `status`, timestamps. No token, cost, or end-reason fields. | Need a ledger and an end reason. |
| Session end | `_stream_one_session` marks a session `DONE` whenever its tmux session disappears; only a launch exception sets `ERROR`. | A run that stopped on a usage limit is indistinguishable from a finished one. |
| Agent identity | An agent is a CLI: the entity name is the adapter name, the worktree is `task-{id}-{agent}`, the branch `kanban/task-{id}-{agent}`, checkpoints are keyed by `(task, agent)`. | Reassigning a task to another CLI today starts a fresh worktree from base and loses in-progress work. |
| Model catalog | Adapter `models` are static: `codex-default`, `opencode-default`, `antigravity-default`, and a single `claude-sonnet-4-6` for claude. `antigravity.yaml` has no `model_flag` even though `agy --model` exists. | Switching models needs a real catalog first. |
| Launch gate | `_scheduling_blocker` enforces project/agent parallel limits before a launch. | Natural place for a capacity check. |
| Pane scanning | Every 5 s the streamer captures 200 lines and runs data-driven `prompt_patterns`. | Limit detection can reuse the same mechanism. |

## Usage sources

Checked against the CLIs installed on the development machine on 2026-09-11.
Every source is an undocumented CLI internal and must be parsed defensively.

| CLI | Source | Match to our session | Tokens | Cost | Actual model | Remaining quota |
|-----|--------|----------------------|--------|------|--------------|-----------------|
| claude | `~/.claude/projects/<slug>/<session>.jsonl`, assistant lines | `cwd` | `message.usage`: input, output, cache creation, cache read | — | `message.model` | Not in transcripts; pane text only |
| codex | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | `session_meta.cwd` | `token_count.info.total_token_usage` (input, cached, output, reasoning) | — | `turn_context.model` | `token_count.rate_limits`: `primary`/`secondary` `used_percent`, `window_minutes`, `resets_at`; `credits`; `plan_type` |
| opencode | `~/.local/share/opencode/opencode.db`, `session` table | `session.directory` | `tokens_input/output/reasoning/cache_read/cache_write` | `session.cost` | `session.model` | — |
| aider | Terminal `Tokens: … Cost: …` lines; `--analytics-log` file | Our own pane | Pane | Pane | `--model` | — |
| antigravity | `~/.gemini/antigravity-cli/conversations/<id>.db`, `gen_metadata` blobs mention usage | Unverified (`conversation_summaries.workspace_uris`) | Encoding not decoded | — | Unverified | Logs show a quota manager; no readable figures found |

Confirmed limit text: codex prints `You've hit your usage limit. Upgrade to Pro …`.
Claude and Antigravity limit messages must be captured from real runs before
patterns ship; do not guess them.

---

## Phase A — Know why a session ended

Small, independently useful, and a prerequisite for everything else.

1. Add `AgentSession.end_reason`: `completed`, `handoff`, `rate_limited`,
   `quota_exhausted`, `auth_failed`, `crashed`, `cancelled`, `unknown`.
2. Add an adapter `limits:` block, loaded like `prompt_patterns` (bundled YAML,
   then `~/.kanban/limit_patterns.yaml`):
   ```yaml
   limits:
     - regex: "You've hit your usage limit"
       kind: quota_exhausted
       resets_at: null   # optional named group to parse a reset time
   ```
3. In `_stream_one_session`, match limit patterns next to `detect_prompt`. On a
   match: set the session `BLOCKED` with the end reason, write a
   `QuotaSnapshot` (below) at 100 %, log a `TaskLog`, and publish
   `AGENT_LIMIT_REACHED`.
4. When tmux disappears, classify instead of defaulting to `DONE`: `completed`
   if STATUS.md reported done, otherwise the last matched limit, otherwise
   `crashed`.

## Phase B — Usage ledger

1. **Tables** (migration 11):
   - `usage_records`: session, task, project, agent, role, cli, model,
     provider, input/output/cache-read/cache-write/reasoning tokens,
     `cost_usd` (nullable), `cost_source` (`reported` | `priced` | null),
     `source` (`transcript` | `db` | `pane`), `external_session_id`,
     `attribution` (`exact` | `approximate`), `captured_at`. Unique on
     `(session_id, source, external_session_id)`; collectors upsert
     cumulative totals, so re-reading a log never double-counts.
   - `quota_snapshots`: cli, plan type, window (`primary` | `secondary` |
     `credits`), `used_percent`, `window_minutes`, `resets_at`, `observed_at`.
   - `AgentSession.resolved_model`: the model the logs say actually ran,
     which can differ from the requested one.
2. **Collectors** in `runtime/usage/`, selected per adapter
   (`usage: {collector: claude_transcripts}`):
   `claude_transcripts`, `codex_rollouts`, `opencode_db`, `aider_pane`, plus an
   `antigravity` spike. Each implements
   `collect(workspace_path, started_at, ended_at) -> list[UsageSnapshot]` and
   optionally `quota() -> list[QuotaSnapshot]`.
3. **Rules every collector follows**
   - Extract numbers and model names only; never store prompt or code content.
   - Open SQLite read-only (`mode=ro`); never touch `auth.json`,
     `oauth_creds.json`, or similar files.
   - On an unrecognised shape, return nothing and log once per CLI version
     (from `info_flag`). Missing usage is shown as unavailable, never zero.
   - Worktrees are per task, so matching on `cwd` is exact. Projects without
     git isolation share `project.path`; fall back to time-window overlap and
     mark the row `approximate`.
4. **Scheduling:** a `usage_sweeper` loop runs collectors every 60 s in
   `asyncio.to_thread`, plus one final sweep when a session is finalized.
   Collectors stay out of the 5 s pane loop.
5. **Quota is account-wide.** The user's own CLI use outside Kanban consumes the
   same allowance, so remaining quota comes only from CLI-reported snapshots,
   never from summing our ledger.
6. **Cost:** show tokens as the primary figure. Use `cost_usd` where the CLI
   reports it (opencode, aider). For the rest, apply an optional user-owned
   `~/.kanban/pricing.yaml`; ship no bundled prices, since they go stale and
   subscription plans make per-token cost notional.
7. **Surfaces**
   - API: `GET /agents/usage?project_id=&task_id=&group_by=cli|model|role|day`,
     `GET /agents/quota`.
   - CLI: `kanban usage [--project] [--by cli|model|role] [--since]`, beside
     `kanban audit`.
   - Board: tokens and actual model in the task card's activity tab, a Usage
     tab in the project workbench, and a remaining-quota chip per CLI in the
     header.
   - MCP: a `get_usage` tool so the orchestrator role can reason about budget.

## Phase C — Model catalog

1. Add an optional `models_command` to adapters and cache the output in
   `~/.kanban/model_catalog.json` with a discovery timestamp: `opencode models`
   and `agy models` exist. Claude and codex have no list command, so they keep
   static entries plus user-added ones.
2. Add `model_flag: "--model"` to `antigravity.yaml`.
3. Let users tag models in preferences with `tier` (`fast` | `balanced` |
   `frontier`) and `context_window`. No CLI reports a tier, so the router
   must not invent one.
4. Revisit the static `claude-sonnet-4-6` default. Since 0.4.0rc7 passes the
   role's model to task sessions, a claude role with no model set now launches
   with `--model claude-sonnet-4-6` instead of the CLI's own default. Replace it
   with a `default` label unless pinning that model is intended.

## Phase D — Routing policy (choosing before launch)

1. **Configuration** per role in `preferences.yaml`:
   ```yaml
   roles:
     worker:
       agent: claude
       model: default
       fallbacks:
         - {agent: codex, model: default}
         - {agent: opencode, model: <provider/model>}
       routing:
         strategy: ordered        # ordered | headroom | cost
         switch_on: [rate_limited, quota_exhausted]
         min_headroom_percent: 10
         max_switches_per_task: 2
   ```
   Listing a fallback is the consent to use it. The router never picks a CLI
   the user did not list.
2. **Router** (`runtime/routing.py`), a pure function
   `choose(role, task, candidates, state) -> Decision(agent, model, reason, rejected)`:
   - Hard filters: installed; adapter supports the role; not limited (latest
     snapshot reached, `resets_at` in the future); remaining quota at least
     `min_headroom_percent` where known; passes `_scheduling_blocker`.
   - Ordering: `ordered` (default, predictable), `headroom` (most remaining
     quota), `cost` (cheapest priced).
   - Autonomy is inherited from the role and never escalated by a switch.
   - Unhealthy cooldown: two `crashed` ends in a row take a candidate out for a
     configurable period. `auth_failed` never triggers a silent switch; it is
     surfaced as a configuration problem.
3. **Wire-in:** `_launch_for_assignment` calls the router before building the
   command. Every decision, including rejected candidates and reasons, is
   written as a routing decision row and a `TaskLog`, so "why did this run on
   codex?" is always answerable.

## Phase E — Handover mid-task

This is the hard part, because workspace identity is currently tied to the CLI.

1. **Key worktrees by task and role, not agent:** `task-{id}-{role}` on
   `kanban/task-{id}-{role}`. A switch within a role then reuses the same
   worktree, uncommitted changes included, with no WIP commit on the user's
   behalf. Existing open sessions keep their legacy agent-keyed paths until they
   end. Review roles keep separate worktrees because they run in parallel.
2. **Cross-agent context:** the handover prompt loads the latest checkpoint for
   the task regardless of agent, STATUS.md, and `git diff --stat` against base,
   prefixed with "Continuing work started by `<cli>`, which stopped because
   `<reason>`."
3. **Sequence:** limit detected → kill the old tmux session → end the old
   session with `end_reason=handoff` → router picks the next eligible fallback →
   replace the task's agent assignee → launch in the same worktree. The unique
   open-session index is per `(agent, task)`, so the two sessions never collide.
4. **No ping-pong:** when the original CLI's `resets_at` passes, running
   tasks stay where they are; only new launches route back to the primary.
   `max_switches_per_task` caps churn. When no eligible candidate remains, the
   task stays `BLOCKED` with the reset time shown on the card, and the launcher
   retries after `resets_at`.
5. **Supervised roles:** an optional `agent_switch` entry in
   `autonomy.require_approval_for` files an approval before switching, for
   users who want to confirm every handover.

## Phase F — Learned routing (optional, later)

1. Per `(role, cli, model)`, record: reached review without handover, moved back
   from review, tokens per completed task, wall time, and limit hits.
2. A `kanban usage --routing-report` table and a workbench view of those rates.
3. A `learned` strategy scores candidates only after a minimum sample (for
   example five tasks), falls back to `ordered` below it, and states its score
   in the decision log. No bandit algorithms in the first version.

---

## Testing

- **Collectors:** synthetic fixtures shaped like each log format; never commit
  lines copied from real transcripts. Include a drifted-schema fixture per
  collector that must yield "unavailable", not a crash or zeros.
- **Idempotency:** running a collector twice over the same log leaves one row
  with unchanged totals.
- **Router:** table-driven tests over candidates × snapshots × strategies,
  including "every candidate limited" and "auth failure does not switch".
- **Handover end-to-end:** a fake adapter script prints the codex limit line
  and exits; assert the task moves to the listed fallback, reuses the worktree,
  keeps its uncommitted file, has exactly one open session, and logs the reason.
- **Migration:** extend `tests/test_db_upgrade.py` for the new columns.

## Risks

| Risk | Mitigation |
|------|-----------|
| CLI log formats change without notice | Version-gated, tolerant parsers; show "usage unavailable"; fixture per known version |
| Transcripts contain private code and prompts | Collectors read numeric and model fields only; nothing else is persisted |
| Limit messages differ by plan and locale | Patterns are data, overridable in `~/.kanban`; ship only patterns backed by captured output |
| Fallback CLI produces different-quality work | Only user-listed fallbacks; autonomy never escalates; review stage still gates Done |
| Shared worktree between successive CLIs leaves confusing state | Handover prompt summarises the diff; one session at a time per role worktree |

## Sequencing

| Order | Milestone | Depends on | Suggested release |
|-------|-----------|------------|-------------------|
| 1 | A — end reasons and limit patterns | — | 0.5.0a1 |
| 2 | B — ledger, claude/codex/opencode collectors, `kanban usage`, API | A | 0.5.0a1 |
| 3 | C — model catalog, antigravity `model_flag`, claude default review | — | 0.5.0a2 (item C4 can ship as a 0.4.0 fix) |
| 4 | B.7 UI surfaces and quota chip | B | 0.5.0a2 |
| 5 | D — pre-launch routing with fallbacks and decision log | A, B, C | 0.5.0b1 |
| 6 | E — mid-task handover with role-keyed worktrees | D | 0.5.0b2 |
| 7 | F — learned routing and report | B, D | later |

Stable `0.4.0` does not need to wait for any of this.

## Decisions to confirm

1. **Automatic handover for listed fallbacks** (recommended), or an approval
   for every switch by default?
2. **Worktrees keyed by `(task, role)`** — confirm this fits how review roles
   read the worker's branch before Phase E starts.
3. **Tokens first, cost only where reported or user-priced** (recommended), or
   ship a bundled price table?
