import logging
import os
import signal
import subprocess
import time
import select
import threading
import re
from pathlib import Path
from typing import Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

try:
    import pty
    HAS_PTY = True
except ImportError:
    HAS_PTY = False

ANSI_ESCAPE = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-9:;<=>?]*[!"#$%&\'()*+,\-./]*[@@-~]')


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub('', text)


class PtySession:
    def __init__(self, session_name: str, process: subprocess.Popen, master_fd: Optional[int], is_pty: bool):
        self.session_name = session_name
        self.process = process
        self.master_fd = master_fd
        self.is_pty = is_pty
        self.buffer: List[str] = []
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None

    def append_output(self, text: str):
        with self.lock:
            self.buffer.append(text)
            total_len = sum(len(x) for x in self.buffer)
            if total_len > 250000:
                merged = "".join(self.buffer)
                self.buffer = [merged[-200000:]]

    def get_output(self) -> str:
        with self.lock:
            return "".join(self.buffer)

    def write_input(self, text: str):
        if self.is_pty and self.master_fd is not None:
            try:
                os.write(self.master_fd, text.encode("utf-8"))
            except Exception as e:
                logger.warning(f"Failed writing to PTY {self.session_name}: {e}")
        else:
            try:
                if self.process.stdin:
                    self.process.stdin.write(text.encode("utf-8"))
                    self.process.stdin.flush()
            except Exception as e:
                logger.warning(f"Failed writing to subprocess stdin {self.session_name}: {e}")


class PtySessionManager:
    def __init__(self):
        self.sessions: Dict[str, PtySession] = {}
        self.lock = threading.Lock()

    def exists(self, session_name: str) -> bool:
        with self.lock:
            session = self.sessions.get(session_name)
            if not session:
                return False
            alive = session.process.poll() is None
            if not alive:
                self._cleanup_session(session)
                self.sessions.pop(session_name, None)
                return False
            return True

    def start_pty_session(
        self,
        session_name: str,
        cwd: str | Path,
        args: list[str],
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        with self.lock:
            if session_name in self.sessions:
                self._kill_session_by_name(session_name)

            cwd_str = str(cwd)
            env_dict = dict(env) if env is not None else dict(os.environ)
            env_dict["PYTHONUNBUFFERED"] = "1"
            env_dict["FORCE_COLOR"] = "0"

            if HAS_PTY:
                master_fd, slave_fd = pty.openpty()
                try:
                    process = subprocess.Popen(
                        args,
                        stdin=slave_fd,
                        stdout=slave_fd,
                        stderr=slave_fd,
                        cwd=cwd_str,
                        env=env_dict,
                        preexec_fn=os.setsid,
                        close_fds=True,
                    )
                except Exception as exc:
                    os.close(master_fd)
                    os.close(slave_fd)
                    raise exc
                os.close(slave_fd)
                session = PtySession(session_name, process, master_fd, is_pty=True)
            else:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    cwd=cwd_str,
                    env=env_dict,
                    close_fds=True,
                )
                session = PtySession(session_name, process, None, is_pty=False)

            self.sessions[session_name] = session

        def read_loop():
            if session.is_pty and session.master_fd is not None:
                m_fd = session.master_fd
                proc = session.process
                while proc.poll() is None:
                    try:
                        r, _, _ = select.select([m_fd], [], [], 0.1)
                        if r:
                            data = os.read(m_fd, 8192)
                            if not data:
                                break
                            session.append_output(data.decode("utf-8", errors="replace"))
                    except Exception:
                        break
                try:
                    while True:
                        r, _, _ = select.select([m_fd], [], [], 0.05)
                        if not r:
                            break
                        data = os.read(m_fd, 8192)
                        if not data:
                            break
                        session.append_output(data.decode("utf-8", errors="replace"))
                except Exception:
                    pass
                try:
                    os.close(m_fd)
                except Exception:
                    pass
            else:
                proc = session.process
                if proc.stdout:
                    while proc.poll() is None:
                        try:
                            r, _, _ = select.select([proc.stdout], [], [], 0.1)
                            if r:
                                data = proc.stdout.read(4096)
                                if not data:
                                    break
                                session.append_output(data.decode("utf-8", errors="replace"))
                        except Exception:
                            break
                    try:
                        data = proc.stdout.read()
                        if data:
                            session.append_output(data.decode("utf-8", errors="replace"))
                    except Exception:
                        pass

        session.thread = threading.Thread(target=read_loop, name=f"pty-reader-{session_name}", daemon=True)
        session.thread.start()
        logger.info(f"Started PTY session '{session_name}' (pid={process.pid}, pty={session.is_pty})")

    def get_session_process(self, session_name: str) -> Optional[subprocess.Popen]:
        with self.lock:
            session = self.sessions.get(session_name)
            return session.process if session else None

    def kill_session(self, session_name: str) -> bool:
        with self.lock:
            return self._kill_session_by_name(session_name)

    def capture_pane(self, session_name: str, lines: int = 50) -> str:
        session = None
        with self.lock:
            session = self.sessions.get(session_name)
        if not session:
            return ""

        raw_text = session.get_output()
        cleaned = strip_ansi(raw_text)

        text_lines = cleaned.splitlines()
        if len(text_lines) <= lines:
            return cleaned
        return "\n".join(text_lines[-lines:])

    def send_text(self, session_name: str, text: str, press_enter: bool = True) -> None:
        session = None
        with self.lock:
            session = self.sessions.get(session_name)
        if not session:
            logger.warning(f"Cannot send text: session '{session_name}' not found")
            return

        to_send = text + "\n" if press_enter else text
        session.write_input(to_send)

    def _kill_session_by_name(self, session_name: str) -> bool:
        session = self.sessions.pop(session_name, None)
        if not session:
            return False
        return self._cleanup_session(session)

    def _cleanup_session(self, session: PtySession) -> bool:
        pid = session.process.pid
        logger.info(f"Cleaning up PTY session '{session.session_name}' (pid={pid})")

        try:
            if session.is_pty:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                session.process.terminate()
        except Exception:
            pass

        for _ in range(10):
            if session.process.poll() is not None:
                break
            time.sleep(0.05)

        if session.process.poll() is None:
            try:
                if session.is_pty:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                else:
                    session.process.kill()
            except Exception:
                pass

        if session.master_fd is not None:
            try:
                os.close(session.master_fd)
            except Exception:
                pass
            session.master_fd = None

        return True


pty_manager = PtySessionManager()
