"""Adapter regression tests for the Gemini -> Antigravity migration.

Google retired Gemini CLI for consumer accounts on 2026-06-18 and replaced it
with Antigravity CLI (`agy`). The gemini adapter has since been removed
outright, so nothing may reintroduce it, and the flags in the bundled adapters
must match what the real CLIs actually accept.

Flags asserted here were read from the installed binaries:
  agy --help       (v1.0.1)
  opencode --help  (v1.18.22)
"""

from __future__ import annotations

import os

import pytest

import tests_helper  # noqa: F401  — autouse cleanup listeners

from agent_kanban_pm.runtime.adapter_loader import (
    BUNDLED_ADAPTERS_DIR,
    POPULAR_CLI_TOOLS,
    AdapterSpec,
    load_adapter,
)
from agent_kanban_pm.runtime.assignment_launcher import _build_agent_command
from agent_kanban_pm.runtime.preferences import AUTONOMY_AUTO, AUTONOMY_SUPERVISED


def bundled_adapters() -> list[AdapterSpec]:
    """Load the adapters that ship in the wheel.

    Deliberately not load_all_adapters(): that reads ~/.kanban/agents, which
    holds whatever the developer copied at some earlier point. These tests are
    about what we ship.
    """
    specs = [load_adapter(path) for path in sorted(BUNDLED_ADAPTERS_DIR.glob("*.yaml"))]
    loaded = [spec for spec in specs if spec]
    assert loaded, f"no bundled adapters found in {BUNDLED_ADAPTERS_DIR}"
    return loaded


def _adapter(name: str) -> AdapterSpec:
    by_name = {a.name: a for a in bundled_adapters()}
    assert name in by_name, f"adapter {name!r} not bundled: {sorted(by_name)}"
    return by_name[name]


