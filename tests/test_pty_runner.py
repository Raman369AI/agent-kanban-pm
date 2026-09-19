import sys
import time
import pytest
from unittest import mock
from agent_kanban_pm.runtime.pty_manager import pty_manager, strip_ansi
from agent_kanban_pm.runtime.process_launcher import (
    runner_available,
    has_session,
    kill_session,
    start_session,
    capture_pane,
    send_text,
    session_state,
    start_tmux_session,
    tmux_session_state,
)


def test_strip_ansi():
    text_with_ansi = "\x1b[31mError:\x1b[0m \x1b[4mSomething went wrong\x1b[24m"
    assert strip_ansi(text_with_ansi) == "Error: Something went wrong"


def test_pty_session_lifecycle():
    session_name = "test-pty-lifecycle"

    cmd = [
        sys.executable,
        "-c",
        "import sys, time; "
        "print('READY'); sys.stdout.flush(); "
        "line = sys.stdin.readline().strip(); "
        "print('ECHO:', line); sys.stdout.flush(); "
        "time.sleep(2)"
    ]

    pty_manager.start_pty_session(session_name, cwd=".", args=cmd)

    try:
        ready = False
        output = ""
        for _ in range(30):
            output = pty_manager.capture_pane(session_name, lines=10)
            if "READY" in output:
                ready = True
                break
            time.sleep(0.1)

        assert ready, f"Process did not output READY: {output}"

        pty_manager.send_text(session_name, "hello-from-test")

        echoed = False
        for _ in range(30):
            output = pty_manager.capture_pane(session_name, lines=10)
            if "ECHO: hello-from-test" in output:
                echoed = True
                break
            time.sleep(0.1)

        assert echoed, f"Process did not echo input: {output}"
        assert pty_manager.exists(session_name)

    finally:
        pty_manager.kill_session(session_name)

    assert not pty_manager.exists(session_name)


def test_process_launcher_fallback():
    with mock.patch("agent_kanban_pm.runtime.process_launcher.tmux_available", return_value=False):
        session_name = "test-launcher-fallback"
        cmd = [
            sys.executable,
            "-c",
            "import sys, time; "
            "print('LAUNCHED'); sys.stdout.flush(); "
            "time.sleep(10)"
        ]

        assert runner_available() is True

        start_session(session_name=session_name, cwd=".", args=cmd, kill_existing=True)

        try:
            launched = False
            for _ in range(30):
                output = capture_pane(session_name, lines=10)
                if "LAUNCHED" in output:
                    launched = True
                    break
                time.sleep(0.1)

            assert launched
            assert has_session(session_name) is True

        finally:
            kill_session(session_name)

        assert has_session(session_name) is False


def test_pty_exit_state_retains_code_and_output():
    session_name = "test-pty-exit-state"
    cmd = [sys.executable, "-c", "print(\"finished\"); raise SystemExit(7)"]

    with mock.patch("agent_kanban_pm.runtime.process_launcher.tmux_available", return_value=False):
        start_session(session_name=session_name, cwd=".", args=cmd)
        try:
            state = session_state(session_name)
            for _ in range(30):
                if state.status == "exited":
                    break
                time.sleep(0.1)
                state = session_state(session_name)

            assert state.status == "exited"
            assert state.exit_code == 7
            assert has_session(session_name) is False
            assert "finished" in capture_pane(session_name)
        finally:
            kill_session(session_name)


def test_tmux_state_reads_pane_exit_status(monkeypatch):
    completed = mock.Mock(returncode=0, stdout="1:23\n")
    monkeypatch.setattr(
        "agent_kanban_pm.runtime.process_launcher.subprocess.run",
        lambda *args, **kwargs: completed,
    )

    state = tmux_session_state("task-pane")

    assert state.status == "exited"
    assert state.exit_code == 23

    completed.stdout = "1:\n"
    state = tmux_session_state("task-pane")
    assert state.status == "exited"
    assert state.exit_code == 0


def test_tmux_launch_execs_cli_after_enabling_remain_on_exit(monkeypatch):
    calls = []
    sent = []

    monkeypatch.setattr(
        "agent_kanban_pm.runtime.process_launcher.tmux_available", lambda: True
    )
    monkeypatch.setattr(
        "agent_kanban_pm.runtime.process_launcher.tmux_session_state",
        lambda _: mock.Mock(status="missing"),
    )
    monkeypatch.setattr(
        "agent_kanban_pm.runtime.process_launcher.subprocess.run",
        lambda args, **kwargs: calls.append(args) or mock.Mock(returncode=0),
    )
    monkeypatch.setattr(
        "agent_kanban_pm.runtime.process_launcher.tmux_send_literal",
        lambda name, text, check=False: sent.append((name, text, check)),
    )

    start_tmux_session(
        session_name="task-pane", cwd=".", args=["agent-cli", "run"],
        env={"KANBAN_SESSION_ID": "42"},
    )

    assert calls[0][:4] == ["tmux", "new-session", "-d", "-s"]
    assert calls[1] == [
        "tmux", "set-option", "-t", "task-pane", "remain-on-exit", "on"
    ]
    assert sent == [(
        "task-pane", "KANBAN_SESSION_ID=42 exec agent-cli run", True
    )]
    assert calls[2] == ["tmux", "send-keys", "-t", "task-pane", "Enter"]


def test_tmux_missing_socket_is_distinct_from_inaccessible_socket(monkeypatch):
    completed = mock.Mock(returncode=1, stdout="", stderr="error connecting to /tmp/tmux-test (No such file or directory)")
    monkeypatch.setattr("agent_kanban_pm.runtime.process_launcher.subprocess.run", lambda *a, **kw: completed)
    assert tmux_session_state("reserved-run").status == "missing"
    completed.stderr = "error connecting to /tmp/tmux-test (Permission denied)"
    assert tmux_session_state("reserved-run").status == "unknown"
