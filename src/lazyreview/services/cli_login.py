"""Drive agent/GitHub CLI logins from the browser and check auth status.

Each CLI prints a URL (and, for gh, a device code) when run without a TTY; agy
additionally waits for the authorization code to be pasted on stdin. The login
process runs in the daemon; the Settings page polls for the URL and relays the
code back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

LOGIN_TIMEOUT_SECONDS = 600
STATUS_TIMEOUT_SECONDS = 45
_OUTPUT_LIMIT = 20_000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_URL_RE = re.compile(r"https://[^\s\"'<>]+")
_GH_CODE_RE = re.compile(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})")
_AGY_PASTE_RE = re.compile(r"paste the authorization code", re.IGNORECASE)


@dataclass(frozen=True)
class LoginProvider:
    id: str
    label: str
    binary: str
    argv: tuple[str, ...]
    env_set: dict[str, str] = field(default_factory=dict)
    env_unset: tuple[str, ...] = ()
    accepts_code: bool = False
    # agy refuses to start its OAuth flow when stdin is a pipe.
    stdin_tty: bool = False


LOGIN_PROVIDERS: dict[str, LoginProvider] = {
    "github": LoginProvider(
        id="github",
        label="GitHub",
        binary="gh",
        argv=("gh", "auth", "login", "--web", "--hostname", "github.com", "--git-protocol", "https"),
        # gh refuses to store a login while a token env var is set.
        env_unset=("GH_TOKEN", "GITHUB_TOKEN"),
    ),
    "cursor": LoginProvider(
        id="cursor",
        label="Cursor",
        binary="agent",
        argv=("agent", "login"),
        env_set={"NO_OPEN_BROWSER": "1"},
    ),
    "agy": LoginProvider(
        id="agy",
        label="Agy",
        binary="agy",
        # agy has no login subcommand; any print-mode prompt starts the OAuth flow.
        argv=("agy", "-p", "Reply with the single word OK."),
        accepts_code=True,
        stdin_tty=True,
    ),
}


@dataclass
class LoginSession:
    id: str
    provider: str
    proc: subprocess.Popen[bytes]
    started_at: float
    tty_fd: int | None = None
    output: str = ""
    status: str = "running"  # running | succeeded | failed | cancelled
    exit_code: int | None = None

    def view(self) -> dict[str, Any]:
        url = None
        for match in _URL_RE.finditer(self.output):
            url = match.group(0).rstrip(".,)")
            break
        code_match = _GH_CODE_RE.search(self.output)
        provider = LOGIN_PROVIDERS[self.provider]
        return {
            "id": self.id,
            "provider": self.provider,
            "status": self.status,
            "url": url,
            "user_code": code_match.group(1) if code_match else None,
            "needs_code": (
                provider.accepts_code
                and self.status == "running"
                and bool(_AGY_PASTE_RE.search(self.output))
            ),
            "exit_code": self.exit_code,
            "output": self.output[-4000:],
        }


class LoginManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, LoginSession] = {}

    def start(self, provider_id: str) -> LoginSession:
        provider = LOGIN_PROVIDERS.get(provider_id)
        if provider is None:
            raise ValueError(f"login not supported for {provider_id}")
        binary = shutil.which(provider.binary)
        if not binary:
            raise RuntimeError(f"{provider.binary} not found on PATH")

        with self._lock:
            for session in list(self._sessions.values()):
                if session.provider == provider_id and session.status == "running":
                    self._terminate(session, "cancelled")

        env = os.environ.copy()
        for key in provider.env_unset:
            env.pop(key, None)
        env.update(provider.env_set)
        tty_fd: int | None = None
        stdin: int = subprocess.PIPE
        if provider.stdin_tty:
            tty_fd, stdin = os.openpty()
        try:
            proc = subprocess.Popen(
                [binary, *provider.argv[1:]],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=os.path.expanduser("~"),
            )
        except OSError:
            if tty_fd is not None:
                os.close(tty_fd)
            raise
        finally:
            if tty_fd is not None:
                os.close(stdin)
        session = LoginSession(
            id=uuid.uuid4().hex,
            provider=provider_id,
            proc=proc,
            started_at=time.time(),
            tty_fd=tty_fd,
        )
        with self._lock:
            self._sessions[session.id] = session
        threading.Thread(
            target=self._pump, args=(session,), name=f"login-{provider_id}", daemon=True
        ).start()
        timer = threading.Timer(LOGIN_TIMEOUT_SECONDS, self._expire, args=(session,))
        timer.daemon = True
        timer.start()
        return session

    def get(self, session_id: str) -> LoginSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def submit_code(self, session_id: str, code: str) -> None:
        session = self.get(session_id)
        if session is None or session.status != "running":
            raise ValueError("login session is not running")
        line = code.strip().encode("utf-8") + b"\n"
        try:
            if session.tty_fd is not None:
                os.write(session.tty_fd, line)
            elif session.proc.stdin is not None:
                session.proc.stdin.write(line)
                session.proc.stdin.flush()
            else:
                raise ValueError("login session does not accept input")
        except OSError as e:
            raise ValueError("login session is not running") from e

    def cancel(self, session_id: str) -> None:
        session = self.get(session_id)
        if session is not None and session.status == "running":
            self._terminate(session, "cancelled")

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.status == "running":
                self._terminate(session, "cancelled")

    @staticmethod
    def _terminate(session: LoginSession, status: str) -> None:
        session.status = status
        try:
            session.proc.terminate()
            session.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            session.proc.kill()
        except OSError:
            pass

    def _expire(self, session: LoginSession) -> None:
        if session.status == "running":
            self._terminate(session, "failed")

    def _pump(self, session: LoginSession) -> None:
        stream = session.proc.stdout
        assert stream is not None
        while True:
            chunk = stream.read1(4096)  # type: ignore[attr-defined]
            if not chunk:
                break
            text = _ANSI_RE.sub("", chunk.decode("utf-8", errors="replace"))
            session.output = (session.output + text)[-_OUTPUT_LIMIT:]
        rc = session.proc.wait()
        if session.tty_fd is not None:
            os.close(session.tty_fd)
            session.tty_fd = None
        session.exit_code = rc
        if session.status == "running":
            session.status = "succeeded" if rc == 0 else "failed"
        logger.info("login %s for %s finished: %s", session.id, session.provider, session.status)


def _run_status(argv: list[str]) -> tuple[int, str]:
    binary = shutil.which(argv[0])
    if not binary:
        return 127, f"{argv[0]} not found on PATH"
    try:
        proc = subprocess.run(
            [binary, *argv[1:]],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} timed out"
    text = _ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, text


def check_provider(provider: str) -> dict[str, Any]:
    """Ask the CLI whether it is authenticated. Returns {ok, detail}."""
    if provider == "github":
        rc, text = _run_status(["gh", "auth", "status"])
        return {"ok": rc == 0, "detail": text}
    if provider == "claude":
        rc, text = _run_status(["claude", "auth", "status"])
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "detail": text}
        ok = bool(data.get("loggedIn"))
        method = data.get("authMethod") or "none"
        return {"ok": ok, "detail": f"logged in via {method}" if ok else "not logged in"}
    if provider == "cursor":
        rc, text = _run_status(["agent", "status"])
        ok = rc == 0 and "not logged in" not in text.lower()
        return {"ok": ok, "detail": text}
    if provider == "agy":
        rc, text = _run_status(["agy", "models"])
        ok = rc == 0 and "sign in" not in text.lower()
        return {"ok": ok, "detail": text if not ok else "models available"}
    raise ValueError(f"unknown provider {provider}")