@pytest.fixture
def cli_stubs(tmp_path, monkeypatch):
    """Put inert stubs for the bundled CLIs on PATH.

    `_build_agent_command` resolves the command with shutil.which and raises if
    it is missing. CI installs none of the agent CLIs, and these tests are about
    the argument vector rather than the binary, so a stub is enough. They are
    never executed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in ("agy", "opencode", "claude", "codex", "aider"):
        stub = bin_dir / command
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir


# ---------------------------------------------------------------------------
# Deprecation
# ---------------------------------------------------------------------------


def test_the_retired_gemini_adapter_is_gone():
    """The adapter was removed outright, not merely hidden."""
    assert not any(adapter.name == "gemini" for adapter in bundled_adapters())


def test_antigravity_is_not_deprecated():
    assert _adapter("antigravity").deprecated is False


def test_discovery_offers_antigravity_not_the_retired_gemini_cli():
    commands = [command for command, _display in POPULAR_CLI_TOOLS]
    assert "agy" in commands
    assert "gemini" not in commands


def test_every_bundled_adapter_defaults_to_not_deprecated():
    for adapter in bundled_adapters():
        assert adapter.deprecated is False, f"{adapter.name} unexpectedly deprecated"


# ---------------------------------------------------------------------------
# Antigravity invocation
# ---------------------------------------------------------------------------


def test_antigravity_uses_the_flags_agy_actually_accepts():
    agy = _adapter("antigravity")
    assert agy.invoke.command == "agy"
    # `agy --help` lists no --model and no --mcp flag; guessing them would
    # produce an unparseable command line at launch.
    assert agy.invoke.model_flag is None
    assert agy.invoke.mcp_flag is None

    args = agy.task_command.args
    assert "--prompt-interactive={prompt}" in args
    assert "--add-dir" in args and "{workspace}" in args


def test_antigravity_auto_args_carry_the_real_bypass_flag():
    agy = _adapter("antigravity")
    assert agy.task_command.auto_args == ["--dangerously-skip-permissions"]
    # The old value was Codex's flag, which agy rejects.
    assert "--full-auto" not in agy.task_command.auto_args


def test_antigravity_chat_designer_demands_a_tty():
    """`agy --print` writes nothing when stdout is a pipe; it needs a PTY."""
    assert _adapter("antigravity").chat_designer.requires_tty is True


def test_antigravity_supervised_run_omits_the_bypass_flag(tmp_path, cli_stubs):
    agy = _adapter("antigravity")
    cmd = _build_agent_command(agy, str(tmp_path), "do the thing", AUTONOMY_SUPERVISED)
    assert "--dangerously-skip-permissions" not in cmd
    assert "--prompt-interactive=do the thing" in cmd
    # The prompt must never arrive as a bare argument: agy would ignore it.
    assert "do the thing" not in cmd


def test_antigravity_auto_run_appends_the_bypass_flag(tmp_path, cli_stubs):
    agy = _adapter("antigravity")
    cmd = _build_agent_command(agy, str(tmp_path), "do the thing", AUTONOMY_AUTO)
    assert "--dangerously-skip-permissions" in cmd


# ---------------------------------------------------------------------------
# OpenCode invocation
# ---------------------------------------------------------------------------


def test_opencode_seeds_the_interactive_tui_rather_than_running_headless():
    """`opencode run` is headless and cannot surface an approval prompt.

    Supervised mode captures the prompt from the tmux pane, so the task must
    start the interactive TUI. --dir is unnecessary: the pane is already
    created with the worktree as its working directory.
    """
    oc = _adapter("opencode")
    args = oc.task_command.args
    assert args[0] != "run", "the headless subcommand cannot prompt for approval"
    assert args == ["--prompt", "{prompt}"]
    # The previous adapter passed a filename as the message, so the agent
    # received the literal string ".kanban_task.md" instead of the task.
    assert oc.task_command.prompt_file is None
    assert ".kanban_task.md" not in args


def test_opencode_auto_args_use_its_own_approval_flag():
    assert _adapter("opencode").task_command.auto_args == ["--auto"]


def test_opencode_supervised_run_omits_auto(tmp_path, cli_stubs):
    oc = _adapter("opencode")
    cmd = _build_agent_command(oc, str(tmp_path), "ship it", AUTONOMY_SUPERVISED)
    assert "--auto" not in cmd
    assert cmd[1] == "--prompt"
    assert cmd[2] == "ship it"


def test_opencode_auto_run_appends_auto(tmp_path, cli_stubs):
    oc = _adapter("opencode")
    cmd = _build_agent_command(oc, str(tmp_path), "ship it", AUTONOMY_AUTO)
    assert "--auto" in cmd


# ---------------------------------------------------------------------------
# Cross-adapter invariants
# ---------------------------------------------------------------------------


def test_no_bundled_adapter_hides_a_bypass_flag_in_its_supervised_args():
    """A bypass flag in `args` would run unsupervised regardless of autonomy."""
    bypass_markers = (
        "--dangerously-skip-permissions",
        "--yes-always",
        "--full-auto",
        "--auto",
        "yolo",
        "bypasspermissions",
    )
    for adapter in bundled_adapters():
        joined = " ".join(adapter.task_command.args).lower()
        for marker in bypass_markers:
            assert marker not in joined, (
                f"{adapter.name} carries {marker!r} in supervised args; it belongs "
                f"in auto_args"
            )


# ---------------------------------------------------------------------------
# Persistent role sessions
# ---------------------------------------------------------------------------


def test_codex_declares_no_mcp_launch_flag():
    """`codex --mcp` is rejected: mcp is a subcommand, not a launch flag."""
    assert _adapter("codex").invoke.mcp_flag == ""


def test_adapters_with_subcommand_task_args_declare_a_role_command():
    """A role session has no prompt, so a subcommand that demands one cannot
    be reused for it. Such adapters must say how they start as a service."""
    for adapter in bundled_adapters():
        args = adapter.task_command.args
        if not args:
            continue
        first = args[0]
        # A leading flag is fine, and a leading placeholder is just the prompt.
        if first.startswith("-") or "{" in first:
            continue
        assert adapter.role_command is not None, (
            f"{adapter.name} starts its task command with the subcommand "
            f"{args[0]!r}; it needs a role_command for persistent sessions"
        )


def test_role_command_is_used_verbatim_for_persistent_sessions(monkeypatch):
    from agent_kanban_pm.runtime.role_supervisor import build_command_for_role
    from agent_kanban_pm.runtime.preferences import RoleAssignment

    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")
    adapter = _adapter("opencode")
    command = build_command_for_role(
        adapter,
        RoleAssignment(agent="opencode", mode="headless"),
        "worker",
        "http://localhost:8000",
    )

    # The bare `run` subcommand must not survive into a role session.
    assert "run" not in command
    assert command[0].endswith("opencode")


def test_role_command_adds_autonomy_flags_only_when_auto(monkeypatch):
    from agent_kanban_pm.runtime.role_supervisor import build_command_for_role
    from agent_kanban_pm.runtime.preferences import RoleAssignment

    monkeypatch.setattr("shutil.which", lambda command: f"/usr/bin/{command}")
    adapter = _adapter("claude")

    supervised = build_command_for_role(
        adapter,
        RoleAssignment(agent="claude", mode="headless", autonomy="supervised"),
        "orchestrator",
        "http://localhost:8000",
    )
    assert "--permission-mode" not in supervised

    auto = build_command_for_role(
        adapter,
        RoleAssignment(agent="claude", mode="headless", autonomy="auto"),
        "orchestrator",
        "http://localhost:8000",
    )
    assert auto[auto.index("--permission-mode") + 1] == "bypassPermissions"


def test_antigravity_runs_tasks_interactively_so_approvals_can_be_captured():
    """Supervised mode reads the CLI's approval prompt out of the tmux pane.

    --print is headless: it renders no prompt, so the approval queue never
    engages and every tool needing permission is auto-denied ("no output
    produced"). The prompt is attached with `=` because the flag takes it as
    its value and would otherwise swallow the following argument.
    """
    args = _adapter("antigravity").task_command.args

    assert not any(a == "--print" or a.startswith("--print=") for a in args), (
        "headless print mode cannot surface an approval prompt"
    )
    assert "--prompt-interactive={prompt}" in args
    assert args.index("--add-dir") < args.index("--prompt-interactive={prompt}")


def test_no_bundled_adapter_leaves_a_bare_print_before_another_flag():
    """A value-taking --print must never be followed by a flag.

    `--print --add-dir <dir>` makes the CLI treat "--add-dir" as the prompt.
    """
    for adapter in bundled_adapters():
        args = adapter.task_command.args
        for index, arg in enumerate(args[:-1]):
            if arg == "--print":
                assert not args[index + 1].startswith("-"), (
                    f"{adapter.name}: --print is followed by {args[index + 1]!r}, "
                    "which it would consume as the prompt"
                )


def test_claude_terminates_its_variadic_add_dir_before_the_prompt():
    """`--add-dir <directories...>` consumes arguments until the next flag.

    With ["--print", "--add-dir", "{workspace}", "{prompt}"] the prompt was
    taken as a second directory and claude ran with no instruction at all.
    """
    args = _adapter("claude").task_command.args

    add_dir = args.index("--add-dir")
    prompt = args.index("{prompt}")
    between = args[add_dir + 1:prompt]
    assert any(a.startswith("-") for a in between), (
        "a flag must terminate --add-dir before the positional prompt"
    )
    assert args[-1] == "{prompt}"


def test_codex_auto_args_use_a_flag_codex_still_accepts():
    """`codex --full-auto` is rejected by current codex."""
    auto = _adapter("codex").task_command.auto_args
    assert "--full-auto" not in auto
    assert auto == ["--dangerously-bypass-approvals-and-sandbox"]
