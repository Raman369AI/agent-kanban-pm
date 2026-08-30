"""Tests for the chat designer's PTY execution path.

Some CLIs check whether stdout is a terminal and stay silent when it is a pipe.
Antigravity's `agy --print` behaves this way: under `subprocess.run` it blocks
until the timeout and exits 0 having written nothing, while the same command on
a PTY answers normally. Adapters declare `chat_designer.requires_tty` and the
designer then runs them on a pseudo-terminal.

These tests use a stub script that reproduces that behaviour rather than the
real `agy`, so they run anywhere and cost nothing.
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

import tests_helper  # noqa: F401  — autouse cleanup listeners

from agent_kanban_pm.cli.chat_designer import (
    DesignerError,
    DesignerInvocation,
    _run_on_pty,
    run_subprocess,
)


def _write_stub(tmp_path: Path, body: str) -> str:
    """Write an executable python stub and return its path."""
    stub = tmp_path / "stub_cli.py"
    stub.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(body), encoding="utf-8"
    )
    stub.chmod(0o755)
    return str(stub)


TTY_ONLY_STUB = """
    import sys
    # Mimics `agy --print`: only speak when attached to a terminal.
    if sys.stdout.isatty():
        sys.stdout.write("<plan>tty</plan>\\n")
    sys.exit(0)
"""


def _invocation(command: str, *, requires_tty: bool, timeout: int = 30,
                use_stdin: bool = False) -> DesignerInvocation:
    return DesignerInvocation(
        command=sys.executable,
        args=[command],
        use_stdin=use_stdin,
        env=os.environ.copy(),
        timeout_seconds=timeout,
        adapter_name="stub",
        display_name="Stub CLI",
        requires_tty=requires_tty,
    )


def test_pipe_run_loses_output_from_a_tty_only_cli(tmp_path):
    """Establishes the failure the PTY path exists to fix."""
    stub = _write_stub(tmp_path, TTY_ONLY_STUB)
    out = run_subprocess(_invocation(stub, requires_tty=False), "prompt")
    assert out.strip() == "", "stub should be silent on a pipe; test is not valid otherwise"


def test_pty_run_recovers_output_from_a_tty_only_cli(tmp_path):
    stub = _write_stub(tmp_path, TTY_ONLY_STUB)
    out = run_subprocess(_invocation(stub, requires_tty=True), "prompt")
    assert "<plan>tty</plan>" in out


def test_pty_output_is_newline_normalised(tmp_path):
    """A PTY emits CRLF; downstream parsing expects the same text a pipe gives."""
    stub = _write_stub(tmp_path, TTY_ONLY_STUB)
    out = run_subprocess(_invocation(stub, requires_tty=True), "prompt")
    assert "\r" not in out


def test_pty_run_strips_ansi_escapes(tmp_path):
    """CLIs colourise when they think they're on a terminal."""
    stub = _write_stub(tmp_path, """
        import sys
        sys.stdout.write("\\x1b[32m<plan>green</plan>\\x1b[0m\\n")
    """)
    out = run_subprocess(_invocation(stub, requires_tty=True), "prompt")
    assert "<plan>green</plan>" in out
    assert "\x1b" not in out


def test_pty_run_receives_the_prompt_as_an_argument(tmp_path):
    stub = _write_stub(tmp_path, """
        import sys
        sys.stdout.write("ARGS:" + "|".join(sys.argv[1:]) + "\\n")
    """)
    out = run_subprocess(_invocation(stub, requires_tty=True), "design me a board")
    assert "design me a board" in out


def test_pty_run_reports_a_nonzero_exit(tmp_path):
    stub = _write_stub(tmp_path, """
        import sys
        sys.stdout.write("things went wrong\\n")
        sys.exit(3)
    """)
    with pytest.raises(DesignerError) as excinfo:
        run_subprocess(_invocation(stub, requires_tty=True), "prompt")
    assert "exited 3" in str(excinfo.value)
    assert "things went wrong" in str(excinfo.value)


def test_pty_run_times_out_instead_of_hanging(tmp_path):
    stub = _write_stub(tmp_path, """
        import time
        time.sleep(60)
    """)
    with pytest.raises(DesignerError) as excinfo:
        run_subprocess(_invocation(stub, requires_tty=True, timeout=2), "prompt")
    assert "timed out" in str(excinfo.value)


def test_pty_run_drains_output_larger_than_the_pty_buffer(tmp_path):
    """A PTY buffer is small; a writer nobody drains would deadlock."""
    stub = _write_stub(tmp_path, """
        import sys
        for i in range(4000):
            sys.stdout.write("line %d padding padding padding padding\\n" % i)
    """)
    out = run_subprocess(_invocation(stub, requires_tty=True, timeout=60), "prompt")
    assert "line 0 " in out
    assert "line 3999 " in out


def test_requires_tty_with_stdin_is_rejected_clearly(tmp_path):
    """The PTY path passes the prompt as argv, so stdin mode is a config error."""
    stub = _write_stub(tmp_path, TTY_ONLY_STUB)
    invocation = _invocation(stub, requires_tty=True, use_stdin=True)
    with pytest.raises(DesignerError) as excinfo:
        run_subprocess(invocation, "prompt")
    assert "requires_tty" in str(excinfo.value)


def test_missing_command_raises_designer_error():
    invocation = DesignerInvocation(
        command="/nonexistent/definitely-not-a-cli",
        args=[],
        use_stdin=False,
        env=os.environ.copy(),
        timeout_seconds=10,
        adapter_name="stub",
        display_name="Stub CLI",
        requires_tty=True,
    )
    with pytest.raises(DesignerError) as excinfo:
        run_subprocess(invocation, "prompt")
    assert "failed to launch" in str(excinfo.value)


@pytest.mark.skipif(
    not Path("/proc/self/fd").exists(),
    reason="fd counting via /proc is Linux-only",
)
def test_run_on_pty_does_not_leak_file_descriptors(tmp_path):
    """openpty() twice per call would exhaust the fd table over a long session."""
    stub = _write_stub(tmp_path, TTY_ONLY_STUB)
    env = os.environ.copy()

    def open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    _run_on_pty([sys.executable, stub], env, 30)  # warm up any lazy imports
    before = open_fds()
    for _ in range(5):
        _run_on_pty([sys.executable, stub], env, 30)
    assert open_fds() <= before, "file descriptors leaked across PTY runs"
