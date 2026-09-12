"""Analytics dashboard aggregates and period filtering."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lazyreview.db import analytics_activity, analytics_combos, analytics_dashboard
from lazyreview.db.schema import connect, init_db


def _conn(tmp_path: Path):
    conn = connect(tmp_path / "a.db")
    init_db(conn)
    return conn


def _seed(conn):
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
        INSERT INTO review_runs
          (pr_id, agent, model, effort, status, duration_ms, cost_usd, findings_sev1, findings_sev2,
           findings_sev3, finished_at, created_at)
        VALUES
          (1, 'claude', 'opus', 'high', 'succeeded', 600000, 2.5, 2, 3, 1, '2026-09-07T12:00:00+00:00', '2026-09-07T11:00:00+00:00'),
          (1, 'cursor', 'composer-2.5', '', 'succeeded', 120000, 0.4, 0, 2, 2, '2026-09-07T12:00:00+00:00', '2026-09-07T11:00:00+00:00'),
          (2, 'agy', 'gemini-3.6-flash-high', '', 'succeeded', 90000, 0.05, 1, 1, 0, '2026-09-07T12:00:00+00:00', '2026-09-07T11:00:00+00:00'),
          (2, 'claude', 'opus', 'high', 'failed', 10000, NULL, 0, 0, 0, '2026-09-07T12:00:00+00:00', '2026-09-07T11:00:00+00:00'),
          (2, 'claude', 'haiku', 'low', 'succeeded', 30000, 0.1, 0, 0, 1, '2020-01-01T00:00:00+00:00', '2020-01-01T00:00:00+00:00')
        """
    )
    conn.commit()


def test_dashboard_all_includes_old_runs(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    data = analytics_dashboard(conn, period="all")
    assert data["summary"]["succeeded"] == 4
    assert data["summary"]["failed"] == 1
    assert data["summary"]["costed_runs"] == 4
    assert abs(data["summary"]["total_cost_usd"] - 3.05) < 1e-9
    assert len(data["usage"]["agents"]) == 3
    assert any(a["label"] == "claude" for a in data["usage"]["agents"])
    assert data["spend"]["agents"]
    assert abs(sum(a["cost"] for a in data["spend"]["agents"]) - 3.05) < 1e-9
    assert data["bubbles"]
    assert data["bubbles"][0]["diff_size"] == 120  # 100+20
    assert data["bubbles"][0]["cost_usd"] == 2.5
    labels = {c["label"] for c in data["combos"]}
    assert any(x == "claude / opus / high" for x in labels)
    opus = next(c for c in data["combos"] if c["label"] == "claude / opus / high")
    assert opus["avg_cost_usd"] == 2.5
    assert "buckets" in data["activity"]
    assert data["activity"]["period"] == "all"
    assert sum(b["total"] for b in data["activity"]["buckets"]) == 5
    conn.close()


def test_dashboard_week_excludes_ancient(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    data = analytics_dashboard(conn, period="week")
    assert data["summary"]["succeeded"] == 3
    assert data["period"] == "week"
    assert abs(data["summary"]["total_cost_usd"] - 2.95) < 1e-9
    # Ancient haiku run excluded.
    assert not any(c["model"] == "haiku" for c in data["combos"])
    conn.close()


def test_combos_period_filter(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    all_rows = analytics_combos(conn, period="all")
    week_rows = analytics_combos(conn, period="week")
    assert sum(r["runs"] for r in all_rows) == 5
    assert sum(r["runs"] for r in week_rows) == 4
    cursor = next(r for r in all_rows if r["agent"] == "cursor")
    assert abs(cursor["avg_cost_usd"] - 0.4) < 1e-9
    assert abs(cursor["total_cost_usd"] - 0.4) < 1e-9
    conn.close()


def test_activity_week_zero_fills(tmp_path: Path):
    conn = _conn(tmp_path)
    _seed(conn)
    act = analytics_activity(conn, period="week")
    assert act["period"] == "week"
    assert act["granularity"] == "day"
    assert len(act["buckets"]) == 7
    assert act["buckets"][-1]["date"] == datetime.now(timezone.utc).date().isoformat()
    assert all("succeeded" in b and "failed" in b and "total" in b for b in act["buckets"])
    by_date = {b["date"]: b for b in act["buckets"]}
    if "2026-09-07" in by_date:
        assert by_date["2026-09-07"]["succeeded"] == 3
        assert by_date["2026-09-07"]["failed"] == 1
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
    assert sum(b["total"] for b in act["buckets"]) == 5
    assert sum(b["succeeded"] for b in act["buckets"]) == 4
    assert sum(b["failed"] for b in act["buckets"]) == 1
    # 2020 → 2026 span uses weekly buckets.
    assert act["granularity"] == "week"
    conn.close()
