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
