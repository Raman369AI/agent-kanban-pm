import sys
import time
import pytest
from unittest import mock
from kanban_runtime.pty_manager import pty_manager, strip_ansi
from kanban_runtime.process_launcher import (
    runner_available,
    has_session,
    kill_session,
    start_session,
    capture_pane,
    send_text,
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
    with mock.patch("kanban_runtime.process_launcher.tmux_available", return_value=False):
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
