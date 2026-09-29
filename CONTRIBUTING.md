# Contributing

Thanks for helping improve Agent Kanban PM.

1. Open an issue for substantial behavioral or schema changes.
2. Create a focused branch from `main`.
3. Install development dependencies with `pip install -e ".[dev]"`.
4. Run `pytest --timeout=60 --timeout-method=thread`.
5. Run `flake8 src tests --count --select=E9,F63,F7,F82 --show-source --statistics`.
6. Build with `python -m build` and validate with `twine check dist/*` for
   packaging or release-related changes.
7. Open a pull request explaining behavior, tests, compatibility, and migration
   impact.

Preserve the local-first, single-operator security boundary. New subprocess
work must not block the asyncio event loop or run while a database transaction
is held. Schema changes must include an idempotent upgrade test from an older
database.


## Changing the database schema

The schema is versioned with Alembic. A model change needs a revision:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Review the generated file in `src/agent_kanban_pm/db_migrations/versions/`
(SQLite needs `op.batch_alter_table` for most alterations; batch mode is on by
default), then run the tests. `tests/test_db_upgrade.py::test_models_match_migrated_schema`
fails if the models and the migrated schema disagree. New databases are built
from the models and stamped at head, so a revision only has to upgrade from the
previous one. Do not edit `db_migrations/legacy.py`; it is the frozen
pre-Alembic chain.
