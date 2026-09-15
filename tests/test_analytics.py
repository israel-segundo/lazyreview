"""Analytics dashboard aggregates and period filtering."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from lazyreview.db import (
    analytics_activity,
    analytics_combos,
    analytics_dashboard,
    analytics_prs_by_repo,
    analytics_repo_activity,
    analytics_repo_pattern,
    analytics_weekday_pattern,
)
from lazyreview.db.schema import connect, init_db


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "a.db")
    init_db(conn)
    return conn


def _seed(conn):
    """Seed runs + resolutions relative to UTC today so rolling windows stay stable."""
    today = datetime.now(timezone.utc).date()
    d1 = (today - timedelta(days=2)).isoformat()
    d2 = (today - timedelta(days=1)).isoformat()
    conn.execute(
        """
        INSERT INTO prs (owner, repo, number, url, title, base_ref, head_ref, head_sha,
                         additions, deletions, changed_files)
        VALUES ('o','r',1,'https://example.com/1','t','main','feat','abc',100,20,3)
        """
    )
    conn.execute(
        """
        INSERT INTO prs (owner, repo, number, url, title, base_ref, head_ref, head_sha,
                         additions, deletions, changed_files)
        VALUES ('o','r',2,'https://example.com/2','t2','main','feat2','def',500,50,8)
        """
    )
    conn.execute(
        """
        INSERT INTO prs (owner, repo, number, url, title, base_ref, head_ref, head_sha,
                         additions, deletions, changed_files)
        VALUES ('o','other',3,'https://example.com/3','t3','main','feat3','ghi',10,1,1)
        """
    )
    conn.execute(
        """
        INSERT INTO review_runs
          (pr_id, agent, model, effort, status, duration_ms, cost_usd, findings_sev1, findings_sev2,
           findings_sev3, finished_at, created_at)
        VALUES
          (1, 'claude', 'opus', 'high', 'succeeded', 600000, 2.5, 2, 3, 1, ? || 'T12:00:00+00:00', ? || 'T11:00:00+00:00'),
          (1, 'cursor', 'composer-2.5', '', 'succeeded', 120000, 0.4, 0, 2, 2, ? || 'T12:00:00+00:00', ? || 'T11:00:00+00:00'),
          (2, 'agy', 'gemini-3.6-flash-high', '', 'succeeded', 90000, 0.05, 1, 1, 0, ? || 'T12:00:00+00:00', ? || 'T11:00:00+00:00'),
          (2, 'claude', 'opus', 'high', 'failed', 10000, NULL, 0, 0, 0, ? || 'T12:00:00+00:00', ? || 'T11:00:00+00:00'),
          (2, 'claude', 'haiku', 'low', 'succeeded', 30000, 0.1, 0, 0, 1, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00'),
          (3, 'claude', 'haiku', 'low', 'succeeded', 20000, 0.05, 0, 0, 0, ? || 'T12:00:00+00:00', ? || 'T11:00:00+00:00')
        """,
        (d1, d1, d1, d1, d1, d1, d1, d1, d2, d2),
    )
    # Posted GitHub resolutions (comment / approve / request_changes).
    conn.execute(
        """
        INSERT INTO consolidations
          (pr_id, head_sha, source_run_ids, agent, model, status, body,
           feedback_provided, gh_action, finished_at, created_at)
        VALUES
          (1, 'abc', '[]', 'claude', 'opus', 'succeeded', 'ok',
           1, 'approve', ? || 'T15:00:00+00:00', ? || 'T14:00:00+00:00'),
          (2, 'def', '[]', 'claude', 'opus', 'succeeded', 'nits',
           1, 'comment', ? || 'T16:00:00+00:00', ? || 'T15:00:00+00:00'),
          (3, 'ghi', '[]', 'external', '', 'succeeded', 'please fix',
           1, 'request_changes', ? || 'T12:00:00+00:00', ? || 'T11:00:00+00:00'),
          (2, 'old', '[]', 'external', '', 'succeeded', 'ancient',
           1, 'approve', '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00'),
          (1, 'pending', '[]', 'claude', 'opus', 'succeeded', 'draft only',
           0, NULL, ? || 'T10:00:00+00:00', ? || 'T09:00:00+00:00')
        """,
        (d1, d1, d1, d1, d2, d2, d1, d1),
    )
    conn.commit()
    return {"d1": d1, "d2": d2}


def test_dashboard_all_includes_old_runs(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    data = analytics_dashboard(conn, period="all")
    assert data["summary"]["succeeded"] == 5
    assert data["summary"]["failed"] == 1
    assert data["summary"]["costed_runs"] == 5
    assert abs(data["summary"]["total_cost_usd"] - 3.10) < 1e-9
    assert len(data["usage"]["agents"]) == 3
    assert any(a["label"] == "claude" for a in data["usage"]["agents"])
    assert data["spend"]["agents"]
    assert abs(sum(a["cost"] for a in data["spend"]["agents"]) - 3.10) < 1e-9
    assert data["bubbles"]
    assert data["bubbles"][0]["diff_size"] == 120  # 100+20
    assert data["bubbles"][0]["cost_usd"] == 2.5
    labels = {c["label"] for c in data["combos"]}
    assert any(x == "claude / opus / high" for x in labels)
    opus = next(c for c in data["combos"] if c["label"] == "claude / opus / high")
    assert opus["avg_cost_usd"] == 2.5
    assert "buckets" in data["activity"]
    assert data["activity"]["period"] == "all"
    assert sum(b["total"] for b in data["activity"]["buckets"]) == 6
    assert data["prs_by_repo"] == [
        {"repo": "r", "pr_count": 2},
        {"repo": "other", "pr_count": 1},
    ]
    assert set(data["repo_activity"]["repos"]) == {"other", "r"}
    assert "all" in data["repo_activity"]["series"]
    assert data["weekday_pattern"]["total"] == 4
    assert data["repo_pattern"]["total"] == 4
    assert dict(zip(data["repo_pattern"]["labels"], data["repo_pattern"]["counts"])) == {
        "r": 3,
        "other": 1,
    }
    conn.close()


def test_dashboard_week_excludes_ancient(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    data = analytics_dashboard(conn, period="week")
    assert data["summary"]["succeeded"] == 4
    assert data["period"] == "week"
    assert abs(data["summary"]["total_cost_usd"] - 3.00) < 1e-9
    # Ancient 2020 haiku excluded; recent other-repo haiku remains.
    haiku_combos = [c for c in data["combos"] if c["model"] == "haiku"]
    assert len(haiku_combos) == 1
    assert haiku_combos[0]["runs"] == 1
    conn.close()


def test_combos_period_filter(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    all_rows = analytics_combos(conn, period="all")
    week_rows = analytics_combos(conn, period="week")
    assert sum(r["runs"] for r in all_rows) == 6
    assert sum(r["runs"] for r in week_rows) == 5
    cursor = next(r for r in all_rows if r["agent"] == "cursor")
    assert abs(cursor["avg_cost_usd"] - 0.4) < 1e-9
    assert abs(cursor["total_cost_usd"] - 0.4) < 1e-9
    conn.close()


def test_activity_week_zero_fills(tmp_path: Path):
    conn = _conn(tmp_path)
    days = _seed(conn)
    act = analytics_activity(conn, period="week")
    assert act["period"] == "week"
    assert act["granularity"] == "day"
    assert len(act["buckets"]) == 7
    assert act["buckets"][-1]["date"] == datetime.now(timezone.utc).date().isoformat()
    assert all("succeeded" in b and "failed" in b and "total" in b for b in act["buckets"])
    by_date = {b["date"]: b for b in act["buckets"]}
    assert by_date[days["d1"]]["succeeded"] == 3
    assert by_date[days["d1"]]["failed"] == 1
    assert by_date[days["d2"]]["succeeded"] == 1
    conn.close()


def test_activity_month_has_30_days(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    act = analytics_activity(conn, period="month")
    assert act["period"] == "month"
    assert act["granularity"] == "day"
    assert len(act["buckets"]) == 30
    conn.close()


def test_activity_all_includes_ancient(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    act = analytics_activity(conn, period="all")
    assert act["period"] == "all"
    assert sum(b["total"] for b in act["buckets"]) == 6
    assert sum(b["succeeded"] for b in act["buckets"]) == 5
    assert sum(b["failed"] for b in act["buckets"]) == 1
    # 2020 → now span uses weekly buckets.
    assert act["granularity"] == "week"
    conn.close()


def test_prs_by_repo_and_repo_activity(tmp_path: Path):
    conn = _conn(tmp_path)
    days = _seed(conn)
    by_repo = analytics_prs_by_repo(conn, period="week")
    assert {r["repo"]: r["pr_count"] for r in by_repo} == {"r": 2, "other": 1}

    repo_act = analytics_repo_activity(conn, period="week")
    assert set(repo_act["repos"]) == {"other", "r"}
    assert "all" in repo_act["series"]
    assert sum(b["total"] for b in repo_act["series"]["all"]) == 3
    assert sum(b["total"] for b in repo_act["series"]["r"]) == 2
    assert sum(b["total"] for b in repo_act["series"]["other"]) == 1
    by_date = {b["date"]: b for b in repo_act["series"]["all"]}
    assert by_date[days["d1"]]["approve"] == 1
    assert by_date[days["d1"]]["comment"] == 1
    assert by_date[days["d2"]]["request_changes"] == 1

    dash = analytics_dashboard(conn, period="week")
    assert dash["prs_by_repo"] == by_repo
    assert dash["repo_activity"]["repos"] == repo_act["repos"]
    conn.close()


def test_weekday_pattern(tmp_path: Path):
    conn = _conn(tmp_path)
    days = _seed(conn)
    pattern = analytics_weekday_pattern(conn, period="week")
    assert pattern["labels"] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert pattern["total"] == 3
    d1_wd = date.fromisoformat(days["d1"]).weekday()
    d2_wd = date.fromisoformat(days["d2"]).weekday()
    assert pattern["counts"][d1_wd] == 2
    assert pattern["counts"][d2_wd] == 1
    assert sum(pattern["counts"]) == 3

    all_pattern = analytics_weekday_pattern(conn, period="all")
    assert all_pattern["total"] == 4
    # Ancient 2020-01-01 is Wednesday.
    assert all_pattern["counts"][date.fromisoformat("2020-01-01").weekday()] >= 1
    conn.close()


def test_repo_pattern(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    pattern = analytics_repo_pattern(conn, period="week")
    assert pattern["total"] == 3
    by_repo = dict(zip(pattern["labels"], pattern["counts"]))
    assert by_repo == {"r": 2, "other": 1}
    assert pattern["labels"][0] == "r"

    all_pattern = analytics_repo_pattern(conn, period="all")
    assert all_pattern["total"] == 4
    assert dict(zip(all_pattern["labels"], all_pattern["counts"])) == {"r": 3, "other": 1}
    conn.close()
