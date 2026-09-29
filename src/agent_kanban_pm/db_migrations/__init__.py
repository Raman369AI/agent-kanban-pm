"""Alembic integration: schema versioning for the local SQLite database.

Three database states are handled by ``db.init_db``:

* ``empty``    - no tables. Built from the models with ``create_all`` and
  stamped at head. Revisions are never replayed on a new database, so a
  revision may assume the schema it upgrades from.
* ``legacy``   - tables exist but there is no ``alembic_version`` row. The
  frozen pre-Alembic chain (``legacy.py``) runs, then the database is stamped
  with ``BASELINE_REVISION`` and upgraded to head.
* ``managed``  - already under Alembic. Upgraded to head.

Every schema change from now on is a revision in ``versions/``.
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

BASELINE_REVISION = "0001"
SCRIPT_LOCATION = Path(__file__).resolve().parent


def make_config(connection=None) -> Config:
    """Build an Alembic config that needs no ini file (works from a wheel)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def detect_state(connection) -> str:
    """Return ``empty``, ``legacy`` or ``managed`` for a sync connection."""
    names = set(inspect(connection).get_table_names())
    if "alembic_version" in names:
        if connection.execute(text("SELECT 1 FROM alembic_version LIMIT 1")).first():
            return "managed"
        names.discard("alembic_version")
    names.discard("sqlite_sequence")
    return "legacy" if names else "empty"


def stamp(connection, revision: str = "head") -> None:
    command.stamp(make_config(connection), revision)


def upgrade(connection, revision: str = "head") -> None:
    command.upgrade(make_config(connection), revision)
