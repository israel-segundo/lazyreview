from pathlib import Path

from lazyreview.services.prepare import collect_review_artifacts, prepare_review_workspace


def test_prepare_installs_skill_and_artifact_paths(tmp_path: Path, monkeypatch):
    # Fake host skill
    host = tmp_path / "host_skills" / "pr-code-review"
    host.mkdir(parents=True)
    (host / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    monkeypatch.setattr(
        "lazyreview.services.prepare.HOST_SKILL_ROOTS",
        (tmp_path / "host_skills",),
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git" / "info").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    prepared = prepare_review_workspace(
        repo_dir=repo, run_dir=run_dir, skill="pr-code-review"
    )
    assert prepared.findings_path.exists()
    assert prepared.report_path.exists()
    assert (repo / ".cursor" / "skills" / "pr-code-review" / "SKILL.md").exists()
    assert any("pr-code-review" in s for s in prepared.skills_installed)

    cli = (repo / ".cursor" / "cli.json").read_text(encoding="utf-8")
    assert '"permissions"' in cli
    assert '"allow"' in cli
    assert '"version"' not in cli
    assert '"permissions": "allow"' not in cli

    exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert ".lazyreview/" in exclude.splitlines()

    prepared.findings_path.write_text("## F1 — [High] x\n", encoding="utf-8")
    prepared.report_path.write_text("<html></html>", encoding="utf-8")
    out = collect_review_artifacts(prepared)
    assert out["findings"] == run_dir / "findings.md"
    assert (run_dir / "findings.md").read_text(encoding="utf-8").startswith("## F1")


def test_prepare_falls_back_to_bundled_skill(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("lazyreview.services.prepare.HOST_SKILL_ROOTS", ())

    repo = tmp_path / "repo"
    (repo / ".git" / "info").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    prepared = prepare_review_workspace(
        repo_dir=repo, run_dir=run_dir, skill="pr-code-review-batch"
    )
    for agent_dir in (".cursor", ".claude"):
        skill_dir = repo / agent_dir / "skills" / "pr-code-review-batch"
        assert (skill_dir / "SKILL.md").exists()
        assert (skill_dir / "references" / "findings-report-template.html").exists()
    assert ".cursor/skills/pr-code-review-batch" in prepared.skills_installed


def test_prepare_exclude_resolves_worktree_gitdir_file(tmp_path: Path, monkeypatch):
    """Shared PR worktrees use a `.git` *file* pointing at the real gitdir."""
    monkeypatch.setattr("lazyreview.services.prepare.HOST_SKILL_ROOTS", ())

    gitdir = tmp_path / "mirror.git" / "worktrees" / "1113"
    (gitdir / "info").mkdir(parents=True)

    repo = tmp_path / "worktree"
    repo.mkdir()
    (repo / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()

    prepare_review_workspace(repo_dir=repo, run_dir=run_dir, skill="pr-code-review")

    exclude = gitdir / "info" / "exclude"
    assert exclude.exists()
    assert ".lazyreview/" in exclude.read_text(encoding="utf-8").splitlines()
    # Must not have tried to mkdir through the `.git` file.
    assert not (repo / ".git" / "info").exists()
