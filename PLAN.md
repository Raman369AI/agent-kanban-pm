# Plan: Make Agent Kanban PM a Standalone, Fully Available Project

Goal: a project anyone can discover, `pip install`, run in under five minutes,
and trust enough to point at their own repositories. Current state is a working
alpha that only runs from a source checkout. This plan is ordered so each phase
is releasable on its own.

---

## Phase 0 — Stabilize what exists (small, do first)

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

## Phase 1 — Packaging correctness (blocks everything else)

1. **Adopt a single-package src layout.** Today `pip install` drops `main`,
   `auth`, `database`, `models`, `schemas`, `adapters`, `event_bus`,
   `websocket_manager`, `open_project`, `mcp_server`, and a generic `routers`
   package into top-level site-packages — near-guaranteed collisions with any
   other installed package. Restructure:

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

## Phase 2 — Security hardening (required before advertising it)

1. **Close the `/ui` auth bypass.** `token_auth_middleware` exempts every path
   starting with `/ui`, but `routers/ui.py` has ~10 mutation endpoints
   (`POST /ui/tasks/create`, `DELETE /ui/tasks/{id}`, `POST /ui/api/open-workspace`,
   ...). Today the only thing stopping a malicious webpage is that browsers
   won't attach `X-Entity-ID` cross-origin — and DNS-rebinding defeats
   origin-based protection because the Host header is never validated. Fix:
   - require the token (cookie or header) on **all** non-GET requests,
     including `/ui/*`;
   - set the cookie `httponly=True` and have UI JS use a same-origin
     `fetch` that relies on the cookie plus a CSRF token embedded in the page;
   - validate `Host` is `localhost`/`127.0.0.1` (TrustedHostMiddleware).
2. **Protect the token file.** `get_auth_token()` writes `~/.kanban/token`
   with default permissions; `chmod 600` it (`token_file.touch(mode=0o600)`).
3. **Make auto-approval an explicit choice.** Bundled adapters default to
   `--permission-mode bypassPermissions` / `--approval-mode yolo` /
   `--full-auto`. For a public product the default should be safe: add a
   per-role `autonomy: supervised|auto` knob in `preferences.yaml`, default
   `supervised`, and have `kanban init` ask once, loudly, before enabling
   bypass modes. Keep worktree isolation as the second layer, not the only one.
4. **WebSocket auth.** `/ws` is exempted from the token middleware; require the
   token as a query param or first message before subscribing.

## Phase 3 — Runtime correctness

1. **Stop blocking the event loop.** `launch_for_assignment()` runs
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
   migrations v1–v7, and run `alembic upgrade head` from `init_db()`.
4. **MCP identity freshness.** `KanbanMCPServer._authenticate()` caches the
   entity object for the life of the process; re-fetch (or at least re-check
   `is_active`/role) per call so demoting an agent takes effect.
5. **Kill remaining hardcoded endpoints.** `open_project.py` pins
   `API_BASE = "http://localhost:8000"`, ignoring the instance port logic —
   route it through `kanban_runtime.instance.get_api_base()` (or fold it into
   `kanban open` as a CLI subcommand and delete the standalone script).

## Phase 4 — Product surface & docs

1. **README as a landing page:** demo GIF/screenshot of the board and a
   terminal session, a 5-minute quickstart that works verbatim
   (`pipx install agent-kanban-pm && kanban init && kanban run`), a support
   matrix (OS, Python, agent CLIs tested).
2. **Community scaffolding:** CONTRIBUTING.md, issue/PR templates,
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

1. **PyPI publishing.** The README already says `pip install --pre
   agent-kanban-pm`, but the name is unregistered (pypi returns 404) — claim
   it now. Add a `release.yml` workflow using PyPI Trusted Publishing
   triggered on `v*` tags: build → twine check → publish; smoke-test the
   published wheel with `pipx run`.
2. **Versioning discipline.** Tag `v0.3.0a2` from the restructured package,
   move the giant `[Unreleased]` CHANGELOG section under it, and cut releases
   per phase. GitHub Releases with notes generated from CHANGELOG.
3. **Alternative install paths:** verify `uvx agent-kanban-pm` / `pipx`
   work; optional Dockerfile (server-only mode, no tmux) for the board UI.
4. **Post-release checks:** a scheduled CI job that installs from PyPI and
   boots the server, so distribution rot is caught automatically.

---

## Suggested sequencing

| Order | Work | Size |
|-------|------|------|
| 1 | Phase 0 (stabilize, CI installs package) | ~1 day |
| 2 | Phase 1 (src layout, `mcp` dep, data home) | 2–3 days, one big PR |
| 3 | Phase 5.1 (claim PyPI name, publish first alpha) | hours |
| 4 | Phase 2 (auth/CSRF/token/permissions defaults) | 2 days |
| 5 | Phase 3 (async fixes, service layer, Alembic) | 1–2 weeks, incremental |
| 6 | Phase 4 (docs/community) | parallel, ongoing |

Publishing an alpha immediately after Phase 1 is deliberate: "available" is
the goal, and early installers will surface packaging bugs faster than CI.
