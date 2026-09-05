# Plan: Make Agent Kanban PM a Standalone, Fully Available Project

Goal: a project anyone can discover, `pip install`, run in under five minutes,
and trust enough to point at their own repositories. Current state is a tested release candidate with installable artifacts. This plan is ordered so each phase
is releasable on its own.

---

## Phase 0 — Stabilize what exists (completed)

Phase 0 was completed in July 2026. The items below are retained as the
implementation record.

1. **Fix the failing test.** `tests/test_ui_routes.py::test_ui_routes_and_board_render`
   fails because one UI route still uses the legacy
   `TemplateResponse(name, {"request": ...})` call order. Find it in
   `routers/ui.py` and switch to `TemplateResponse(request, name, context)`.
2. **Make CI test the package, not the checkout.** The workflow installs
   `requirements.txt` and runs pytest against the working directory, so entry
   points, package data, and the sdist/wheel are never exercised. Add:
   - `pip install -e ".[dev]"` instead of `-r requirements.txt`
   - a `python -m build && twine check dist/*` step
   - a job that installs the built wheel in a clean venv and runs
     `kanban --help` plus a server-boot smoke test.
3. **Single source of dependencies.** Drop `requirements.txt` (or generate it
   from `pyproject.toml`); add `pytest-timeout` to the `dev` extra so local and
   CI environments match.
4. **Delete dead config.** `.env.example` documents `LOCAL_DEV_MODE`, which no
   code reads. Replace `.env.example` with the env vars that actually exist
   (`KANBAN_PORT`, `KANBAN_API_BASE`, `DATABASE_URL`, `KANBAN_INSTANCE_ID`,
   `KANBAN_USER_NAME`, `KANBAN_USER_EMAIL`, `KANBAN_AGENT_NAME`, ...).
