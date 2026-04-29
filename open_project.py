#!/usr/bin/env python3
"""
Open Folder as Project

Register the current working directory (or a given path) as a Kanban project.
If a project already exists at that path, return it. Otherwise create one.

Usage:
    python open_project.py                    # Register current directory
    python open_project.py /path/to/folder    # Register specific folder
    python open_project.py --list             # List all projects with paths
"""

import argparse
import os
import sys

import httpx

API_BASE = "http://localhost:8000"


def list_projects():
    resp = httpx.get(f"{API_BASE}/projects", timeout=10)
    resp.raise_for_status()
    projects = resp.json()
    print(f"{'ID':>3} | {'Name':<25} | Path")
    print("-" * 70)
    for p in projects:
        path = p.get("path") or "(no path)"
        print(f"{p['id']:>3} | {p['name']:<25} | {path}")
    return projects


def get_or_create_project(path: str):
    abs_path = os.path.abspath(path)
    name = os.path.basename(abs_path)

    # Check if project already exists at this path
    projects = list_projects()
    for p in projects:
        if p.get("path") == abs_path:
            print(f"\n✅ Project already exists: #{p['id']} {p['name']} -> {abs_path}")
            return p

    # Create new project
    resp = httpx.post(
        f"{API_BASE}/projects",
        json={"name": name, "description": f"Project opened from {abs_path}", "path": abs_path},
        timeout=10,
    )
    resp.raise_for_status()
    project = resp.json()
    print(f"\n✅ Created project: #{project['id']} {project['name']} -> {abs_path}")
    return project


def main():
    parser = argparse.ArgumentParser(description="Open a folder as a Kanban project")
    parser.add_argument("path", nargs="?", default=".", help="Folder path to open (default: current directory)")
    parser.add_argument("--list", action="store_true", help="List all projects")
    args = parser.parse_args()

    try:
        if args.list:
            list_projects()
        else:
            get_or_create_project(args.path)
    except httpx.ConnectError:
        print("❌ Error: Cannot connect to API server at", API_BASE)
        print("   Make sure the server is running: python main.py")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
