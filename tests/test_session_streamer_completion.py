import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kanban_runtime.session_streamer import _terminal_completion_summary, _check_completion


def test_completion_summary_detects_file_list_handoff_at_shell_prompt():
    pane = """
# Todos
[x] Explore workspace file structure
[x] Compare files against AGENTS.md documented structure
[x] List files unrelated to existing structure

Here are the files **unrelated to the existing structure** as documented in AGENTS.md:

| File | Notes |
|---|---|
| `agent_reactor.py` | AGENTS.md: "deleted" |

kronos@host:~/worktree$
"""

    # Test the legacy terminal heuristic directly
    summary = _terminal_completion_summary(pane)
    assert summary is not None
    assert summary.startswith("Here are the files")
    assert "agent_reactor.py" in summary

    # Test the full two-tier check (falls back to heuristic with no workspace)
    summary2 = _check_completion(pane, workspace_path=None)
    assert summary2 is not None
    assert "agent_reactor.py" in summary2
