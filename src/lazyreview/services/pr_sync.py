"""Fetch and upsert PRs awaiting the current user's review."""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any

from lazyreview.db import (
    clear_stale_review_requests,
    get_setting,
    list_stale_review_request_prs,
    set_setting,
    upsert_pr,
    utcnow,
)
from lazyreview.services.gh import GhError, search_pending_review_prs, view_pr

logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL_MINUTES = 15


def get_pr_sync_interval_minutes(conn: sqlite3.Connection) -> int:
    raw = get_setting(conn, "pr_sync_interval_minutes", str(DEFAULT_SYNC_INTERVAL_MINUTES))
    try:
        return max(1, int(raw or DEFAULT_SYNC_INTERVAL_MINUTES))
    except ValueError:
        return DEFAULT_SYNC_INTERVAL_MINUTES


def _upsert_from_view(
    conn: sqlite3.Connection,
    data: dict[str, Any],
    *,
    review_requested: bool | None,
) -> dict[str, Any]:
    return upsert_pr(
        conn,
        owner=data["owner"],
        repo=data["repo"],
        number=data["number"],
        url=data["url"],
        title=data["title"],
        author=data.get("author") or "",
        base_ref=data["base_ref"],
        head_ref=data["head_ref"],
        head_sha=data["head_sha"],
        state=data.get("state") or "open",
        is_draft=bool(data.get("is_draft")),
        additions=data.get("additions"),
        deletions=data.get("deletions"),
        changed_files=data.get("changed_files"),
        review_requested=review_requested,
    )


def sync_pending_review_prs(conn: sqlite3.Connection) -> dict[str, Any]:
    """Pull open PRs where the authenticated user is a requested reviewer."""
    items = search_pending_review_prs()
    upserted: list[dict[str, Any]] = []
    errors: list[str] = []
    keys: list[tuple[str, str, int]] = []

    for item in items:
        owner = item["owner"]
        repo = item["repo"]
        number = int(item["number"])
        keys.append((owner, repo, number))
        try:
            data = view_pr(item["url"])
            pr = _upsert_from_view(conn, data, review_requested=True)
            upserted.append(pr)
        except GhError as e:
            try:
                pr = upsert_pr(
                    conn,
                    owner=owner,
                    repo=repo,
                    number=number,
                    url=item["url"],
                    title=item.get("title") or "",
                    base_ref="",
                    head_ref="",
                    head_sha="",
                    state="open",
                    review_requested=True,
                )
                upserted.append(pr)
                errors.append(f"{owner}/{repo}#{number}: enrich failed ({e})")
            except Exception as inner:
                errors.append(f"{owner}/{repo}#{number}: {inner}")

    # Refresh state for PRs that dropped out of the pending queue (often merged/closed)
    stale = list_stale_review_request_prs(conn, keys)
    for row in stale:
        try:
            data = view_pr(row["url"])
            _upsert_from_view(conn, data, review_requested=False)
        except GhError as e:
            upsert_pr(
                conn,
                owner=row["owner"],
                repo=row["repo"],
                number=int(row["number"]),
                url=row["url"],
                title=row.get("title") or "",
                base_ref=row.get("base_ref") or "",
                head_ref=row.get("head_ref") or "",
                head_sha=row.get("head_sha") or "",
                state=row.get("state") or "closed",
                review_requested=False,
            )
            errors.append(f"stale {row['owner']}/{row['repo']}#{row['number']}: {e}")

    cleared = clear_stale_review_requests(conn, keys)
    set_setting(conn, "pr_sync_last_at", utcnow())
    conn.commit()
    return {
        "fetched": len(items),
        "upserted": len(upserted),
        "cleared_stale": cleared,
        "prs": upserted,
        "errors": errors,
        "synced_at": get_setting(conn, "pr_sync_last_at"),
    }


class PrSyncer:
    """Periodically sync pending review PRs using the DB interval setting."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="lazyreview-pr-sync", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        """Interrupt the sleep so the next interval is re-read soon."""
        self._wake.set()

    def _loop(self) -> None:
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            try:
                result = sync_pending_review_prs(self.conn)
                logger.info(
                    "pr sync: fetched=%s upserted=%s cleared=%s",
                    result["fetched"],
                    result["upserted"],
                    result["cleared_stale"],
                )
            except Exception:
                logger.exception("pr sync failed")
            minutes = get_pr_sync_interval_minutes(self.conn)
            deadline = minutes * 60
            waited = 0
            self._wake.clear()
            while waited < deadline and not self._stop.is_set():
                slice_s = min(30, deadline - waited)
                if self._wake.wait(slice_s):
                    if self._stop.is_set():
                        return
                    self._wake.clear()
                    break
                waited += slice_s
