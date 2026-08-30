"""Adapter regression tests for the Gemini -> Antigravity migration.

Google retired Gemini CLI for consumer accounts on 2026-06-18 and replaced it
with Antigravity CLI (`agy`). The gemini adapter stays loadable because Gemini
Code Assist Standard/Enterprise licences keep working, but it must not be
offered to new users, and the flags in the bundled adapters must match what the
real CLIs actually accept.

Flags asserted here were read from the installed binaries:
  agy --help       (v1.0.1)
  opencode --help  (v1.18.22)
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Deprecation
# ---------------------------------------------------------------------------


def test_gemini_is_marked_deprecated_and_points_at_its_replacement():
    gemini = _adapter("gemini")
    assert gemini.deprecated is True
    assert gemini.replaced_by == "antigravity"
    assert gemini.deprecation_note
    assert "antigravity" in gemini.deprecation_note.lower()


def test_gemini_stays_loadable_for_enterprise_licence_holders():
    """Deprecated must mean hidden, never removed — enterprise access remains."""
    gemini = _adapter("gemini")
    assert gemini.invoke.command == "gemini"
    assert gemini.task_command.args, "retired adapter still needs a usable invocation"


def test_antigravity_is_not_deprecated():
    assert _adapter("antigravity").deprecated is False


def test_discovery_offers_antigravity_not_the_retired_gemini_cli():
    commands = [command for command, _display in POPULAR_CLI_TOOLS]
    assert "agy" in commands
    assert "gemini" not in commands


def test_every_other_bundled_adapter_defaults_to_not_deprecated():
    for adapter in bundled_adapters():
        if adapter.name == "gemini":
            continue
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
    assert "--print" in args, "non-interactive runs need --print"
    assert "-i" not in args, "-i is agy's *interactive* flag"
    assert "{prompt}" in args
    assert "--add-dir" in args and "{workspace}" in args


def test_antigravity_auto_args_carry_the_real_bypass_flag():
    agy = _adapter("antigravity")
    assert agy.task_command.auto_args == ["--dangerously-skip-permissions"]
    # The old value was Codex's flag, which agy rejects.
    assert "--full-auto" not in agy.task_command.auto_args


def test_antigravity_chat_designer_demands_a_tty():
    """`agy --print` writes nothing when stdout is a pipe; it needs a PTY."""
    assert _adapter("antigravity").chat_designer.requires_tty is True


def test_antigravity_supervised_run_omits_the_bypass_flag(tmp_path):
    agy = _adapter("antigravity")
    cmd = _build_agent_command(agy, str(tmp_path), "do the thing", AUTONOMY_SUPERVISED)
    assert "--dangerously-skip-permissions" not in cmd
    assert "--print" in cmd
    assert "do the thing" in cmd


def test_antigravity_auto_run_appends_the_bypass_flag(tmp_path):
    agy = _adapter("antigravity")
    cmd = _build_agent_command(agy, str(tmp_path), "do the thing", AUTONOMY_AUTO)
    assert "--dangerously-skip-permissions" in cmd


# ---------------------------------------------------------------------------
# OpenCode invocation
# ---------------------------------------------------------------------------


def test_opencode_uses_the_run_subcommand_with_the_prompt():
    oc = _adapter("opencode")
    args = oc.task_command.args
    assert args[0] == "run", "opencode's non-interactive form is `opencode run`"
    assert "{prompt}" in args, "the prompt is passed positionally to `run`"
    assert "--dir" in args and "{workspace}" in args
    # The previous adapter passed a filename as the message, so the agent
    # received the literal string ".kanban_task.md" instead of the task.
    assert oc.task_command.prompt_file is None
    assert ".kanban_task.md" not in args


def test_opencode_auto_args_use_its_own_approval_flag():
    assert _adapter("opencode").task_command.auto_args == ["--auto"]


def test_opencode_supervised_run_omits_auto(tmp_path):
    oc = _adapter("opencode")
    cmd = _build_agent_command(oc, str(tmp_path), "ship it", AUTONOMY_SUPERVISED)
    assert "--auto" not in cmd
    assert cmd[1] == "run"
    assert str(tmp_path) in cmd


def test_opencode_auto_run_appends_auto(tmp_path):
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
