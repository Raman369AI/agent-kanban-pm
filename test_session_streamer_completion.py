from kanban_runtime.session_streamer import _completion_summary


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

    summary = _completion_summary(pane)

    assert summary is not None
    assert summary.startswith("Here are the files")
    assert "agent_reactor.py" in summary
