"""Shared PR checkouts: bare mirror per GitHub repo + worktree per PR."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lazyreview.config import Settings
from lazyreview.services.gh import GhError, clone_url

logger = logging.getLogger(__name__)

_locks_guard = threading.Lock()
_pr_locks: dict[int, threading.RLock] = {}
_refcounts: dict[int, int] = {}


def workspaces_dir(settings: Settings) -> Path:
    path = settings.data_dir / "workspaces"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mirrors_dir(settings: Settings) -> Path:
    path = workspaces_dir(settings) / "mirrors"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prs_worktrees_dir(settings: Settings) -> Path:
    path = workspaces_dir(settings) / "prs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mirror_key(owner: str, repo: str) -> str:
    safe_owner = owner.replace("/", "_")
    safe_repo = repo.replace("/", "_")
    return f"{safe_owner}__{safe_repo}"


def mirror_path(settings: Settings, owner: str, repo: str) -> Path:
    return mirrors_dir(settings) / f"{mirror_key(owner, repo)}.git"


def worktree_path(settings: Settings, pr_id: int) -> Path:
    return prs_worktrees_dir(settings) / str(pr_id)


def _pr_lock(pr_id: int) -> threading.RLock:
    with _locks_guard:
        lock = _pr_locks.get(pr_id)
        if lock is None:
            lock = threading.RLock()
            _pr_locks[pr_id] = lock
        return lock


def _run_git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )


def ensure_mirror(settings: Settings, owner: str, repo: str) -> Path:
    """Create or update a bare mirror for owner/repo. Returns mirror path."""
    dest = mirror_path(settings, owner, repo)
    url = clone_url(owner, repo)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        proc = _run_git(["clone", "--mirror", url, str(dest)])
        if proc.returncode != 0:
            raise GhError(
                f"git clone --mirror failed: {(proc.stderr or proc.stdout).strip()}"
            )
        return dest
    proc = _run_git(["remote", "update", "--prune"], cwd=dest)
    if proc.returncode != 0:
        # Non-fatal if update fails temporarily; callers may still have objects.
        logger.warning(
            "mirror update failed for %s/%s: %s",
            owner,
            repo,
            (proc.stderr or proc.stdout).strip(),
        )
    return dest


def _checkout_sha(worktree: Path, head_sha: str) -> None:
    fetch = _run_git(["fetch", "origin", head_sha], cwd=worktree)
    if fetch.returncode != 0:
        _run_git(["fetch", "origin", "--tags", "--force"], cwd=worktree)
    checkout = _run_git(["checkout", "--force", head_sha], cwd=worktree)
    if checkout.returncode != 0:
        raise GhError(
            f"git checkout {head_sha} failed: "
            f"{(checkout.stderr or checkout.stdout).strip()}"
        )


def ensure_pr_worktree(
    settings: Settings,
    pr: dict[str, Any],
    head_sha: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> Path:
    """Ensure a worktree for the PR exists at head_sha. Returns worktree path."""
    pr_id = int(pr["id"])
    owner = str(pr["owner"])
    repo = str(pr["repo"])
    if not head_sha:
        raise GhError("head_sha is required for PR worktree")

    with _pr_lock(pr_id):
        mirror = ensure_mirror(settings, owner, repo)
        dest = worktree_path(settings, pr_id)
        if dest.exists() and (dest / ".git").exists():
            _checkout_sha(dest, head_sha)
        else:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            add = _run_git(
                ["worktree", "add", "--force", "--detach", str(dest), head_sha],
                cwd=mirror,
            )
            if add.returncode != 0:
                # Fetch sha into mirror then retry.
                _run_git(["fetch", "origin", head_sha], cwd=mirror)
                add = _run_git(
                    ["worktree", "add", "--force", "--detach", str(dest), head_sha],
                    cwd=mirror,
                )
            if add.returncode != 0:
                raise GhError(
                    f"git worktree add failed: {(add.stderr or add.stdout).strip()}"
                )
            _checkout_sha(dest, head_sha)

        if conn is not None:
            conn.execute(
                """
                UPDATE prs
                SET workspace_path = ?,
                    workspace_status = 'active',
                    workspace_delete_after = NULL,
                    mirror_key = ?
                WHERE id = ?
                """,
                (str(dest), mirror_key(owner, repo), pr_id),
            )
            conn.commit()
        return dest


def refresh_pr_worktree(
    settings: Settings,
    pr: dict[str, Any],
    head_sha: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> Path:
    """Fetch and reset the PR worktree to head_sha."""
    return ensure_pr_worktree(settings, pr, head_sha, conn=conn)


def acquire_worktree(pr_id: int) -> None:
    with _locks_guard:
        _refcounts[pr_id] = _refcounts.get(pr_id, 0) + 1


def release_worktree(pr_id: int) -> None:
    with _locks_guard:
        cur = _refcounts.get(pr_id, 0)
        if cur <= 1:
            _refcounts.pop(pr_id, None)
        else:
            _refcounts[pr_id] = cur - 1


def worktree_in_use(pr_id: int) -> bool:
    with _locks_guard:
        return _refcounts.get(pr_id, 0) > 0


@contextmanager
def hold_worktree(
    settings: Settings,
    pr: dict[str, Any],
    head_sha: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> Iterator[Path]:
    """Ensure worktree at head_sha and hold a refcount for the duration."""
    pr_id = int(pr["id"])
    path = ensure_pr_worktree(settings, pr, head_sha, conn=conn)
    acquire_worktree(pr_id)
    try:
        yield path
    finally:
        release_worktree(pr_id)


def queue_workspace_deletion(conn: sqlite3.Connection, pr_id: int) -> None:
    """Mark the PR workspace for async deletion (terminal PR)."""
    conn.execute(
        """
        UPDATE prs
        SET workspace_status = 'pending_delete',
            workspace_delete_after = datetime('now')
        WHERE id = ?
        """,
        (pr_id,),
    )
    conn.commit()


def _remove_worktree(settings: Settings, pr: dict[str, Any]) -> None:
    pr_id = int(pr["id"])
    dest = Path(pr["workspace_path"] or worktree_path(settings, pr_id))
    owner = str(pr.get("owner") or "")
    repo = str(pr.get("repo") or "")
    mirror = mirror_path(settings, owner, repo) if owner and repo else None

    with _pr_lock(pr_id):
        if worktree_in_use(pr_id):
            return
        if dest.exists():
            if mirror and mirror.exists():
                removed = _run_git(["worktree", "remove", "--force", str(dest)], cwd=mirror)
                if removed.returncode != 0:
                    shutil.rmtree(dest, ignore_errors=True)
                    _run_git(["worktree", "prune"], cwd=mirror)
            else:
                shutil.rmtree(dest, ignore_errors=True)


def prune_queued_workspaces(settings: Settings, conn: sqlite3.Connection) -> int:
    """Delete worktrees marked pending_delete when idle. Returns count removed."""
    rows = conn.execute(
        """
        SELECT * FROM prs
        WHERE workspace_status = 'pending_delete'
        ORDER BY id ASC
        """
    ).fetchall()
    removed = 0
    for raw in rows:
        pr = dict(raw)
        pr_id = int(pr["id"])
        if worktree_in_use(pr_id):
            continue
        try:
            _remove_worktree(settings, pr)
            conn.execute(
                """
                UPDATE prs
                SET workspace_status = 'deleted',
                    workspace_path = NULL,
                    workspace_delete_after = NULL
                WHERE id = ?
                """,
                (pr_id,),
            )
            conn.commit()
            removed += 1
        except Exception:
            logger.exception("failed to prune workspace for pr %s", pr_id)
    return removed


class WorkspacePruner:
    """Background sweeper for pending workspace deletions."""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        *,
        interval_seconds: int = 60,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.interval_seconds = max(5, int(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="lazyreview-workspace-pruner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                prune_queued_workspaces(self.settings, self.conn)
            except Exception:
                logger.exception("workspace prune failed")
            self._stop.wait(self.interval_seconds)
