"""Background review job runner."""

from __future__ import annotations

import json
import logging
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
    get_run,
    replace_findings,
    update_run,
    utcnow,
)
from lazyreview.db.schema import connect
from lazyreview.services.clone import clone_pr_head, remove_repo_dir
from lazyreview.services.pr_workspace import hold_worktree
from lazyreview.services.findings_parse import load_findings_from_workdir, severity_counts
from lazyreview.services.prepare import collect_review_artifacts, prepare_review_workspace

logger = logging.getLogger(__name__)


class JobRunner:
    def __init__(self, settings: Settings, conn: sqlite3.Connection) -> None:
        self.settings = settings
        # Main-thread / API connection — never touch from worker threads.
        self.conn = conn
        self._queue: Queue[int] = Queue()
        self._dispatcher: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cancel_requested: set[int] = set()
        self._procs: dict[int, subprocess.Popen[str]] = {}
        self._inflight: set[int] = set()
        # Optional hook: consolidation observer wakes when a review run ends.
        self.on_run_finished: Any | None = None

    @property
    def max_workers(self) -> int:
        return max(1, int(self.settings.max_concurrent_jobs))

    def _worker_conn(self) -> sqlite3.Connection:
        """Dedicated SQLite connection for a job worker thread."""
        return connect(self.settings.db_path)

    def start(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        self._stop.clear()
        workers = self.max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="lazyreview-job",
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="lazyreview-jobs-dispatch",
            daemon=True,
        )
        self._dispatcher.start()
        # After a daemon restart, in-flight agent processes are gone but rows may
        # still say "running". Reclaim them so the UI and dormant consolidations
        # are not blocked forever.
        orphaned = self.conn.execute(
            "SELECT id FROM review_runs WHERE status = 'running' ORDER BY id ASC"
        ).fetchall()
        for r in orphaned:
            rid = int(r["id"])
            update_run(
                self.conn,
                rid,
                status="failed",
                finished_at=utcnow(),
                error_message="Interrupted by daemon restart (orphaned running job)",
            )
            logger.warning("marked orphaned running run %s as failed", rid)
            self._notify_run_finished(rid)
        rows = self.conn.execute(
            "SELECT id FROM review_runs WHERE status = 'queued' ORDER BY id ASC"
        ).fetchall()
        for r in rows:
            self._queue.put(int(r["id"]))
        logger.info(
            "job runner started max_concurrent_jobs=%s queued=%s reclaimed_running=%s",
            workers,
            len(rows),
            len(orphaned),
        )

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for proc in list(self._procs.values()):
                if proc.poll() is None:
                    proc.terminate()
        if self._dispatcher:
            self._dispatcher.join(timeout=5)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def enqueue(self, run_id: int) -> None:
        self._queue.put(run_id)

    def cancel(self, run_id: int) -> dict[str, Any]:
        """Cancel a queued or running review. Returns the updated run row."""
        with self._lock:
            self._cancel_requested.add(run_id)
            proc = self._procs.get(run_id)
        run = get_run(self.conn, run_id)
        if not run:
            raise KeyError(run_id)
        status = run["status"]
        if status not in ("queued", "running"):
            raise ValueError(f"Run is {status}, cannot cancel")
        updated = update_run(
            self.conn,
            run_id,
            status="cancelled",
            finished_at=utcnow(),
            error_message="Cancelled by user",
        )
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                logger.exception("failed to terminate run %s", run_id)
        assert updated is not None
        self._notify_run_finished(run_id)
        return updated


    def cancel_for_pr(self, pr_id: int) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT id FROM review_runs
            WHERE pr_id = ? AND status IN ('queued', 'running')
            ORDER BY id ASC
            """,
            (pr_id,),
        ).fetchall()
        cancelled: list[int] = []
        for r in rows:
            rid = int(r["id"])
            try:
                self.cancel(rid)
                cancelled.append(rid)
            except (KeyError, ValueError):
                continue
        return cancelled

    def _notify_run_finished(self, run_id: int) -> None:
        hook = self.on_run_finished
        if hook is None:
            return
        try:
            hook(run_id)
        except Exception:
            logger.exception("on_run_finished hook failed for run %s", run_id)

    def _is_cancel_requested(self, run_id: int, conn: sqlite3.Connection | None = None) -> bool:
        with self._lock:
            if run_id in self._cancel_requested:
                return True
        own = conn is None
        c = conn or self._worker_conn()
        try:
            run = get_run(c, run_id)
            return bool(run and run["status"] == "cancelled")
        except sqlite3.Error:
            logger.exception("cancel check failed for run %s", run_id)
            return False
        finally:
            if own:
                c.close()

    def _dispatch_loop(self) -> None:
        """Pull queued run ids and submit them up to max_concurrent_jobs."""
        assert self._pool is not None
        while not self._stop.is_set():
            with self._lock:
                slots = self.max_workers - len(self._inflight)
            if slots <= 0:
                time.sleep(0.2)
                continue
            try:
                run_id = self._queue.get(timeout=0.5)
            except Empty:
                continue
            with self._lock:
                if run_id in self._inflight:
                    # Already running (e.g. duplicate enqueue) — skip.
                    self._queue.task_done()
                    continue
                self._inflight.add(run_id)
            self._pool.submit(self._run_job, run_id)

    def _run_job(self, run_id: int) -> None:
        try:
            self._execute(run_id)
        except Exception:
            logger.exception("run %s crashed", run_id)
            if not self._is_cancel_requested(run_id):
                conn = self._worker_conn()
                try:
                    update_run(
                        conn,
                        run_id,
                        status="failed",
                        finished_at=utcnow(),
                        error_message=traceback.format_exc()[-2000:],
                    )
                finally:
                    conn.close()
        finally:
            with self._lock:
                self._procs.pop(run_id, None)
                self._cancel_requested.discard(run_id)
                self._inflight.discard(run_id)
            self._queue.task_done()
            self._notify_run_finished(run_id)

    def _execute(self, run_id: int) -> None:
        conn = self._worker_conn()
        try:
            self._execute_with_conn(run_id, conn)
        finally:
            conn.close()

    def _execute_with_conn(self, run_id: int, conn: sqlite3.Connection) -> None:
        run = get_run(conn, run_id)
        if not run:
            return
        if run["status"] not in ("queued", "running"):
            return
        if self._is_cancel_requested(run_id, conn):
            return

        pr = get_pr(conn, int(run["pr_id"]))
        if not pr:
            update_run(
                conn,
                run_id,
                status="failed",
                finished_at=utcnow(),
                error_message="PR not found",
            )
            return

        run_dir = Path(run["workdir_path"] or self.settings.runs_dir / str(run_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "artifacts").mkdir(exist_ok=True)
        repo_dir = run_dir / "repo"
        _shared_worktree = False
        _wt_cm = None
        prompt_path = run_dir / "prompt.md"
        stdout_path = run_dir / "stdout.log"
        stderr_path = run_dir / "stderr.log"
        cli_path = run_dir / "cli.json"
        meta_path = run_dir / "meta.json"

        started = time.monotonic()
        started_at = utcnow()
        update_run(
            conn,
            run_id,
            status="running",
            started_at=started_at,
            workdir_path=str(run_dir),
            prompt_path=str(prompt_path),
            log_stdout_path=str(stdout_path),
            log_stderr_path=str(stderr_path),
        )

        try:
            if self._is_cancel_requested(run_id, conn):
                return

            # Shared PR worktree (no throwaway per-run clone).
            _wt_cm = hold_worktree(
                self.settings,
                pr,
                run["head_sha"] or pr["head_sha"],
                conn=conn,
            )
            repo_dir = _wt_cm.__enter__()
            _shared_worktree = True
            if self._is_cancel_requested(run_id, conn):
                return

            skill = run["skill"] or "pr-code-review-batch"
            prepared = prepare_review_workspace(
                repo_dir=repo_dir,
                run_dir=run_dir,
                skill=skill,
            )
            adapter = get_adapter(run["agent"])
            from lazyreview.agents.base import ReviewContext

            ctx = ReviewContext(
                pr_url=pr["url"],
                repo_dir=repo_dir,
                run_dir=run_dir,
                prompt_path=prompt_path,
                model=run["model"],
                effort=run["effort"],
                skill=skill,
                head_sha=run["head_sha"] or pr["head_sha"],
                findings_path=prepared.findings_path,
                report_path=prepared.report_path,
                skills_installed=prepared.skills_installed,
            )
            built = adapter.build_review_command(ctx)
            cli_path.write_text(
                json.dumps(
                    {
                        "argv": built.argv,
                        "stdin": bool(built.stdin_text),
                        "cwd": str(repo_dir),
                        "agent": run["agent"],
                        "model": run["model"],
                        "effort": run["effort"],
                        "skill": skill,
                        "skills_installed": prepared.skills_installed,
                        "findings_path": str(prepared.findings_path),
                        "report_path": str(prepared.report_path),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            update_run(conn, run_id, cli_command=json.dumps(built.argv))

            if self._is_cancel_requested(run_id, conn):
                return

            returncode = self._run_process(
                run_id,
                built.argv,
                cwd=repo_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                conn=conn,
                stdin_text=built.stdin_text,
            )

            if self._is_cancel_requested(run_id, conn):
                duration_ms = int((time.monotonic() - started) * 1000)
                update_run(
                    conn,
                    run_id,
                    status="cancelled",
                    finished_at=utcnow(),
                    duration_ms=duration_ms,
                    error_message="Cancelled by user",
                )
                return

            collect_review_artifacts(prepared)

            stdout_text = (
                stdout_path.read_text(encoding="utf-8", errors="replace")
                if stdout_path.exists()
                else ""
            )
            parsed = adapter.parse_result(run_dir, stdout_text)
            if parsed.findings_md and not (run_dir / "findings.md").exists():
                (run_dir / "findings.md").write_text(parsed.findings_md, encoding="utf-8")
            if parsed.report_html and not (run_dir / "report.html").exists():
                (run_dir / "report.html").write_text(parsed.report_html, encoding="utf-8")

            findings = load_findings_from_workdir(run_dir)
            s1, s2, s3 = severity_counts(findings)
            replace_findings(conn, run_id, findings)

            duration_ms = int((time.monotonic() - started) * 1000)
            finished_at = utcnow()
            status = "succeeded" if returncode == 0 else "failed"
            err = None
            if returncode != 0:
                stderr_text = (
                    stderr_path.read_text(encoding="utf-8", errors="replace")
                    if stderr_path.exists()
                    else ""
                )
                err = (stderr_text or stdout_text or f"exit {returncode}")[-2000:]

            cost_usd = parsed.cost_usd
            if cost_usd is None:
                from lazyreview.services.cost import estimate_cost_usd

                cost_usd = estimate_cost_usd(
                    model=str(run["model"] or ""),
                    input_tokens=parsed.input_tokens,
                    output_tokens=parsed.output_tokens,
                )

            meta: dict[str, Any] = {
                "exit_code": returncode,
                "duration_ms": duration_ms,
                "head_sha": ctx.head_sha,
                "findings": len(findings),
                "skills_installed": prepared.skills_installed,
                "tokens": {
                    "input": parsed.input_tokens,
                    "output": parsed.output_tokens,
                    "total": parsed.total_tokens,
                },
                "cost_usd": cost_usd,
            }
            meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

            if self._is_cancel_requested(run_id, conn):
                update_run(
                    conn,
                    run_id,
                    status="cancelled",
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    error_message="Cancelled by user",
                )
                return

            update_run(
                conn,
                run_id,
                status=status,
                finished_at=finished_at,
                duration_ms=duration_ms,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                total_tokens=parsed.total_tokens,
                cost_usd=cost_usd,
                findings_sev1=s1,
                findings_sev2=s2,
                findings_sev3=s3,
                report_md_path=str(run_dir / "findings.md")
                if (run_dir / "findings.md").exists()
                else None,
                report_html_path=str(run_dir / "report.html")
                if (run_dir / "report.html").exists()
                else None,
                error_message=err,
            )
        except Exception as e:
            if self._is_cancel_requested(run_id, conn):
                duration_ms = int((time.monotonic() - started) * 1000)
                update_run(
                    conn,
                    run_id,
                    status="cancelled",
                    finished_at=utcnow(),
                    duration_ms=duration_ms,
                    error_message="Cancelled by user",
                )
                return
            duration_ms = int((time.monotonic() - started) * 1000)
            update_run(
                conn,
                run_id,
                status="failed",
                finished_at=utcnow(),
                duration_ms=duration_ms,
                error_message=str(e)[-2000:],
            )
            raise
        finally:
            if _shared_worktree and _wt_cm is not None:
                _wt_cm.__exit__(None, None, None)
            elif not _shared_worktree:
                remove_repo_dir(repo_dir)

    def _run_process(
        self,
        run_id: int,
        cmd: list[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        conn: sqlite3.Connection,
        stdin_text: str | None = None,
    ) -> int:
        """Start the agent CLI, stream logs to disk, honor cancel + timeout."""
        timeout = self.settings.job_timeout_seconds
        deadline = time.monotonic() + timeout if timeout else None

        # Prefer line-buffered stdio so NDJSON events reach the log files promptly.
        # (Many CLIs fully buffer when stdout is a plain file.)
        import shutil

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
            self._procs[run_id] = proc

        def _pump(stream, path: Path) -> None:
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
            target=_pump, args=(proc.stdout, stdout_path), name=f"lr-out-{run_id}", daemon=True
        )
        t_err = threading.Thread(
            target=_pump, args=(proc.stderr, stderr_path), name=f"lr-err-{run_id}", daemon=True
        )
        t_out.start()
        t_err.start()

        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except OSError:
                logger.exception("failed writing stdin for run %s", run_id)

        while proc.poll() is None:
            if self._is_cancel_requested(run_id, conn):
                self._terminate(proc)
                proc.wait(timeout=30)
                break
            if deadline is not None and time.monotonic() > deadline:
                self._terminate(proc)
                proc.wait(timeout=30)
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                raise TimeoutError(f"Job exceeded {timeout}s timeout")
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
