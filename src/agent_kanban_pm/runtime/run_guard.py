"""Portable durable at-most-once execution for one task run.

The receipt is an atomic, persistent claim. It intentionally survives both the
server and the executing process: uncertain or remote claims are never replayed.
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Optional


UNCERTAIN_EXIT = 125
ALREADY_CLAIMED_EXIT = 75


def _write_receipt(path: Path, payload: dict) -> None:
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        directory = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory)
    except OSError:
        pass
    finally:
        os.close(directory)


def _create_claim(path: Path, payload: dict) -> bool:
    """Create the permanent claim atomically across processes and hosts."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        # The file remains as a fail-closed claim even if serialization fails.
        raise
    return True


def _windows_process_identity(pid: int) -> Optional[str]:
    process_query = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query, False, pid)
    if not handle:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        ok = ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return None
        if exit_code.value != 259:  # STILL_ACTIVE
            return None
        return str(creation.value)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _process_identity(pid: int) -> Optional[str]:
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_identity(pid)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        return None
    try:
        # Linux start time prevents a recycled PID from matching the receipt.
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return fields[21]
    except (OSError, IndexError):
        # macOS and other POSIX systems still get liveness protection.
        return "alive"


def _process_matches(pid: object, identity: object) -> bool:
    try:
        current = _process_identity(int(pid))
    except (TypeError, ValueError):
        return False
    return current is not None and (identity in (None, "alive") or current == identity)


def execution_state(receipt: str) -> tuple[str, int | None]:
    """Observe a claimed run without making a remote claim replayable."""
    path = Path(receipt)
    if not path.exists():
        return "missing", None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") == "exited":
            return "exited", int(data["exit_code"])
        owner_host = data.get("hostname")
        if owner_host and owner_host != socket.gethostname():
            if data.get("status") == "running" and data.get("heartbeat_at"):
                heartbeat = datetime.fromisoformat(data["heartbeat_at"])
                if heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=UTC)
                if (datetime.now(UTC) - heartbeat).total_seconds() <= 15:
                    return "running", None
            return "unknown", None
        if data.get("status") == "running":
            if _process_matches(data.get("child_pid"), data.get("child_identity")):
                return "running", None
            return "exited", UNCERTAIN_EXIT
        if data.get("status") == "claimed":
            if _process_matches(data.get("guard_pid"), data.get("guard_identity")):
                return "running", None
            return "exited", UNCERTAIN_EXIT
        return "unknown", None
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown", None


def execute_once(receipt: str, args: list[str]) -> int:
    path = Path(receipt)
    path.parent.mkdir(parents=True, exist_ok=True)
    claim = {
        "version": 2,
        "status": "claimed",
        "hostname": socket.gethostname(),
        "guard_pid": os.getpid(),
        "guard_identity": _process_identity(os.getpid()),
    }
    if not _create_claim(path, claim):
        return ALREADY_CLAIMED_EXIT
    try:
        process = subprocess.Popen(args)
        running = {
            **claim,
            "status": "running",
            "child_pid": process.pid,
            "child_identity": _process_identity(process.pid),
            "heartbeat_at": datetime.now(UTC).isoformat(),
        }
        _write_receipt(path, running)
        while True:
            try:
                code = process.wait(timeout=2)
                break
            except subprocess.TimeoutExpired:
                running["heartbeat_at"] = datetime.now(UTC).isoformat()
                _write_receipt(path, running)
    except Exception:
        _write_receipt(path, {**claim, "status": "exited", "exit_code": UNCERTAIN_EXIT})
        raise
    code = code if code >= 0 else 128 - code
    _write_receipt(path, {
        **running,
        "status": "exited",
        "exit_code": code,
    })
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("receipt")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    parsed = parser.parse_args()
    if not parsed.command:
        parser.error("a command is required")
    return execute_once(parsed.receipt, parsed.command)


if __name__ == "__main__":
    raise SystemExit(main())
