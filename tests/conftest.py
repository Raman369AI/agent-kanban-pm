"""Pytest entry point — wires up the throwaway-entity cleanup fixture.

Importing `tests_helper` registers the SQLAlchemy listeners and exposes a
session-scoped autouse fixture (`_kanban_throwaway_cleanup`) that deletes any
rows created during the test session. Tests run as plain scripts must still
`import tests_helper` themselves.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root
sys.path.insert(0, str(Path(__file__).resolve().parent))          # tests dir

import tests_helper  # noqa: F401  — side-effect: install listeners & fixture