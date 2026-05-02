"""Runtime paths for source and installed package layouts."""

from __future__ import annotations

import importlib.resources
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "agent-kanban-pm"


def _resource_path_via_importlib(subdir: str) -> Path | None:
    try:
        ref = importlib.resources.files("kanban_runtime.data")
        base = Path(str(ref))
        candidate = base / subdir
        if candidate.is_dir():
            return candidate
    except (ModuleNotFoundError, TypeError, FileNotFoundError):
        pass
    return None


def _candidate_roots() -> list[Path]:
    roots = []
    env_root = os.getenv("KANBAN_PACKAGE_ROOT")
    if env_root:
        roots.append(Path(env_root).expanduser())
    roots.extend([
        Path.cwd(),
        PROJECT_ROOT,
        PROJECT_ROOT / "share" / APP_NAME,
        Path(sys.prefix) / "share" / APP_NAME,
        Path(sys.base_prefix) / "share" / APP_NAME,
    ])
    seen = set()
    unique = []
    for root in roots:
        resolved = root.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def resource_dir(name: str) -> Path:
    importlib_path = _resource_path_via_importlib(name)
    if importlib_path is not None:
        return importlib_path
    for root in _candidate_roots():
        candidate = root / name
        if candidate.exists():
            return candidate
    return PROJECT_ROOT / name


def templates_dir() -> Path:
    return resource_dir("templates")


def static_dir() -> Path:
    return resource_dir("static")


def bundled_adapters_dir() -> Path:
    return resource_dir("agents")


def mcp_configs_dir() -> Path:
    return resource_dir("mcp_configs")