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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests dir

# Keep pytest runs off the developer's real ./kanban.db. This must happen
# before any test module imports database.py and creates the global engine.
_TEST_DB_DIR = tempfile.TemporaryDirectory(prefix="agent-kanban-pm-tests-")
_TEST_DB_PATH = Path(_TEST_DB_DIR.name) / "kanban.db"
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TEST_DB_PATH}")

import tests_helper  # noqa: F401  — side-effect: install listeners & fixture


def pytest_sessionfinish(session, exitstatus):
    import asyncio
    import tests_helper
    from database import engine

    tests_helper.cleanup_now()
    asyncio.run(engine.dispose())
    _TEST_DB_DIR.cleanup()
