"""Background PR analysis runner (queued drain, max 2 concurrent by default)."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import subprocess
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from lazyreview.agents import get_adapter
from lazyreview.config import Settings
from lazyreview.db import (
    get_pr,
    get_pr_analysis,
    record_analysis_finished_event,
    update_pr_analysis,
    utcnow,
)
from lazyreview.db.schema import connect
from lazyreview.services.consolidation_jobs import build_consolidate_command
from lazyreview.services.pr_workspace import hold_worktree
from lazyreview.services.settings import get_pr_analysis_config

logger = logging.getLogger(__name__)


def analyses_dir(settings: Settings) -> Path:
    path = settings.data_dir / "analyses"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_analysis_prompt(
    template: str,
    *,
    pr: dict[str, Any],
    brief_path: Path,
    worktree: Path,
) -> str:
    text = template
    replacements = {
        "{pr_url}": str(pr.get("url") or ""),
        "{owner}": str(pr.get("owner") or ""),
        "{repo}": str(pr.get("repo") or ""),
        "{number}": str(pr.get("number") or ""),
        "{title}": str(pr.get("title") or ""),
        "{head_sha}": str(pr.get("head_sha") or ""),
        "{head_ref}": str(pr.get("head_ref") or ""),
        "{base_ref}": str(pr.get("base_ref") or ""),
        "{worktree}": str(worktree),
        "{brief_path}": str(brief_path),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    footer = (
        "\n\n---\n"
        f"Write the full orientation brief as Markdown to this exact file path "
        f"(create/overwrite): `{brief_path}`\n"
        f"PR: {pr.get('url') or ''} — head `{pr.get('head_sha') or ''}`\n"
        f"Repository worktree (cwd): `{worktree}`\n"
    )
    return text.rstrip() + footer


class AnalysisRunner:
    def __init__(self, settings: Settings, conn: sqlite3.Connection) -> None:
        self.settings = settings
        self.conn = conn
        self._queue: Queue[int] = Queue()
        self._dispatcher: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cancel_requested: set[int] = set()
        self._procs: dict[int, subprocess.Popen[str]] = {}
        self._inflight: set[int] = set()

    @property
    def max_workers(self) -> int:
        return max(1, int(getattr(self.settings, "max_concurrent_analyses", 2) or 2))

    def _worker_conn(self) -> sqlite3.Connection:
        return connect(self.settings.db_path)

    def start(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        self._stop.clear()
        workers = self.max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="lazyreview-analysis",
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="lazyreview-analysis-dispatch",
            daemon=True,
        )
        self._dispatcher.start()

        orphaned = self.conn.execute(
            "SELECT id FROM pr_analyses WHERE status = 'running' ORDER BY id ASC"
        ).fetchall()
        for r in orphaned:
            aid = int(r["id"])
            updated = update_pr_analysis(
                self.conn,
                aid,
                status="failed",
                finished_at=utcnow(),
                error_message="Interrupted by daemon restart (orphaned running analysis)",
            )
            if updated:
                record_analysis_finished_event(self.conn, updated)
            logger.warning("marked orphaned running analysis %s as failed", aid)

        rows = self.conn.execute(
            "SELECT id FROM pr_analyses WHERE status = 'queued' ORDER BY id ASC"
        ).fetchall()
        for r in rows:
            self._queue.put(int(r["id"]))
        logger.info(
            "analysis runner started max_concurrent_analyses=%s queued=%s reclaimed_running=%s",
            workers,
            len(rows),
            len(orphaned),
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for proc in list(self._procs.values()):
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
        if self._dispatcher:
            self._dispatcher.join(timeout=5)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def enqueue(self, analysis_id: int) -> None:
        self._queue.put(int(analysis_id))

    def cancel(self, analysis_id: int) -> dict[str, Any]:
        with self._lock:
            self._cancel_requested.add(analysis_id)
            proc = self._procs.get(analysis_id)
        row = get_pr_analysis(self.conn, analysis_id)
        if not row:
            raise KeyError(analysis_id)
        if row["status"] not in ("queued", "running"):
            raise ValueError(f"Analysis is {row['status']}, cannot cancel")
        updated = update_pr_analysis(
            self.conn,
            analysis_id,
            status="cancelled",
            finished_at=utcnow(),
            error_message="Cancelled by user",
        )
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                logger.exception("failed to terminate analysis %s", analysis_id)
        assert updated is not None
        record_analysis_finished_event(self.conn, updated)
        return updated

    def cancel_for_pr(self, pr_id: int) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT id FROM pr_analyses
            WHERE pr_id = ? AND status IN ('queued', 'running')
            ORDER BY id ASC
            """,
            (pr_id,),
        ).fetchall()
        cancelled: list[int] = []
        for r in rows:
            aid = int(r["id"])
            try:
                self.cancel(aid)
                cancelled.append(aid)
            except (KeyError, ValueError):
                continue
        return cancelled

    def _is_cancel_requested(
        self, analysis_id: int, conn: sqlite3.Connection | None = None
    ) -> bool:
        with self._lock:
            if analysis_id in self._cancel_requested:
                return True
        own = conn is None
        c = conn or self._worker_conn()
        try:
            row = get_pr_analysis(c, analysis_id)
            return bool(row and row["status"] == "cancelled")
        except sqlite3.Error:
            logger.exception("cancel check failed for analysis %s", analysis_id)
            return False
        finally:
            if own:
                c.close()

    def _dispatch_loop(self) -> None:
        assert self._pool is not None
        while not self._stop.is_set():
            with self._lock:
                slots = self.max_workers - len(self._inflight)
            if slots <= 0:
                time.sleep(0.1)
                continue
            try:
                analysis_id = self._queue.get(timeout=0.25)
            except Empty:
                continue
            with self._lock:
                if analysis_id in self._inflight:
                    continue
                if analysis_id in self._cancel_requested:
                    continue
                self._inflight.add(analysis_id)
            self._pool.submit(self._run_job, analysis_id)

    def _run_job(self, analysis_id: int) -> None:
        conn = self._worker_conn()
        try:
            self._execute_with_conn(analysis_id, conn)
        except Exception:
            logger.exception("analysis %s crashed", analysis_id)
            try:
                row = get_pr_analysis(conn, analysis_id)
                if row and row["status"] in ("queued", "running"):
                    updated = update_pr_analysis(
                        conn,
                        analysis_id,
                        status="failed",
                        finished_at=utcnow(),
                        error_message=traceback.format_exc()[-2000:],
                    )
                    if updated:
                        record_analysis_finished_event(conn, updated)
            except Exception:
                logger.exception("failed to mark analysis %s failed", analysis_id)
        finally:
            with self._lock:
                self._inflight.discard(analysis_id)
                self._procs.pop(analysis_id, None)
                self._cancel_requested.discard(analysis_id)
            conn.close()

    def _mark_terminal(
        self,
        conn: sqlite3.Connection,
        analysis_id: int,
        *,
        status: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        updated = update_pr_analysis(conn, analysis_id, status=status, **fields)
        if updated:
            record_analysis_finished_event(conn, updated)
        return updated

    def _execute_with_conn(self, analysis_id: int, conn: sqlite3.Connection) -> None:
        row = get_pr_analysis(conn, analysis_id)
        if not row or row["status"] not in ("queued", "running"):
            return
        if self._is_cancel_requested(analysis_id, conn):
            return

        pr = get_pr(conn, int(row["pr_id"]))
        if not pr:
            self._mark_terminal(
                conn,
                analysis_id,
                status="failed",
                finished_at=utcnow(),
                error_message="PR not found",
            )
            return

        head_sha = (row.get("head_sha") or pr.get("head_sha") or "").strip()
        if not head_sha:
            self._mark_terminal(
                conn,
                analysis_id,
                status="failed",
                finished_at=utcnow(),
                error_message="PR has no head_sha",
            )
            return

        work_dir = Path(row["workdir_path"] or analyses_dir(self.settings) / str(analysis_id))
        work_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = work_dir / "prompt.md"
        brief_path = work_dir / "brief.md"
        stdout_path = work_dir / "stdout.log"
        stderr_path = work_dir / "stderr.log"
        cli_path = work_dir / "cli.json"

        started = time.monotonic()
        update_pr_analysis(
            conn,
            analysis_id,
            status="running",
            started_at=utcnow(),
            workdir_path=str(work_dir),
            brief_path=str(brief_path),
        )

        try:
            if self._is_cancel_requested(analysis_id, conn):
                return

            cfg = get_pr_analysis_config(conn)
            agent = cfg["agent"]
            model = cfg["model"]
            effort = cfg["effort"] or None
            adapter = get_adapter(agent)
            if not adapter.available():
                raise RuntimeError(f"Agent {agent} binary not available on PATH")

            with hold_worktree(self.settings, pr, head_sha, conn=conn) as worktree:
                if self._is_cancel_requested(analysis_id, conn):
                    return

                prompt_text = render_analysis_prompt(
                    cfg["prompt"],
                    pr=pr,
                    brief_path=brief_path,
                    worktree=worktree,
                )
                prompt_path.write_text(prompt_text, encoding="utf-8")

                built = build_consolidate_command(
                    agent=agent,
                    model=model,
                    effort=effort,
                    prompt=prompt_text,
                    work_dir=worktree,
                )
                cli_path.write_text(
                    json.dumps(
                        {
                            "argv": built.argv,
                            "stdin": bool(built.stdin_text),
                            "cwd": str(worktree),
                            "agent": agent,
                            "model": model,
                            "effort": effort,
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                update_pr_analysis(
                    conn,
                    analysis_id,
                    agent=agent,
                    model=model,
                    effort=effort,
                    cli_command=json.dumps(built.argv),
                )

                if self._is_cancel_requested(analysis_id, conn):
                    return

                returncode = self._run_process(
                    analysis_id,
                    built.argv,
                    cwd=worktree,
                    stdout_path=stdout_path,
                    stderr_path=stderr_path,
                    conn=conn,
                    stdin_text=built.stdin_text,
                )

                duration_ms = int((time.monotonic() - started) * 1000)
                finished_at = utcnow()

                if self._is_cancel_requested(analysis_id, conn):
                    self._mark_terminal(
                        conn,
                        analysis_id,
                        status="cancelled",
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        error_message="Cancelled by user",
                    )
                    return

                brief = ""
                if brief_path.exists():
                    brief = brief_path.read_text(encoding="utf-8", errors="replace")
                if returncode != 0 or not brief.strip():
                    stderr_text = (
                        stderr_path.read_text(encoding="utf-8", errors="replace")
                        if stderr_path.exists()
                        else ""
                    )
                    stdout_text = (
                        stdout_path.read_text(encoding="utf-8", errors="replace")
                        if stdout_path.exists()
                        else ""
                    )
                    err = (stderr_text or stdout_text or f"exit {returncode}")[-2000:]
                    if returncode == 0 and not brief.strip():
                        err = "Analysis succeeded but brief.md was missing or empty"
                    self._mark_terminal(
                        conn,
                        analysis_id,
                        status="failed",
                        finished_at=finished_at,
                        duration_ms=duration_ms,
                        error_message=err,
                        brief_markdown=brief or None,
                    )
                    return

                self._mark_terminal(
                    conn,
                    analysis_id,
                    status="succeeded",
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    brief_markdown=brief,
                    brief_path=str(brief_path),
                    error_message=None,
                )
        except Exception as e:
            if self._is_cancel_requested(analysis_id, conn):
                self._mark_terminal(
                    conn,
                    analysis_id,
                    status="cancelled",
                    finished_at=utcnow(),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_message="Cancelled by user",
                )
                return
            self._mark_terminal(
                conn,
                analysis_id,
                status="failed",
                finished_at=utcnow(),
                duration_ms=int((time.monotonic() - started) * 1000),
                error_message=str(e)[-2000:],
            )
            raise

    def _run_process(
        self,
        analysis_id: int,
        cmd: list[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        conn: sqlite3.Connection,
        stdin_text: str | None = None,
    ) -> int:
        timeout = int(getattr(self.settings, "job_timeout_seconds", 3600) or 3600)
        deadline = time.monotonic() + timeout if timeout else None

        argv = list(cmd)
        if shutil.which("stdbuf"):
            argv = ["stdbuf", "-oL", "-eL", *argv]

        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        with self._lock:
            self._procs[analysis_id] = proc

        def _pump(stream: Any, path: Path) -> None:
            if stream is None:
                return
            try:
                with path.open("w", encoding="utf-8", buffering=1) as out:
                    for line in stream:
                        out.write(line)
                        out.flush()
            except Exception:
                logger.exception("log pump failed for %s", path)

        t_out = threading.Thread(
            target=_pump,
            args=(proc.stdout, stdout_path),
            name=f"lr-aout-{analysis_id}",
            daemon=True,
        )
        t_err = threading.Thread(
            target=_pump,
            args=(proc.stderr, stderr_path),
            name=f"lr-aerr-{analysis_id}",
            daemon=True,
        )
        t_out.start()
        t_err.start()

        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except OSError:
                logger.exception("failed writing stdin for analysis %s", analysis_id)

        while proc.poll() is None:
            if self._is_cancel_requested(analysis_id, conn):
                self._terminate(proc)
                proc.wait(timeout=30)
                break
            if deadline is not None and time.monotonic() > deadline:
                self._terminate(proc)
                proc.wait(timeout=30)
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                raise TimeoutError(f"Analysis exceeded {timeout}s timeout")
            time.sleep(0.4)

        t_out.join(timeout=30)
        t_err.join(timeout=30)
        return int(proc.returncode if proc.returncode is not None else -1)

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        except OSError:
            logger.exception("terminate failed")
