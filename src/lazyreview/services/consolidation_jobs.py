"""Background consolidation job runner."""

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
from lazyreview.agents.base import BuiltCommand, which_binary
from lazyreview.config import Settings
from lazyreview.db import (
    get_consolidation,
    get_pr,
    get_run,
    list_findings,
    mark_runs_consolidated,
    update_consolidation,
    utcnow,
)
from lazyreview.db.schema import connect
from lazyreview.services.gh import GhError, list_pr_comments
from lazyreview.services.settings import get_consolidator_config

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def consolidations_dir(settings: Settings) -> Path:
    path = settings.data_dir / "consolidations"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_dormant_consolidations_for_run(
    conn: sqlite3.Connection, run_id: int
) -> list[dict[str, Any]]:
    """Return dormant consolidations that list this review run as a source."""
    rows = conn.execute(
        "SELECT * FROM consolidations WHERE status = 'dormant' ORDER BY id ASC"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        try:
            ids = [int(x) for x in json.loads(row.get("source_run_ids") or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if int(run_id) not in ids:
            continue
        row["source_run_ids"] = ids
        out.append(row)
    return out


def try_activate_dormant_consolidation(
    conn: sqlite3.Connection, consolidation_id: int
) -> dict[str, Any] | None:
    """
    If every source run is finished, promote dormant → queued (or cancel if none succeeded).

    Uses an atomic status flip so concurrent observers only enqueue once.
    Returns the updated row when this caller won the flip; otherwise None.
    """
    row = get_consolidation(conn, consolidation_id)
    if not row or row.get("status") != "dormant":
        return None

    source_ids = [int(x) for x in (row.get("source_run_ids") or [])]
    if not source_ids:
        cur = conn.execute(
            """
            UPDATE consolidations
            SET status = 'cancelled', finished_at = ?, error_message = ?
            WHERE id = ? AND status = 'dormant'
            """,
            (utcnow(), "No source runs to consolidate", consolidation_id),
        )
        conn.commit()
        return get_consolidation(conn, consolidation_id) if cur.rowcount else None

    runs_by_id: dict[int, dict[str, Any] | None] = {
        rid: get_run(conn, rid) for rid in source_ids
    }
    # Wait while any source run is still queued/running (or otherwise non-terminal).
    for rid in source_ids:
        run = runs_by_id[rid]
        if run is None:
            continue
        if str(run.get("status") or "") not in _TERMINAL_RUN_STATUSES:
            return None

    # All known runs are terminal (or deleted). Prefer successful, eligible runs.
    succeeded_ids = [
        rid
        for rid in source_ids
        if (r := runs_by_id[rid]) is not None
        and str(r.get("status")) == "succeeded"
        and not int(r.get("consolidated") or 0)
        and not int(r.get("deprecated") or 0)
    ]
    if not succeeded_ids:
        cur = conn.execute(
            """
            UPDATE consolidations
            SET status = 'cancelled', finished_at = ?, error_message = ?
            WHERE id = ? AND status = 'dormant'
            """,
            (
                utcnow(),
                "No successful runs to consolidate after all source runs finished",
                consolidation_id,
            ),
        )
        conn.commit()
        return get_consolidation(conn, consolidation_id) if cur.rowcount else None

    cur = conn.execute(
        """
        UPDATE consolidations
        SET status = 'queued', source_run_ids = ?
        WHERE id = ? AND status = 'dormant'
        """,
        (json.dumps(succeeded_ids), consolidation_id),
    )
    conn.commit()
    if not cur.rowcount:
        return None
    return get_consolidation(conn, consolidation_id)


def build_consolidate_command(
    *,
    agent: str,
    model: str,
    effort: str | None,
    prompt: str,
    work_dir: Path,
) -> BuiltCommand:
    """Build a print-mode CLI invocation for consolidation (no PR clone)."""
    adapter = get_adapter(agent)
    path = which_binary(adapter.binary)
    if not path:
        raise RuntimeError(f"{adapter.binary} not found on PATH")
    model_args = adapter.resolve_model_args(model, effort or None)
    work = str(work_dir)

    if agent == "claude":
        return BuiltCommand(
            argv=[
                path,
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--dangerously-skip-permissions",
                "--permission-mode",
                "bypassPermissions",
                *model_args,
                "--allowedTools",
                "Read,Glob,Grep,Write,Edit,Bash(gh *)",
                "--add-dir",
                work,
            ],
            stdin_text=prompt,
        )
    if agent == "agy":
        return BuiltCommand(
            argv=[
                path,
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--mode",
                "accept-edits",
                "--dangerously-skip-permissions",
                *model_args,
                "--add-dir",
                work,
            ],
        )
    if agent == "cursor":
        return BuiltCommand(
            argv=[
                path,
                "-p",
                "--force",
                "--trust",
                "--workspace",
                work,
                "--add-dir",
                work,
                "--output-format",
                "stream-json",
                "--stream-partial-output",
                *model_args,
                prompt,
            ]
        )
    raise RuntimeError(f"Unsupported consolidator agent: {agent}")


def render_consolidator_prompt(
    template: str,
    *,
    body_path: Path,
    findings_json_path: Path,
    source_runs_label: str,
    pr_url: str = "",
) -> str:
    return (
        template.replace("{body_path}", str(body_path))
        .replace("{findings_json_path}", str(findings_json_path))
        .replace("{source_runs_label}", source_runs_label)
        .replace("{pr_url}", pr_url or "")
        # Back-compat for older saved prompts that still use {model_label}
        .replace("{model_label}", source_runs_label)
    )


def format_source_runs_label(findings_by_run: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in findings_by_run:
        run = item.get("run") or {}
        agent = (run.get("agent") or "?").strip()
        model = (run.get("model") or "?").strip()
        effort = (run.get("effort") or "").strip()
        label = f"{agent}/{model}"
        if effort:
            label = f"{label}/{effort}"
        parts.append(label)
    return ", ".join(parts) if parts else "(none)"



class ConsolidationRunner:
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
        return max(1, int(self.settings.max_concurrent_jobs))

    def _worker_conn(self) -> sqlite3.Connection:
        return connect(self.settings.db_path)

    def start(self) -> None:
        if self._dispatcher and self._dispatcher.is_alive():
            return
        self._stop.clear()
        workers = self.max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="lazyreview-consol",
        )
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop,
            name="lazyreview-consol-dispatch",
            daemon=True,
        )
        self._dispatcher.start()
        rows = self.conn.execute(
            "SELECT id FROM consolidations WHERE status = 'queued' ORDER BY id ASC"
        ).fetchall()
        for r in rows:
            self._queue.put(int(r["id"]))
        logger.info("consolidation runner started max_concurrent_jobs=%s", workers)

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

    def enqueue(self, consolidation_id: int) -> None:
        self._queue.put(consolidation_id)

    def notify_run_finished(self, run_id: int) -> None:
        """Observer: wake dormant consolidations that were waiting on this review run."""
        conn = self._worker_conn()
        try:
            for row in list_dormant_consolidations_for_run(conn, run_id):
                updated = try_activate_dormant_consolidation(conn, int(row["id"]))
                if not updated:
                    continue
                if updated.get("status") == "queued":
                    logger.info(
                        "dormant consolidation %s queued after run %s finished",
                        updated["id"],
                        run_id,
                    )
                    self.enqueue(int(updated["id"]))
                else:
                    logger.info(
                        "dormant consolidation %s ended as %s after run %s finished",
                        updated["id"],
                        updated.get("status"),
                        run_id,
                    )
        except Exception:
            logger.exception("failed waking dormant consolidations for run %s", run_id)
        finally:
            conn.close()

    def cancel(self, consolidation_id: int) -> dict[str, Any]:
        with self._lock:
            self._cancel_requested.add(consolidation_id)
            proc = self._procs.get(consolidation_id)
        row = get_consolidation(self.conn, consolidation_id)
        if not row:
            raise KeyError(consolidation_id)
        status = row["status"]
        if status not in ("dormant", "queued", "running"):
            raise ValueError(f"Consolidation is {status}, cannot cancel")
        updated = update_consolidation(
            self.conn,
            consolidation_id,
            status="cancelled",
            finished_at=utcnow(),
            error_message="Cancelled by user",
        )
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                logger.exception("failed to terminate consolidation %s", consolidation_id)
        assert updated is not None
        return updated

    def _is_cancel_requested(
        self, consolidation_id: int, conn: sqlite3.Connection | None = None
    ) -> bool:
        with self._lock:
            if consolidation_id in self._cancel_requested:
                return True
        own = conn is None
        c = conn or self._worker_conn()
        try:
            row = get_consolidation(c, consolidation_id)
            return bool(row and row["status"] == "cancelled")
        except sqlite3.Error:
            logger.exception("cancel check failed for consolidation %s", consolidation_id)
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
                time.sleep(0.2)
                continue
            try:
                consolidation_id = self._queue.get(timeout=0.5)
            except Empty:
                continue
            with self._lock:
                if consolidation_id in self._inflight:
                    self._queue.task_done()
                    continue
                self._inflight.add(consolidation_id)
            self._pool.submit(self._run_job, consolidation_id)

    def _run_job(self, consolidation_id: int) -> None:
        try:
            self._execute(consolidation_id)
        except Exception:
            logger.exception("consolidation %s crashed", consolidation_id)
            if not self._is_cancel_requested(consolidation_id):
                conn = self._worker_conn()
                try:
                    update_consolidation(
                        conn,
                        consolidation_id,
                        status="failed",
                        finished_at=utcnow(),
                        error_message=traceback.format_exc()[-2000:],
                    )
                finally:
                    conn.close()
        finally:
            with self._lock:
                self._procs.pop(consolidation_id, None)
                self._cancel_requested.discard(consolidation_id)
                self._inflight.discard(consolidation_id)
            self._queue.task_done()

    def _execute(self, consolidation_id: int) -> None:
        conn = self._worker_conn()
        try:
            self._execute_with_conn(consolidation_id, conn)
        finally:
            conn.close()

    def _execute_with_conn(self, consolidation_id: int, conn: sqlite3.Connection) -> None:
        row = get_consolidation(conn, consolidation_id)
        if not row:
            return
        if row["status"] not in ("queued", "running"):
            return
        if self._is_cancel_requested(consolidation_id, conn):
            return

        pr = get_pr(conn, int(row["pr_id"]))
        if not pr:
            update_consolidation(
                conn,
                consolidation_id,
                status="failed",
                finished_at=utcnow(),
                error_message="PR not found",
            )
            return

        work_dir = Path(
            row["workdir_path"] or consolidations_dir(self.settings) / str(consolidation_id)
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        body_path = work_dir / "body.md"
        findings_out = work_dir / "findings.json"
        prompt_path = work_dir / "prompt.md"
        stdout_path = work_dir / "stdout.log"
        stderr_path = work_dir / "stderr.log"
        cli_path = work_dir / "cli.json"

        started = time.monotonic()
        started_at = utcnow()
        update_consolidation(
            conn,
            consolidation_id,
            status="running",
            started_at=started_at,
            workdir_path=str(work_dir),
            prompt_path=str(prompt_path),
            log_stdout_path=str(stdout_path),
            log_stderr_path=str(stderr_path),
        )

        try:
            if self._is_cancel_requested(consolidation_id, conn):
                return

            cfg = get_consolidator_config(conn)
            # Always use live Settings at execution time (not values stamped when queued).
            agent = cfg["agent"]
            model = cfg["model"]
            effort = cfg["effort"] or None

            # Keep the row in sync so the UI reflects what actually ran.
            update_consolidation(
                conn,
                consolidation_id,
                agent=agent,
                model=model,
                effort=effort,
            )
            source_ids = list(row.get("source_run_ids") or [])
            findings_by_run: list[dict[str, Any]] = []
            for rid in source_ids:
                run = get_run(conn, int(rid))
                if not run:
                    continue
                findings_by_run.append(
                    {
                        "run": {
                            "id": run["id"],
                            "agent": run["agent"],
                            "model": run["model"],
                            "effort": run.get("effort"),
                            "head_sha": run.get("head_sha"),
                            "status": run.get("status"),
                        },
                        "findings": list_findings(conn, int(rid)),
                    }
                )

            try:
                comments = list_pr_comments(pr["owner"], pr["repo"], int(pr["number"]))
            except GhError as e:
                comments = []
                logger.warning("list_pr_comments failed for consolidation %s: %s", consolidation_id, e)

            (work_dir / "findings-by-run.json").write_text(
                json.dumps(findings_by_run, indent=2), encoding="utf-8"
            )
            (work_dir / "pr-comments.json").write_text(
                json.dumps(comments, indent=2), encoding="utf-8"
            )
            (work_dir / "pr.json").write_text(
                json.dumps(
                    {
                        "id": pr["id"],
                        "owner": pr["owner"],
                        "repo": pr["repo"],
                        "number": pr["number"],
                        "url": pr["url"],
                        "title": pr.get("title"),
                        "head_sha": pr.get("head_sha"),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            source_runs_label = format_source_runs_label(findings_by_run)
            prompt_text = render_consolidator_prompt(
                cfg["prompt"],
                body_path=body_path,
                findings_json_path=findings_out,
                source_runs_label=source_runs_label,
                pr_url=str(pr.get("url") or ""),
            )
            prompt_path.write_text(prompt_text, encoding="utf-8")

            built = build_consolidate_command(
                agent=agent,
                model=model,
                effort=effort,
                prompt=prompt_text,
                work_dir=work_dir,
            )
            cli_path.write_text(
                json.dumps(
                    {
                        "argv": built.argv,
                        "stdin": bool(built.stdin_text),
                        "cwd": str(work_dir),
                        "agent": agent,
                        "model": model,
                        "effort": effort,
                        "source_runs_label": source_runs_label,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            update_consolidation(conn, consolidation_id, cli_command=json.dumps(built.argv))

            if self._is_cancel_requested(consolidation_id, conn):
                return

            returncode = self._run_process(
                consolidation_id,
                built.argv,
                cwd=work_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                conn=conn,
                stdin_text=built.stdin_text,
            )

            duration_ms = int((time.monotonic() - started) * 1000)
            finished_at = utcnow()

            if self._is_cancel_requested(consolidation_id, conn):
                update_consolidation(
                    conn,
                    consolidation_id,
                    status="cancelled",
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    error_message="Cancelled by user",
                )
                return

            stdout_text = (
                stdout_path.read_text(encoding="utf-8", errors="replace")
                if stdout_path.exists()
                else ""
            )
            stderr_text = (
                stderr_path.read_text(encoding="utf-8", errors="replace")
                if stderr_path.exists()
                else ""
            )

            adapter = get_adapter(agent)
            parsed = adapter.parse_result(work_dir, stdout_text)

            body = body_path.read_text(encoding="utf-8") if body_path.exists() else ""
            findings_json = None
            if findings_out.exists():
                findings_json = findings_out.read_text(encoding="utf-8")

            cost_usd = parsed.cost_usd
            if cost_usd is None:
                from lazyreview.services.cost import estimate_cost_usd

                cost_usd = estimate_cost_usd(
                    model=str(model or ""),
                    input_tokens=parsed.input_tokens,
                    output_tokens=parsed.output_tokens,
                )

            if returncode != 0 or not body.strip():
                err = (stderr_text or stdout_text or f"exit {returncode}")[-2000:]
                if returncode == 0 and not body.strip():
                    err = "Consolidation succeeded but body.md was missing or empty"
                update_consolidation(
                    conn,
                    consolidation_id,
                    status="failed",
                    finished_at=finished_at,
                    duration_ms=duration_ms,
                    input_tokens=parsed.input_tokens,
                    output_tokens=parsed.output_tokens,
                    total_tokens=parsed.total_tokens,
                    cost_usd=cost_usd,
                    body=body or None,
                    findings_json=findings_json,
                    error_message=err,
                )
                return

            n_findings = 0
            if findings_json:
                try:
                    n_findings = len(json.loads(findings_json))
                except json.JSONDecodeError:
                    n_findings = 0
            dedup_summary = (
                f"Consolidated {len(source_ids)} runs → {n_findings} findings "
                f"({source_runs_label})"
            )

            update_consolidation(
                conn,
                consolidation_id,
                status="succeeded",
                finished_at=finished_at,
                duration_ms=duration_ms,
                body=body,
                findings_json=findings_json,
                dedup_summary=dedup_summary,
                input_tokens=parsed.input_tokens,
                output_tokens=parsed.output_tokens,
                total_tokens=parsed.total_tokens,
                cost_usd=cost_usd,
                error_message=None,
            )
            mark_runs_consolidated(conn, [int(x) for x in source_ids])
        except Exception as e:
            if self._is_cancel_requested(consolidation_id, conn):
                duration_ms = int((time.monotonic() - started) * 1000)
                update_consolidation(
                    conn,
                    consolidation_id,
                    status="cancelled",
                    finished_at=utcnow(),
                    duration_ms=duration_ms,
                    error_message="Cancelled by user",
                )
                return
            duration_ms = int((time.monotonic() - started) * 1000)
            update_consolidation(
                conn,
                consolidation_id,
                status="failed",
                finished_at=utcnow(),
                duration_ms=duration_ms,
                error_message=str(e)[-2000:],
            )
            raise

    def _run_process(
        self,
        consolidation_id: int,
        cmd: list[str],
        *,
        cwd: Path,
        stdout_path: Path,
        stderr_path: Path,
        conn: sqlite3.Connection,
        stdin_text: str | None = None,
    ) -> int:
        timeout = self.settings.job_timeout_seconds
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
            self._procs[consolidation_id] = proc

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
            target=_pump,
            args=(proc.stdout, stdout_path),
            name=f"lr-cout-{consolidation_id}",
            daemon=True,
        )
        t_err = threading.Thread(
            target=_pump,
            args=(proc.stderr, stderr_path),
            name=f"lr-cerr-{consolidation_id}",
            daemon=True,
        )
        t_out.start()
        t_err.start()

        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except OSError:
                logger.exception("failed writing stdin for consolidation %s", consolidation_id)

        while proc.poll() is None:
            if self._is_cancel_requested(consolidation_id, conn):
                self._terminate(proc)
                proc.wait(timeout=30)
                break
            if deadline is not None and time.monotonic() > deadline:
                self._terminate(proc)
                proc.wait(timeout=30)
                t_out.join(timeout=5)
                t_err.join(timeout=5)
                raise TimeoutError(f"Consolidation exceeded {timeout}s timeout")
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
