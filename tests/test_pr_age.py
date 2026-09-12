"""PR head age (days awaiting review) and list ordering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from lazyreview.db import head_age_days, list_prs, upsert_pr
from lazyreview.db.schema import connect, init_db
from lazyreview.services.gh import _head_committed_at


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "age.db")
    init_db(conn)
    return conn


def test_head_age_days_floors_to_whole_days():
    now = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    assert head_age_days(None, now=now) is None
    assert head_age_days((now - timedelta(hours=5)).isoformat(), now=now) == 0
    assert head_age_days((now - timedelta(days=1, hours=1)).isoformat(), now=now) == 1
    assert head_age_days((now - timedelta(days=3)).isoformat(), now=now) == 3
    assert head_age_days("2026-09-01T00:00:00Z", now=now) == 6


def test_head_committed_at_prefers_matching_oid():
    commits = [
        {"oid": "aaa", "committedDate": "2026-09-01T00:00:00Z"},
        {"oid": "bbb", "committedDate": "2026-09-05T00:00:00Z"},
    ]
    assert _head_committed_at(commits, "bbb") == "2026-09-05T00:00:00Z"
    assert _head_committed_at(commits, "missing") == "2026-09-05T00:00:00Z"


def test_list_prs_sorted_by_age_desc(tmp_path: Path):
    conn = _conn(tmp_path)
    upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/1",
        title="new",
        base_ref="main",
        head_ref="a",
        head_sha="sha1",
        head_updated_at="2026-09-06T00:00:00+00:00",
    )
    upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=2,
        url="https://example.com/2",
        title="old",
        base_ref="main",
        head_ref="b",
        head_sha="sha2",
        head_updated_at="2026-09-01T00:00:00+00:00",
    )
    upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=3,
        url="https://example.com/3",
        title="mid",
        base_ref="main",
        head_ref="c",
        head_sha="sha3",
        head_updated_at="2026-09-04T00:00:00+00:00",
    )
    rows = list_prs(conn)
    assert [r["number"] for r in rows] == [2, 3, 1]
    assert rows[0]["age_days"] >= rows[1]["age_days"] >= rows[2]["age_days"]


def test_head_change_resets_head_updated_at(tmp_path: Path):
    conn = _conn(tmp_path)
    upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="old",
        head_updated_at="2026-08-01T00:00:00+00:00",
    )
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=1,
        url="https://example.com/1",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="new",
        head_updated_at="2026-09-07T00:00:00+00:00",
    )
    assert pr["head_sha"] == "new"
    assert pr["head_updated_at"].startswith("2026-09-07")
