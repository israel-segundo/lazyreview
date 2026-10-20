"""Inline review posting helpers + gh submit_pr_review comments payload."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from lazyreview.config import Settings
from lazyreview.db import (
    create_consolidation,
    update_consolidation,
    upsert_pr,
)
from lazyreview.db.schema import connect, init_db
from lazyreview.services.gh import GhError, submit_pr_review
from lazyreview.services.review_post import (
    build_review_comments,
    count_inline_post_plan,
    filter_comments_for_changed_paths,
    finding_is_blocking,
    inline_comment_body,
    parse_findings_list,
    recommended_action_from_findings,
    split_findings_for_post,
)


def test_finding_is_blocking_disposition():
    assert finding_is_blocking({"disposition": "_Fix before merge._"})
    assert finding_is_blocking({"disposition": "blocker"})
    assert finding_is_blocking(
        {"title": "x", "body": "This compromises the build until fixed."}
    )
    assert not finding_is_blocking({"disposition": "_Fast-follow._"})
    assert not finding_is_blocking({"disposition": "_Author's call._", "severity": 1})
    assert not finding_is_blocking({"class": "follow_up", "severity": 1})
    assert finding_is_blocking({"class": "blocker", "severity": 3})


def test_recommended_action_from_findings():
    assert (
        recommended_action_from_findings(
            [{"disposition": "_Fast-follow._", "severity": 1}]
        )
        == "approve"
    )
    assert (
        recommended_action_from_findings(
            [{"disposition": "_Fix before merge._", "severity": 2}]
        )
        == "request_changes"
    )
    assert recommended_action_from_findings([]) == "approve"


def test_filter_comments_for_changed_paths():
    comments = [
        {"path": "in.py", "line": 1, "body": "a"},
        {"path": "out.py", "line": 2, "body": "b"},
    ]
    kept, skipped = filter_comments_for_changed_paths(comments, {"in.py"})
    assert len(kept) == 1 and kept[0]["path"] == "in.py"
    assert len(skipped) == 1 and skipped[0]["path"] == "out.py"


def test_count_inline_post_plan_off_diff():
    findings = [
        {"id": "C1", "path": "a.py", "line": 1},
        {"id": "C2", "path": "other.py", "line": 2},
        {"id": "C3", "title": "no anchor"},
    ]
    plan = count_inline_post_plan(findings, {"a.py"})
    assert plan == {
        "inline": 1,
        "summary": 2,
        "off_diff": 1,
        "bad_line": 0,
        "total": 3,
    }


def test_split_findings_uses_file_alias():
    inline, summary = split_findings_for_post(
        [{"id": "C1", "file": "a.py", "line": 3, "title": "x"}]
    )
    assert [f["id"] for f in inline] == ["C1"]
    assert summary == []


def test_split_and_inline_bodies():
    findings = [
        {"id": "C1", "path": "a.py", "line": 10, "title": "bad guard", "body": "Wrong branch."},
        {"id": "C2", "title": "missing test", "body": "No coverage for X."},
        {"id": "C3", "path": "b.py", "line": "not-int", "title": "skip"},
    ]
    inline, summary = split_findings_for_post(findings)
    assert [f["id"] for f in inline] == ["C1"]
    assert [f["id"] for f in summary] == ["C2", "C3"]
    comments = build_review_comments(inline)
    assert comments == [
        {
            "path": "a.py",
            "line": 10,
            "side": "RIGHT",
            "body": "**C1** — bad guard. Wrong branch.",
        }
    ]
    assert inline_comment_body({"id": "C9", "title": "t"}).startswith("**C9** —")


def test_parse_findings_list():
    assert parse_findings_list(None) == []
    assert parse_findings_list("[]") == []
    assert parse_findings_list('[{"id":"C1"}]')[0]["id"] == "C1"
    assert parse_findings_list([{"id": "C1"}])[0]["id"] == "C1"


def test_submit_pr_review_payload_includes_comments():
    captured: list[str] = []

    def _fake_post(owner, repo, number, payload):
        captured.append(payload)
        return {"ok": True, "id": "1", "html_url": "https://example.com/r/1"}

    with patch("lazyreview.services.gh._post_review", side_effect=_fake_post):
        res = submit_pr_review(
            "o",
            "r",
            1,
            "Approved.",
            "APPROVE",
            commit_id="abc123",
            comments=[
                {"path": "a.py", "line": 3, "body": "**C1** — x"},
                {"path": "", "line": 1, "body": "skip"},
            ],
        )
    assert res["dropped_comments"] == []
    data = json.loads(captured[0])
    assert data["commit_id"] == "abc123"
    assert data["event"] == "APPROVE"
    assert data["body"] == "Approved."
    assert data["comments"] == [
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "**C1** — x"}
    ]


def test_submit_pr_review_requires_commit_id_with_comments():
    with pytest.raises(GhError, match="commit_id"):
        submit_pr_review(
            "o",
            "r",
            1,
            "body",
            "COMMENT",
            comments=[{"path": "a.py", "line": 1, "body": "x"}],
        )


def test_submit_pr_review_422_attaches_comments_individually():
    review_calls: list[str] = []

    def _fake_post(owner, repo, number, payload):
        review_calls.append(payload)
        data = json.loads(payload)
        if data.get("comments"):
            raise GhError("HTTP 422: Line must be part of the diff")
        return {"ok": True, "id": "9", "html_url": "https://example.com/r/9"}

    def _fake_single(owner, repo, number, *, commit_id, comments, review_body=""):
        return list(comments), []

    with (
        patch("lazyreview.services.gh._post_review", side_effect=_fake_post),
        patch("lazyreview.services.gh._post_inline_comments_one_review", side_effect=_fake_single),
    ):
        res = submit_pr_review(
            "o",
            "r",
            1,
            "Summary",
            "APPROVE",
            commit_id="deadbeef",
            comments=[{"path": "a.py", "line": 99, "body": "**C1** — ok"}],
        )
    assert len(review_calls) == 2
    assert "comments" not in json.loads(review_calls[1])
    assert res["posted_inline_count"] == 1
    assert res["dropped_comments"] == []


def test_submit_pr_review_prefilters_off_diff_paths():
    captured: list[str] = []

    def _fake_post(owner, repo, number, payload):
        captured.append(payload)
        return {"ok": True, "id": "1", "html_url": "u"}

    with patch("lazyreview.services.gh._post_review", side_effect=_fake_post):
        res = submit_pr_review(
            "o",
            "r",
            1,
            "Summary",
            "APPROVE",
            commit_id="abc",
            changed_paths=frozenset({"in.py"}),
            comments=[
                {"path": "in.py", "line": 1, "body": "keep"},
                {"path": "out.py", "line": 2, "body": "skip"},
            ],
        )
    data = json.loads(captured[0])
    assert len(data["comments"]) == 1
    assert data["comments"][0]["path"] == "in.py"
    assert len(res["dropped_comments"]) == 1
    assert res["skipped_off_diff_count"] == 1
    assert res["posted_inline_count"] == 1


def test_submit_pr_review_body_only_when_no_comments():
    captured: list[str] = []

    def _fake_post(owner, repo, number, payload):
        captured.append(payload)
        return {"ok": True, "id": "2", "html_url": "u"}

    with patch("lazyreview.services.gh._post_review", side_effect=_fake_post):
        submit_pr_review("o", "r", 1, "hi", "COMMENT")
    assert json.loads(captured[0]) == {"body": "hi", "event": "COMMENT"}


def _settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Settings(
        host="127.0.0.1",
        port=3334,
        data_dir=data,
        runs_dir=data / "runs",
        db_path=data / "lazyreview.db",
        config_path=tmp_path / "config.toml",
        job_timeout_seconds=60,
        retention_sweep_seconds=3600,
        max_concurrent_jobs=1,
    )


def _seed_succeeded_analysis(conn, pr):
    from lazyreview.db import create_pr_analysis, update_pr_analysis

    row = create_pr_analysis(
        conn,
        pr_id=int(pr["id"]),
        head_sha=pr["head_sha"],
        agent="claude",
        model="haiku",
        effort="low",
    )
    update_pr_analysis(
        conn,
        int(row["id"]),
        status="succeeded",
        brief_markdown="# ok",
        finished_at="2026-01-01T00:00:00+00:00",
    )


def test_post_sends_inline_comments(tmp_path: Path):
    from lazyreview.main import create_app

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    conn = connect(settings.db_path)
    init_db(conn)
    pr = upsert_pr(
        conn,
        owner="o",
        repo="r",
        number=42,
        url="https://example.com/o/r/pull/42",
        title="t",
        base_ref="main",
        head_ref="feat",
        head_sha="abc123def",
    )
    _seed_succeeded_analysis(conn, pr)
    cur = conn.execute(
        """
        INSERT INTO review_runs (pr_id, agent, model, status, head_sha, workdir_path, consolidated)
        VALUES (?, 'claude', 'opus', 'succeeded', 'abc123def', ?, 1)
        """,
        (pr["id"], str(tmp_path / "w")),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    findings = [
        {
            "id": "C1",
            "severity": 2,
            "title": "guard",
            "body": "Wrong branch.",
            "path": "src/a.py",
            "line": 12,
            "disposition": "_Fast-follow._",
        },
        {
            "id": "C2",
            "severity": 3,
            "title": "missing doc",
            "body": "No README note.",
            "disposition": "_Author's call._",
        },
    ]
    row = create_consolidation(
        conn,
        pr_id=pr["id"],
        head_sha="abc123def",
        source_run_ids=[run_id],
        agent="claude",
        model="haiku",
        effort="low",
        workdir_path=str(tmp_path / "c"),
    )
    update_consolidation(
        conn,
        int(row["id"]),
        status="succeeded",
        body="## Review\n",
        publish_body="## Review\n\nApproved.\n\nDetails on the diff for C1.\n",
        ready_to_post=1,
        recommended_action="approve",
        findings_json=json.dumps(findings),
    )
    cid = int(row["id"])
    conn.close()

    captured: dict = {}

    def _fake_submit(
        owner,
        repo,
        number,
        body,
        event,
        *,
        commit_id=None,
        comments=None,
        changed_paths=None,
    ):
        captured["args"] = {
            "owner": owner,
            "repo": repo,
            "number": number,
            "body": body,
            "event": event,
            "commit_id": commit_id,
            "comments": comments,
            "changed_paths": changed_paths,
        }
        return {
            "ok": True,
            "id": "99",
            "html_url": "https://example.com/review/99",
            "dropped_comments": [],
            "posted_inline_count": len(comments or []),
            "skipped_off_diff_count": 0,
        }

    with (
        patch("lazyreview.main.get_settings", return_value=settings),
        patch(
            "lazyreview.api.consolidations.list_pr_file_patches",
            return_value={"src/a.py": "@@ -1 +1,2 @@\n line\n+add\n"},
        ),
        patch(
            "lazyreview.api.consolidations.submit_pr_review",
            side_effect=_fake_submit,
        ),
    ):
        app = create_app()
        with TestClient(app) as client:
            res = client.post(
                f"/api/consolidations/{cid}/post",
                json={"action": "approve", "body": "## Review\n\nApproved.\n"},
            )
            assert res.status_code == 200, res.text
            data = res.json()
            assert data["inline_comments"] == 1
            assert data["dropped_inline_comments"] == 0

    assert captured["args"]["event"] == "APPROVE"
    assert captured["args"]["commit_id"] == "abc123def"
    assert len(captured["args"]["comments"]) == 1
    assert captured["args"]["comments"][0]["path"] == "src/a.py"
    assert captured["args"]["comments"][0]["line"] == 12
    assert "**C1**" in captured["args"]["comments"][0]["body"]
