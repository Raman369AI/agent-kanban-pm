from types import SimpleNamespace

from agent_kanban_pm.runtime.handoff_protocol import (
    STATUS_TEMPLATE,
    available_handoff_agents,
    build_handoff_instructions,
    ensure_instruction_aliases,
    initialize_status_file,
    parse_status_frontmatter,
    parse_status_state,
    profile_for_agent,
    read_status_file,
    status_path_for_workspace,
    status_matches_session,
)
from agent_kanban_pm.runtime.preferences import Preferences, RoleAssignment, RoleConfig


def test_default_profiles_match_multi_agent_protocol():
    """profile_for_agent resolves from adapter YAML first, then
    falls back to DEFAULT_AGENT_PROFILES for agents without YAML."""
    claude = profile_for_agent("claude")
    opencode = profile_for_agent("opencode")
    codex = profile_for_agent("codex")
    unknown = profile_for_agent("totally-unknown-agent")

    # All known agents resolve to canonical names
    assert claude.agent == "claude"
    assert opencode.agent == "opencode"
    assert codex.agent == "codex"

    # All profiles have a non-empty role string
    assert claude.role
    assert opencode.role
    assert codex.role

    # Unknown agents get a generic fallback
    assert unknown.agent == "totally-unknown-agent"
    assert unknown.role == "implementation"
    assert unknown.owns == ()
    assert unknown.review_only is False


def test_status_template_and_parser_use_assigned_state():
    assert parse_status_state(STATUS_TEMPLATE) == "assigned"
    assert parse_status_frontmatter(STATUS_TEMPLATE)["handoff_ready"] is False
    assert parse_status_state("not frontmatter") is None
    assert parse_status_state("---\nstate: blocked\n---\n") == "blocked"


def test_instruction_aliases_point_to_agents_md_without_overwriting_real_files(tmp_path):
    workspace = tmp_path / "task-worktree"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    (workspace / "CODEX.md").write_text("custom codex rules\n", encoding="utf-8")

    results = ensure_instruction_aliases(workspace)

    assert results["CLAUDE.md"] == "linked to AGENTS.md"
    assert (workspace / "CLAUDE.md").is_symlink()
    assert (workspace / "CLAUDE.md").readlink().as_posix() == "AGENTS.md"
    assert results["CODEX.md"] == "kept existing real file"
    assert (workspace / "CODEX.md").read_text(encoding="utf-8") == "custom codex rules\n"
    assert "GEMINI.md" not in results


def test_initialize_status_file_writes_worktree_task_handoff(tmp_path):
    workspace = tmp_path / "task-worktree"
    workspace.mkdir()

    path = initialize_status_file(
        workspace,
        task_id=12,
        project_id=3,
        current_agent="opencode",
        assigned_role="ui",
        task_title="Build board",
    )

    assert path == workspace / "STATUS.md"
    info = read_status_file(workspace)
    assert info["exists"] is True
    assert info["state"] == "assigned"
    assert info["handoff_ready"] is False
    assert info["frontmatter"]["task_id"] == 12
    assert info["frontmatter"]["project_id"] == 3
    assert info["frontmatter"]["current_agent"] == "opencode"
    assert info["frontmatter"]["assigned_role"] == "ui"


def test_handoff_instructions_are_self_contained(tmp_path):
    workspace = tmp_path / "codex"
    workspace.mkdir()
    text = build_handoff_instructions("codex", workspace)

    assert "Read AGENTS.md for instructions only" in text
    assert "CLAUDE.md and CODEX.md should be symlinks to AGENTS.md" in text
    # Text contains either review-only instruction or owned-paths instruction
    assert ("review-only" in text) or ("owned paths" in text)
    assert str(workspace / "STATUS.md") in text
    assert "do not write sibling worktree STATUS.md files" in text
    assert "handoff_ready: true" in text


def test_available_handoff_agents_includes_active_team_and_defaults(monkeypatch):
    prefs = Preferences(
        roles=RoleConfig(
            orchestrator=RoleAssignment(agent="claude", mode="headless"),
            worker=RoleAssignment(agent="custom-cli", command="custom-cli", mode="headless"),
        )
    )

    monkeypatch.setattr(
        "agent_kanban_pm.runtime.handoff_protocol.discover_popular_clis",
        lambda: [],
    )

    agents = available_handoff_agents(prefs=prefs, adapters=[])
    assert "claude" in agents
    assert "codex" in agents
    assert "custom-cli" in agents


def test_reassigning_shared_workspace_resets_stale_completion_and_preserves_notes(tmp_path):
    initialize_status_file(
        tmp_path, task_id=1, project_id=7, session_id=11, run_token="first",
        current_agent="agent-a", assigned_role="worker",
    )
    status_path = tmp_path / "STATUS.md"
    status_path.write_text(status_path.read_text() + "Human notes stay here.\n", encoding="utf-8")
    from agent_kanban_pm.runtime.handoff_protocol import update_status_file
    update_status_file(tmp_path, {"state": "done", "handoff_ready": True, "summary": "First task"})
    assert "Human notes stay here." in status_path.read_text()
    stale = read_status_file(tmp_path)
    second = SimpleNamespace(id=12, task_id=2, project_id=7, run_token="second")
    assert status_matches_session(stale, second) is False

    initialize_status_file(
        tmp_path, task_id=2, project_id=7, session_id=12, run_token="second",
        current_agent="agent-b", assigned_role="worker",
    )
    fresh = read_status_file(tmp_path)
    assert fresh["state"] == "assigned"
    assert fresh["handoff_ready"] is False
    assert fresh["validated"].task_id == 2
    assert fresh["validated"].session_id == 12
    assert status_matches_session(fresh, second) is False
    update_status_file(tmp_path, {"state": "done", "handoff_ready": True, "summary": "Second task"})
    assert status_matches_session(read_status_file(tmp_path), second) is True
