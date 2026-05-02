from kanban_runtime.handoff_protocol import (
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
)
from kanban_runtime.preferences import Preferences, RoleAssignment, RoleConfig


def test_default_profiles_match_multi_agent_protocol():
    """profile_for_agent resolves from adapter YAML first, then
    falls back to DEFAULT_AGENT_PROFILES for agents without YAML."""
    claude = profile_for_agent("claude")
    gemini = profile_for_agent("gemini")
    codex = profile_for_agent("codex")
    unknown = profile_for_agent("totally-unknown-agent")

    # All known agents resolve to canonical names
    assert claude.agent == "claude"
    assert gemini.agent == "gemini"
    assert codex.agent == "codex"

    # All profiles have a non-empty role string
    assert claude.role
    assert gemini.role
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
    (workspace / "GEMINI.md").write_text("custom gemini rules\n", encoding="utf-8")

    results = ensure_instruction_aliases(workspace)

    assert results["CLAUDE.md"] == "linked to AGENTS.md"
    assert (workspace / "CLAUDE.md").is_symlink()
    assert (workspace / "CLAUDE.md").readlink().as_posix() == "AGENTS.md"
    assert results["GEMINI.md"] == "kept existing real file"
    assert (workspace / "GEMINI.md").read_text(encoding="utf-8") == "custom gemini rules\n"
    assert results["CODEX.md"] == "linked to AGENTS.md"


def test_initialize_status_file_writes_worktree_task_handoff(tmp_path):
    workspace = tmp_path / "task-worktree"
    workspace.mkdir()

    path = initialize_status_file(
        workspace,
        task_id=12,
        project_id=3,
        current_agent="gemini",
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
    assert info["frontmatter"]["current_agent"] == "gemini"
    assert info["frontmatter"]["assigned_role"] == "ui"


def test_handoff_instructions_are_self_contained(tmp_path):
    workspace = tmp_path / "codex"
    workspace.mkdir()
    text = build_handoff_instructions("codex", workspace)

    assert "Read AGENTS.md for instructions only" in text
    assert "CLAUDE.md, GEMINI.md, and CODEX.md should be symlinks to AGENTS.md" in text
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
        "kanban_runtime.handoff_protocol.discover_popular_clis",
        lambda: [],
    )

    agents = available_handoff_agents(prefs=prefs, adapters=[])
    assert "claude" in agents
    assert "gemini" in agents
    assert "codex" in agents
    assert "custom-cli" in agents
