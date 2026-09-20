# Marketing site

This directory contains the static Agent Kanban PM marketing site deployed by
GitHub Pages. It intentionally has no build step and no dependency on the
FastAPI application.

Preview it locally from the repository root:

```bash
python -m http.server 8080 --directory site
```

Then open `http://localhost:8080`.

The site describes the supported local, single-operator product. Keep feature
claims aligned with `README.md` and `ARCHITECTURE.md` when updating copy.