5. **Remove dev-artifact heuristics from product code.** `kanban sheet` hides
   projects matching hardcoded markers ("phase 6", "/tmp/", "folder picker
   smoke"). Replace with a real flag on the row (e.g. `Project.is_demo`) or
   drop the filtering.

## Phase 1 — Packaging correctness (completed)

1. **Adopt a single-package src layout (completed).** Previously, `pip install` dropped `main`,
   `auth`, `database`, `models`, `schemas`, `adapters`, `event_bus`,
   `websocket_manager`, `open_project`, `mcp_server`, and a generic `routers`
   package into top-level site-packages — near-guaranteed collisions with any
   other installed package. Implemented structure:

   ```
   src/agent_kanban_pm/
     __init__.py
     app.py            (was main.py)
     auth.py, db.py, models.py, schemas.py, events.py, ws.py
     routers/
     runtime/          (was kanban_runtime/)
     cli/              (was kanban_cli/)
     mcp/server.py     (was mcp_server.py)
     data/             (templates, static, agents, mcp_configs)
   ```

   Update `pyproject.toml` (`[tool.setuptools.packages.find] where = ["src"]`),
   remove `py-modules`, keep `kanban` / `kanban-cli` scripts, and fix
   `kanban run` to spawn `uvicorn agent_kanban_pm.app:app`. This is a
   mechanical rename plus import rewrites; do it in one PR with tests green
   before and after.
2. **Declare the `mcp` dependency.** `mcp_server.py` imports `mcp` but no
   dependency file lists it, so the headline MCP feature fails on every clean
   install (it is not even in the dev venv, so tests never exercise the real
   server). Add `mcp>=1.0` to `[project.dependencies]` and add a
   `kanban-mcp` console script so docs can stop saying `python mcp_server.py`.
3. **Fix `_is_primary_worktree()` for installed mode.** It compares the project
   root against the package's parent directory, which is site-packages after
   install, so every install is treated as a "secondary worktree". Redefine
   the default data home as `~/.kanban/` (db at `~/.kanban/kanban.db` or
   XDG_DATA_HOME) with per-project instance dirs; keep CWD-relative `./kanban.db`
   only behind an explicit `KANBAN_DEV=1` or when a `kanban.db` already exists.
4. **Python version window.** Add 3.13 to CI matrix and classifiers; state
   Linux/macOS support explicitly (the tmux/pty runtime is Unix-only — the PTY
   fallback imports `pty`), and emit a clear error on Windows instead of a
   traceback. WSL note in README.

## Phase 2 — Security hardening (completed)

1. **Close the `/ui` auth bypass (completed).** `token_auth_middleware` now
   requires the token on every non-safe request, including `/ui/*` mutations,
   and on all methods for `/ui/api/*` JSON endpoints. The `kanban-token`
   cookie is `HttpOnly` + `SameSite=strict`; cookie-authenticated mutations
   must also send `X-CSRF-Token`, an HMAC of the instance token embedded in
   every page as a meta tag and attached by a `fetch` wrapper in `base.html`.
   Header-authenticated callers (`X-Kanban-Token`, CLI/supervisor) skip CSRF.
   `TrustedHostMiddleware` rejects non-loopback Host headers before routing
   (`KANBAN_ALLOWED_HOSTS` extends the allowlist).
2. **Protect the token file (completed).** `get_auth_token()` creates
   `~/.kanban/token` with `0600` up front and tightens pre-existing
   loose-permission files on read.
3. **Make auto-approval an explicit choice (completed).** Adapter YAMLs moved
   bypass flags (`--permission-mode bypassPermissions`, `--approval-mode
   yolo`, `--full-auto`, `--yes-always`) from `task_command.args` to
   `task_command.auto_args`, which the launcher and role supervisor append
   only when the role's `autonomy` is `auto`. `RoleAssignment.autonomy`
   defaults to `supervised` (unknown values fall back to supervised), and
   `kanban init` asks once, loudly, before enabling AUTO for all roles;
   `kanban roles assign --autonomy` and the roles UI API accept the knob.
4. **WebSocket auth (completed).** The HTTP middleware exempts WebSocket
   upgrades, but both `/ws` and `/ws/projects/{project_id}` now verify the
   Kanban token before subscribing. Keep regression coverage for query-string,
   cookie, and authorization-header authentication.

## Phase 3 — Runtime correctness

1. **Stop blocking the event loop (completed).** `launch_for_assignment()` runs
   `git fetch`/`rebase`/`worktree add` via synchronous `subprocess.run` inside
   an async event handler **while holding an open DB session** — a slow network
   fetch freezes the entire server. The session streamer and sweepers make
   similar sync tmux calls. Wrap all subprocess work in `asyncio.to_thread`
   (or `asyncio.create_subprocess_exec`) and do git setup *before* opening the
   DB transaction, persisting results afterwards.
2. **Extract a service layer.** REST routers and the 2,000-line
   `KanbanMCPServer` are two parallel implementations of the same business
   logic (move task, assign, approve, comment, sessions...). Create
   `agent_kanban_pm/services/` (tasks, projects, sessions, approvals) used by
   both routers and MCP handlers. Split `KanbanMCPServer.list_tools()`'s
   ~500-line inline schema list into per-tool modules. Break
   `launch_for_assignment()` (~270 lines) into: resolve → gate → prepare
   worktree → persist session → spawn.
3. **Adopt Alembic.** The homegrown `schema_migrations` table in `database.py`
   works but reimplements Alembic poorly and mixes `create_all()` with manual
   DDL. Generate an initial Alembic baseline from current models, port
   migrations v1–v9, and run `alembic upgrade head` from `init_db()`.
4. **MCP identity freshness (completed).** `KanbanMCPServer._authenticate()` caches the
   entity object for the life of the process; re-fetch (or at least re-check
   `is_active`/role) per call so demoting an agent takes effect.
5. **Kill remaining hardcoded endpoints (completed).** `open_project.py` pins
   `API_BASE = "http://localhost:8000"`, ignoring the instance port logic —
   route it through `kanban_runtime.instance.get_api_base()` (or fold it into
   `kanban open` as a CLI subcommand and delete the standalone script).

6. **Cleanly close async database resources in tests and shutdown (completed).**
   The suite now drains queued event work before stopping, disposes the
   application engine during lifespan shutdown, and closes test resources
   before their event loops end. Unhandled pytest worker-thread warnings fail CI.
7. **Make assignment admission atomic (completed for the supported single-server topology).**
   Parallel assignment events pass through a process-local admission lock,
   while partial unique database indexes enforce one open session and one active
   lease for an assignment. Conflicts are handled without spawning a duplicate
   worker.

## Phase 4 — Product surface & docs

1. **README as a landing page (partially completed):** the install quickstart
   and support matrix work; a demo GIF/screenshot of the board and a terminal
   session remain.
2. **Community scaffolding (completed):** CONTRIBUTING.md, issue/PR templates,
   CODE_OF_CONDUCT.md, SECURITY.md (local-first threat model and how to report).
3. **Docs site** (mkdocs-material): concepts (roles, stages, handoff,
   STATUS.md contract), adapter YAML reference, preferences.yaml reference,
   MCP tool reference (generate from tool schemas), troubleshooting.
4. **Rename test files** from `test_phase1/2/6.py` to behavior-named modules;
   add coverage reporting to CI.
5. **Frontend debt (optional, later):** `kanban_board.html` is a 2,164-line
   template with inline JS. Short-term: extract the JS into
   `static/js/board.js` modules. Long-term: consider htmx or a small Vite
   bundle — do not block release on this.

## Phase 5 — Release & distribution

1. **PyPI publishing (automation complete; account setup pending).** The tag-triggered `release.yml` workflow builds and checks artifacts, publishes through PyPI Trusted Publishing, verifies `pipx` and `uvx`, and creates the GitHub Release. Configure the `pypi` GitHub environment and pending PyPI publisher before pushing the first tag.
2. **Versioning discipline (automation ready):** the changelog now has a dated
   `0.4.0rc1` section and the workflow rejects a tag that differs from the
   package version. A maintainer must push `v0.4.0rc1`; GitHub Release notes
   and artifacts are then generated automatically.
3. **Alternative install paths (verified):** `uvx` and `pipx` are exercised
   by the release workflow; an optional Dockerfile (server-only mode, no tmux)
   remains future work.
4. **Post-release checks (completed):** a scheduled CI job installs from PyPI
   and boots the server so distribution rot is caught automatically.

---

## Suggested sequencing

| Order | Work | Size |
|-------|------|------|
| 1 | Phase 0 (completed: stabilize, CI installs package) | Done |
| 2 | Phase 1 (src layout, `mcp` dep, data home) | 2–3 days, one big PR |
| 3 | Phase 5.1 (claim PyPI name, publish first alpha) | hours |
| 4 | Phase 2 (auth/CSRF/token/permissions defaults) | 2 days |
| 5 | Phase 3 (async fixes, service layer, Alembic) | 1–2 weeks, incremental |
| 6 | Phase 4 (docs/community) | parallel, ongoing |

Publishing an alpha immediately after Phase 1 is deliberate: "available" is
the goal, and early installers will surface packaging bugs faster than CI.
