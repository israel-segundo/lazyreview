"""Tests for shared PR worktrees (mirror + worktree + delete queue)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from lazyreview.config import Settings
from lazyreview.db.schema import connect, init_db
from lazyreview.services.pr_workspace import (
    ensure_pr_worktree,
    prune_queued_workspaces,
    queue_workspace_deletion,
    refresh_pr_worktree,
    worktree_path,
)


def _settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    data.mkdir()
    return Settings(
        host="127.0.0.1",
        port=3334,
        data_dir=data,
        db_path=data / "lazyreview.db",
        runs_dir=data / "runs",
        config_path=tmp_path / "config.toml",
        job_timeout_seconds=60,
        retention_sweep_seconds=3600,
        max_concurrent_jobs=2,
        max_concurrent_analyses=2,
    )


def _git_repo(path: Path) -> tuple[str, str]:
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "a"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    sha_a = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (path / "README.md").write_text("b\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "b"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    sha_b = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return sha_a, sha_b


def test_worktree_create_refresh_and_delete(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    init_db(conn)

    source = tmp_path / "source"
    sha_a, sha_b = _git_repo(source)

    # Point clone_url at the local source repo.
    monkeypatch.setattr(
        "lazyreview.services.pr_workspace.clone_url",
        lambda owner, repo: str(source),
    )

    conn.execute(
        """
        INSERT INTO prs (owner, repo, number, url, title, base_ref, head_ref, head_sha, state)
        VALUES ('acme', 'demo', 1, 'https://example/pr/1', 't', 'main', 'feat', ?, 'open')
        """,
        (sha_a,),
    )
    conn.commit()
    pr = dict(conn.execute("SELECT * FROM prs WHERE id = 1").fetchone())

    wt = ensure_pr_worktree(settings, pr, sha_a, conn=conn)
    assert wt.exists()
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == sha_a

    wt2 = refresh_pr_worktree(settings, pr, sha_b, conn=conn)
    assert wt2 == wt
    head2 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head2 == sha_b

    queue_workspace_deletion(conn, 1)
    removed = prune_queued_workspaces(settings, conn)
    assert removed == 1
    assert not worktree_path(settings, 1).exists()
    row = dict(conn.execute("SELECT workspace_status FROM prs WHERE id = 1").fetchone())
    assert row["workspace_status"] == "deleted"
    conn.close()


def test_worktree_recovered_when_gitdir_missing(tmp_path: Path, monkeypatch):
    """Orphan PR dirs (`.git` file but no mirror worktree metadata) are recreated."""
    settings = _settings(tmp_path)
    conn = connect(settings.db_path)
    init_db(conn)

    source = tmp_path / "source"
    sha_a, _sha_b = _git_repo(source)
    monkeypatch.setattr(
        "lazyreview.services.pr_workspace.clone_url",
        lambda owner, repo: str(source),
    )

    conn.execute(
        """
        INSERT INTO prs (owner, repo, number, url, title, base_ref, head_ref, head_sha, state)
        VALUES ('acme', 'demo', 2, 'https://example/pr/2', 't', 'main', 'feat', ?, 'open')
        """,
        (sha_a,),
    )
    conn.commit()
    pr = dict(conn.execute("SELECT * FROM prs WHERE id = 1").fetchone())

    wt = ensure_pr_worktree(settings, pr, sha_a, conn=conn)
    gitdir = (wt / ".git").read_text(encoding="utf-8").split(":", 1)[1].strip()
    shutil.rmtree(gitdir, ignore_errors=True)

    wt2 = ensure_pr_worktree(settings, pr, sha_a, conn=conn)
    assert wt2 == wt
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt2,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == sha_a
    conn.close()
