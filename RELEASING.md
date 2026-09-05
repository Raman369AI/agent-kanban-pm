# Release Process

## One-time setup

1. Create the `agent-kanban-pm` project on PyPI, or configure a pending
   Trusted Publisher for the first publication.
2. Configure the PyPI publisher with owner `Raman369AI`, repository
   `agent-kanban-pm`, workflow `release.yml`, and environment `pypi`.
3. Create a protected GitHub environment named `pypi`; require approval if
   desired.
4. Keep tag creation restricted to maintainers.

The publishing job alone receives `id-token: write`. Build artifacts cross
the job boundary through GitHub's artifact service and PyPI attestations are
enabled by the official publishing action.

## Every release

1. Move shipped entries from `[Unreleased]` into a versioned changelog
   section and set the release date.
2. Set `__version__` in
   `src/agent_kanban_pm/runtime/_version.py`; prereleases use PEP 440 forms
   such as `0.4.0rc1`.
3. Run:

   ```bash
   uv lock --check
   uv run --locked --extra dev pytest --timeout=60 --timeout-method=thread
   uv run --locked --extra dev flake8 src tests --count --select=E9,F63,F7,F82
   uv run --locked --extra dev python -m build
   uv run --locked --extra dev twine check dist/*
   ```

4. Merge the release commit to `main`.
5. Create and push an annotated tag matching the package version exactly:
   `v0.4.0rc1`, for example.
6. The Release workflow validates the tag, builds and checks the artifacts,
   publishes through PyPI Trusted Publishing, verifies both `pipx` and
   `uvx`, and creates the GitHub Release.
7. Confirm the PyPI metadata, console entry points, release artifacts, and
   attestations.
