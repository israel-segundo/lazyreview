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

    prepared.findings_path.write_text("## F1 — [High] x\n", encoding="utf-8")
    prepared.report_path.write_text("<html></html>", encoding="utf-8")
    out = collect_review_artifacts(prepared)
    assert out["findings"] == run_dir / "findings.md"
    assert (run_dir / "findings.md").read_text(encoding="utf-8").startswith("## F1")
