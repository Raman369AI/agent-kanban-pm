"""Pytest entry point — wires up the throwaway-entity cleanup fixture.

Importing `tests_helper` registers the SQLAlchemy listeners and exposes a
session-scoped autouse fixture (`_kanban_throwaway_cleanup`) that deletes any
rows created during the test session. Tests run as plain scripts must still
`import tests_helper` themselves.
"""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests dir

# Keep pytest runs off the developer's real ./kanban.db. This must happen
# before any test module imports database.py and creates the global engine.
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="agent-kanban-pm-tests-")
_TEST_DB_PATH = Path(_TEST_DB_DIR.name) / "kanban.db"
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_PATH}")
os.environ["KANBAN_TESTING"] = "1"

# Redirect HOME to a throwaway directory so adapter sync neither reads the
# developer's real ~/.kanban/preferences.yaml nor pollutes ~/.kanban/agents
# with copied bundled YAMLs. PREFERENCES_PATH and USER_ADAPTERS_DIR capture
# Path.home() at import time, so HOME must be set before any kanban_runtime
# module is imported.
_TEST_HOME_DIR = tempfile.TemporaryDirectory(prefix="agent-kanban-pm-home-")
os.environ["HOME"] = _TEST_HOME_DIR.name

# Register every bundled adapter as an Entity. Production only registers
# *role-assigned* agents (driven by preferences.yaml), so a clean checkout with
# no preferences registers nothing and test_integration / test_phase2 fail for
# lack of claude/gemini/opencode entities. We deliberately do NOT seed a
# preferences.yaml here: a configured manager would make the app auto-assign
# tasks to agents and try to spawn them in tmux during tests (which hangs on
# CI). Registering entities without a manager keeps load_preferences() empty,
# so the agent-launching machinery stays inert.
os.environ["KANBAN_REGISTER_ALL_ADAPTERS"] = "1"

# An adapter Entity is only marked is_active when its CLI is found on PATH
# (shutil.which), and GET /entities hides inactive agents. CI installs none of
# the agent CLIs, so without this every adapter is inactive and invisible,
# failing test_integration / test_phase2. Drop tiny stub executables for the
# adapters those tests assert on so they resolve as "installed". The stubs are
# prepended to PATH (shadowing any real CLI) and are inert no-ops; nothing in
# the suite triggers an agent launch, so they are never actually executed.
_TEST_BIN_DIR = tempfile.TemporaryDirectory(prefix="agent-kanban-pm-bin-")
for _cmd in ("claude", "gemini", "opencode"):
    _stub = Path(_TEST_BIN_DIR.name) / _cmd
    _stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    _stub.chmod(0o755)
os.environ["PATH"] = _TEST_BIN_DIR.name + os.pathsep + os.environ.get("PATH", "")

import tests_helper  # noqa: F401  — side-effect: install listeners & fixture



def pytest_sessionfinish(session, exitstatus):
    import asyncio
    import tests_helper
    from agent_kanban_pm.db import engine

    tests_helper.cleanup_now()
    asyncio.run(engine.dispose())
    _TEST_DB_DIR.cleanup()
    _TEST_HOME_DIR.cleanup()
    _TEST_BIN_DIR.cleanup()
