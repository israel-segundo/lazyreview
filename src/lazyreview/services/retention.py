"""Run retention sweeper."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from lazyreview.config import Settings
from lazyreview.db import (
    delete_chat_session,
    delete_run,
    get_setting,
    list_purgeable_chat_sessions,
    list_purgeable_runs,
)
from lazyreview.services.session_jobs import sessions_dir

logger = logging.getLogger(__name__)


def retention_timedelta(value: int, unit: str) -> timedelta:
    unit = unit.lower()
    if unit == "hours":
        return timedelta(hours=value)
    if unit == "days":
        return timedelta(days=value)
    if unit == "weeks":
        return timedelta(weeks=value)
    raise ValueError(f"Unknown retention unit: {unit}")


def cutoff_iso(value: int, unit: str) -> str:
    cutoff = datetime.now(timezone.utc) - retention_timedelta(value, unit)
    return cutoff.replace(microsecond=0).isoformat()


def purge_old_runs(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    raw_val = get_setting(conn, "run_retention_value", "1") or "1"
    unit = get_setting(conn, "run_retention_unit", "weeks") or "weeks"
    try:
        value = max(1, int(raw_val))
    except ValueError:
        value = 1
    cutoff = cutoff_iso(value, unit)
    runs = list_purgeable_runs(conn, cutoff)
    deleted = 0
    freed_dirs = 0
    for run in runs:
        workdir = run.get("workdir_path")
        delete_run(conn, int(run["id"]))
        deleted += 1
        if workdir:
            p = Path(workdir)
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                freed_dirs += 1

    # Orphan dirs with no DB row
    orphans = 0
    if settings.runs_dir.exists():
        known = {
            str(r["id"])
            for r in conn.execute("SELECT id FROM review_runs").fetchall()
        }
        known |= {
            str(r["id"])
            for r in conn.execute(
                "SELECT id FROM review_runs WHERE status IN ('queued', 'running')"
            ).fetchall()
        }
        for child in settings.runs_dir.iterdir():
            if not child.is_dir():
                continue
            if child.name not in known:
                # Only remove if clearly a numeric run id and not referenced
                if child.name.isdigit():
                    row = conn.execute(
                        "SELECT id FROM review_runs WHERE id = ?", (int(child.name),)
                    ).fetchone()
                    if row is None:
                        shutil.rmtree(child, ignore_errors=True)
                        orphans += 1

    # Interactive chat sessions (same retention window)
    sessions = list_purgeable_chat_sessions(conn, cutoff)
    deleted_sessions = 0
    freed_session_dirs = 0
    for sess in sessions:
        workdir = sess.get("workdir_path")
        delete_chat_session(conn, int(sess["id"]))
        deleted_sessions += 1
        if workdir:
            p = Path(workdir)
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                freed_session_dirs += 1

    session_orphans = 0
    sdir = sessions_dir(settings)
    if sdir.exists():
        known_sess = {
            str(r["id"])
            for r in conn.execute("SELECT id FROM chat_sessions").fetchall()
        }
        for child in sdir.iterdir():
            if not child.is_dir() or not child.name.isdigit():
                continue
            if child.name not in known_sess:
                shutil.rmtree(child, ignore_errors=True)
                session_orphans += 1

    return {
        "deleted_runs": deleted,
        "removed_dirs": freed_dirs,
        "orphan_dirs": orphans,
        "deleted_sessions": deleted_sessions,
        "removed_session_dirs": freed_session_dirs,
        "orphan_session_dirs": session_orphans,
        "cutoff": cutoff,
        "policy": f"{value} {unit}",
    }


class RetentionSweeper:
    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        interval_seconds: int = 3600,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lazyreview-retention", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        # Small delay after startup
        if self._stop.wait(15):
            return
        while not self._stop.is_set():
            try:
                result = purge_old_runs(self.conn, self.settings)
                if result["deleted_runs"] or result["orphan_dirs"]:
                    logger.info("retention sweep: %s", result)
            except Exception:
                logger.exception("retention sweep failed")
            if self._stop.wait(self.interval):
                return
