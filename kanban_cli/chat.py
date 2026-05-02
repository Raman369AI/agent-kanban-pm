"""
Kanban CLI chat — interactive REPL backed by the orchestrator adapter.

Workflow:
  1. Resolve project + orchestrator role.
  2. Loop: read user turn, call ChatDesigner, render plan.
  3. User picks: (c)ommit / (r)efine / (e)dit n / (d)rop n / (q)uit.
  4. Commit POSTs items[] to /ui/tasks/chat-plan.
  5. Quit saves a draft under ~/.kanban/chat/drafts/.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

import yaml

from kanban_cli.chat_designer import (
    ChatDesigner,
    DesignerError,
    DesignerResult,
    PlanTask,
    PlanV1,
)


DRAFTS_DIR = Path.home() / ".kanban" / "chat" / "drafts"


def _resolve_api_base(fallback: Optional[str] = None) -> str:
    env = os.environ.get("KANBAN_API_BASE")
    if env:
        return env
    try:
        from kanban_runtime.instance import get_api_base
        return get_api_base()
    except Exception:
        return fallback or "http://localhost:8000"


DEFAULT_API_BASE = _resolve_api_base()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _api_request(
    api_base: str,
    method: str,
    path: str,
    *,
    body: Optional[dict] = None,
    entity_id: Optional[int] = None,
    timeout: int = 15,
) -> tuple[int, Optional[dict], str]:
    url = f"{api_base.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if entity_id is not None:
        req.add_header("X-Entity-ID", str(entity_id))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, (json.loads(payload) if payload else None), ""
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8")
        except Exception:
            pass
        return exc.code, None, body_text
    except (urllib.error.URLError, OSError) as exc:
        return 0, None, f"network error: {exc}"


def _fetch_project(api_base: str, project_id: int, entity_id: Optional[int]) -> dict:
    code, payload, err = _api_request(
        api_base, "GET", f"/projects/{project_id}", entity_id=entity_id
    )
    if code == 200 and payload:
        return payload
    if code == 0:
        raise SystemExit(
            f"Cannot reach Kanban server at {api_base}. "
            f"Start it with: python -m kanban_cli run\n  ({err})"
        )
    if code == 404:
        raise SystemExit(f"Project {project_id} not found.")
    raise SystemExit(f"Failed to fetch project {project_id}: HTTP {code} {err[:200]}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _wrap(text: str, width: int = 78, indent: str = "    ") -> str:
    out: List[str] = []
    for line in (text or "").splitlines() or [""]:
        if not line.strip():
            out.append("")
            continue
        out.extend(textwrap.wrap(line, width=width, initial_indent=indent, subsequent_indent=indent))
    return "\n".join(out)


def render_plan(plan: PlanV1) -> str:
    lines: List[str] = []
    lines.append("Plan")
    if plan.summary:
        lines.append(f"  Summary: {plan.summary}")
    if plan.questions:
        lines.append("  Questions:")
        for i, q in enumerate(plan.questions, 1):
            lines.append(f"    {i}. {q}")
    if plan.tasks:
        lines.append(f"\n  Tasks ({len(plan.tasks)}):")
        for idx, t in enumerate(plan.tasks):
            role = t.role_hint or "—"
            dep = f"  (depends on {', '.join(str(d + 1) for d in t.depends_on)})" if t.depends_on else ""
            lines.append(f"    {idx + 1:>2}. [{t.priority}] {role:<14} {t.title}{dep}")
            if t.description.strip():
                lines.append(_wrap(t.description, width=72, indent="        "))
            if t.acceptance:
                lines.append("        Acceptance:")
                for a in t.acceptance:
                    lines.append(f"          - {a}")
    else:
        lines.append("  (no tasks)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Edit / drop
# ---------------------------------------------------------------------------

def _editor_cmd() -> List[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ""
    if editor:
        return editor.split()
    for candidate in ("nano", "vi", "vim"):
        if shutil.which(candidate):
            return [candidate]
    raise DesignerError("no editor available; set $EDITOR")


def edit_task(task: PlanTask) -> PlanTask:
    """Open $EDITOR on a YAML rendering of the task; reapply changes."""
    payload = {
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "role_hint": task.role_hint,
        "acceptance": list(task.acceptance),
    }
    with tempfile.NamedTemporaryFile("w+", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
        tmp_path = fh.name
    try:
        cmd = _editor_cmd() + [tmp_path]
        subprocess.run(cmd, check=False)
        with open(tmp_path, "r", encoding="utf-8") as fh:
            updated = yaml.safe_load(fh) or {}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    merged = {
        "title": (updated.get("title") or task.title),
        "description": updated.get("description", task.description) or "",
        "priority": updated.get("priority", task.priority),
        "role_hint": updated.get("role_hint", task.role_hint),
        "acceptance": list(updated.get("acceptance", task.acceptance) or []),
        "depends_on": list(task.depends_on or []),
    }
    return PlanTask(**merged)


def drop_task(plan: PlanV1, drop_idx: int) -> PlanV1:
    """Remove plan.tasks[drop_idx] and rewire depends_on indices."""
    if drop_idx < 0 or drop_idx >= len(plan.tasks):
        raise ValueError(f"invalid task index {drop_idx + 1}")
    new_tasks: List[PlanTask] = []
    for i, t in enumerate(plan.tasks):
        if i == drop_idx:
            continue
        rewired: List[int] = []
        for d in t.depends_on:
            if d == drop_idx:
                continue
            rewired.append(d - 1 if d > drop_idx else d)
        new_tasks.append(t.model_copy(update={"depends_on": rewired}))
    return plan.model_copy(update={"tasks": new_tasks})


# ---------------------------------------------------------------------------
# Drafts
# ---------------------------------------------------------------------------

def save_draft(project_id: int, plan: PlanV1, history: List[dict]) -> Path:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = DRAFTS_DIR / f"project-{project_id}-{ts}.json"
    payload = {
        "project_id": project_id,
        "saved_at": ts,
        "plan": plan.model_dump(),
        "history": history,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def commit_plan(
    api_base: str,
    project_id: int,
    plan: PlanV1,
    transcript: str,
    entity_id: Optional[int],
) -> dict:
    body = {
        "project_id": project_id,
        "message": (plan.summary or transcript[-1000:] or "Chat designer plan").strip(),
        "items": [
            {
                "title": t.title,
                "description": t.description,
                "priority": t.priority,
                "role_hint": t.role_hint,
                "acceptance": list(t.acceptance),
                "depends_on": list(t.depends_on),
            }
            for t in plan.tasks
        ],
        "transcript": transcript,
    }
    code, payload, err = _api_request(
        api_base, "POST", "/ui/tasks/chat-plan",
        body=body, entity_id=entity_id, timeout=30,
    )
    if code == 200 and payload:
        return payload
    if code == 0:
        raise SystemExit(f"Cannot reach server at {api_base}: {err}")
    raise SystemExit(f"Commit failed: HTTP {code} {err[:500]}")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

INDEX_RE = re.compile(r"\s*(\d+)\s*$")


def _parse_index_arg(arg: str, total: int) -> int:
    m = INDEX_RE.match(arg or "")
    if not m:
        raise ValueError("expected a task number")
    n = int(m.group(1))
    if n < 1 or n > total:
        raise ValueError(f"task number must be 1..{total}")
    return n - 1


def _format_transcript(history: List[dict]) -> str:
    out = []
    for entry in history:
        role = entry.get("role", "user").upper()
        content = (entry.get("content") or "").strip()
        if content:
            out.append(f"{role}: {content}")
    return "\n\n".join(out)


def run_chat(project_id: int, *, api_base: str, entity_id: Optional[int]) -> int:
    project = _fetch_project(api_base, project_id, entity_id)
    project_name = project.get("name") or f"project {project_id}"
    project_path = project.get("path") or "(no local path)"

    try:
        designer = ChatDesigner.from_preferences(api_base=api_base)
    except DesignerError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    print("=" * 64)
    print(f"Project: {project_name}  (id {project_id})")
    print(f"Path:    {project_path}")
    print(f"Orchestrator: {designer.invocation.display_name} "
          f"({designer.role_assignment.model or 'default'}, "
          f"{designer.role_assignment.mode})")
    print("=" * 64)
    print('Type your request. Empty line submits. Ctrl-D or "q" to quit.\n')

    history: List[dict] = []
    plan: Optional[PlanV1] = None

    def _read_multiline(prompt: str) -> Optional[str]:
        print(prompt, end="", flush=True)
        lines: List[str] = []
        try:
            while True:
                line = input()
                if line == "" and lines:
                    break
                if line == "" and not lines:
                    continue
                lines.append(line)
        except EOFError:
            return None
        return "\n".join(lines).strip() or None

    while True:
        user_turn = _read_multiline("You> ")
        if user_turn is None:
            print("\n(no input — exiting)")
            return 0
        if user_turn.lower() in ("q", "quit", "exit"):
            return 0

        print(f"\n[{designer.invocation.display_name} is thinking…]", flush=True)
        t0 = time.time()
        try:
            result: DesignerResult = designer.design(history, user_turn)
        except DesignerError as exc:
            print(f"\n[designer error] {exc}\n", file=sys.stderr)
            history.append({"role": "user", "content": user_turn})
            continue
        elapsed = time.time() - t0
        plan = result.plan
        history.append({"role": "user", "content": user_turn})
        history.append({"role": "assistant", "content": result.raw_output})

        print(f"[done in {elapsed:.1f}s]\n")
        print(render_plan(plan))
        print()

        # action loop
        while plan is not None:
            try:
                choice = input("(c)ommit  (r)efine  (e)dit n  (d)rop n  (q)uit > ").strip()
            except EOFError:
                choice = "q"
            if not choice:
                continue
            head, _, tail = choice.partition(" ")
            head = head.lower()

            if head in ("c", "commit"):
                if not plan.tasks:
                    print("Nothing to commit — plan has no tasks.")
                    continue
                if plan.questions:
                    print(
                        "There are unanswered questions. Type 'r' to refine, "
                        "or type 'force' to commit anyway."
                    )
                    confirm = input("> ").strip().lower()
                    if confirm != "force":
                        continue
                transcript = _format_transcript(history)
                response = commit_plan(api_base, project_id, plan, transcript, entity_id)
                created = response.get("tasks", [])
                print(f"\nCreated {len(created)} backlog cards:")
                for t in created:
                    print(f"  #{t['id']}  [{t['priority']}]  {t['title']}")
                if response.get("agents_path"):
                    print(f"Plan appended to: {response['agents_path']}")
                return 0

            if head in ("r", "refine"):
                break  # back to outer loop, ask for a new turn

            if head in ("q", "quit", "exit"):
                if plan and plan.tasks:
                    path = save_draft(project_id, plan, history)
                    print(f"Draft saved to {path}")
                return 0

            if head in ("e", "edit"):
                try:
                    idx = _parse_index_arg(tail, len(plan.tasks))
                except ValueError as exc:
                    print(f"  {exc}")
                    continue
                try:
                    plan.tasks[idx] = edit_task(plan.tasks[idx])
                except DesignerError as exc:
                    print(f"  {exc}")
                    continue
                print(render_plan(plan))
                continue

            if head in ("d", "drop"):
                try:
                    idx = _parse_index_arg(tail, len(plan.tasks))
                except ValueError as exc:
                    print(f"  {exc}")
                    continue
                plan = drop_task(plan, idx)
                print(render_plan(plan))
                continue

            print("  unknown command — try c / r / e n / d n / q")


def cmd_chat(args) -> None:
    api_base = args.api_base or DEFAULT_API_BASE
    entity_id = args.entity_id
    if entity_id is None and os.environ.get("KANBAN_ENTITY_ID"):
        try:
            entity_id = int(os.environ["KANBAN_ENTITY_ID"])
        except ValueError:
            entity_id = None
    rc = run_chat(args.project_id, api_base=api_base, entity_id=entity_id)
    sys.exit(rc)
