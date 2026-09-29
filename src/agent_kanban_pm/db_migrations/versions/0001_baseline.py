"""Baseline: schema as of the last legacy migration (schema_migrations v20).

Databases that predate Alembic are brought to this schema by the frozen chain
in ``db_migrations/legacy.py`` and then stamped here, so this revision does
nothing. New databases are built with ``create_all`` and stamped at head, so
revisions are never replayed from here on an empty file.

Revision ID: 0001
Revises:
"""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise NotImplementedError("The baseline cannot be downgraded.")
