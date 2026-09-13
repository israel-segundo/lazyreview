"""Prepare a cloned PR worktree for headless agentic review."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Skills we try to make available in the clone (first match wins per name).
DEFAULT_SKILL_NAMES = (
    "pr-code-review-batch",
    "pr-code-review",
    "code-review",
    "pr-review",
)

HOST_SKILL_ROOTS = (
    Path.home() / ".cursor" / "skills",
    Path.home() / ".agents" / "skills",
    Path.home() / ".claude" / "skills",
)


@dataclass
class PreparedWorkspace:
    repo_dir: Path
    run_dir: Path
    work_dir: Path  # repo/.lazyreview
    findings_path: Path
    report_path: Path
    skills_installed: list[str] = field(default_factory=list)


def _copy_skill(src: Path, dest: Path) -> None:
    if dest.exists():
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    shutil.copytree(src, dest, dirs_exist_ok=False)


def _install_host_skills(repo_dir: Path, skill: str) -> list[str]:
    """Copy host skills into repo-local locations agents discover."""
    names = []
    for name in (skill, *DEFAULT_SKILL_NAMES):
        if name and name not in names:
            names.append(name)

    installed: list[str] = []
    cursor_skills = repo_dir / ".cursor" / "skills"
    claude_skills = repo_dir / ".claude" / "skills"

    for name in names:
        src: Path | None = None
        for root in HOST_SKILL_ROOTS:
            candidate = root / name
            if candidate.is_dir() and (candidate / "SKILL.md").exists():
                src = candidate
                break
        if src is None:
            continue

        # Cursor / agent
        cursor_skills.mkdir(parents=True, exist_ok=True)
        try:
            _copy_skill(src, cursor_skills / name)
            installed.append(f".cursor/skills/{name}")
        except OSError as e:
            logger.warning("failed to install cursor skill %s: %s", name, e)

        # Claude Code often looks under .claude/skills
        claude_skills.mkdir(parents=True, exist_ok=True)
        try:
            _copy_skill(src, claude_skills / name)
            installed.append(f".claude/skills/{name}")
        except OSError as e:
            logger.warning("failed to install claude skill %s: %s", name, e)

    return installed


def _git_common_dir(repo_dir: Path) -> Path | None:
    """Resolve the real git dir (handles worktrees where ``.git`` is a file)."""
    git = repo_dir / ".git"
    if git.is_dir():
        return git
    if not git.is_file():
        return None
    # Linked worktree: `.git` contains `gitdir: /path/to/.../worktrees/<id>`
    for line in git.read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("gitdir:"):
            raw = line.split(":", 1)[1].strip()
            if not raw:
                return None
            gitdir = Path(raw)
            if not gitdir.is_absolute():
                gitdir = (repo_dir / gitdir).resolve()
            return gitdir
    return None


def _ensure_gitignore(repo_dir: Path) -> None:
    """Keep review artifacts out of the agent's notion of dirty source."""
    git_dir = _git_common_dir(repo_dir)
    if git_dir is None:
        logger.warning("no .git dir/file under %s; skip exclude update", repo_dir)
        return
    gi = git_dir / "info" / "exclude"
    gi.parent.mkdir(parents=True, exist_ok=True)
    line = ".lazyreview/"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if line not in existing.splitlines():
        with gi.open("a", encoding="utf-8") as f:
            f.write(f"\n{line}\n")


def prepare_review_workspace(
    *,
    repo_dir: Path,
    run_dir: Path,
    skill: str,
) -> PreparedWorkspace:
    """
    After clone: create writeable artifact dir, install host skills, trust markers.

    Findings/report are written under repo/.lazyreview/ so the agent stays inside cwd,
    then collected back to run_dir after the run.
    """
    work_dir = repo_dir / ".lazyreview"
    work_dir.mkdir(parents=True, exist_ok=True)
    findings_path = work_dir / "findings.md"
    report_path = work_dir / "report.html"

    # Seed empty files so Write tools have a clear target
    if not findings_path.exists():
        findings_path.write_text("", encoding="utf-8")
    if not report_path.exists():
        report_path.write_text("", encoding="utf-8")

    _ensure_gitignore(repo_dir)
    skills_installed = _install_host_skills(repo_dir, skill)

    # Project-level Cursor CLI config: permissions only (no version/editor — those are
    # global). permissions must be an object with allow/deny arrays, not a string.
    cursor_dir = repo_dir / ".cursor"
    cursor_dir.mkdir(parents=True, exist_ok=True)
    trust = cursor_dir / "cli.json"
    if not trust.exists():
        trust.write_text(
            '{\n'
            '  "permissions": {\n'
            '    "allow": ["Shell(*)", "Read(**)", "Write(**)", "WebFetch(*)"],\n'
            '    "deny": []\n'
            '  }\n'
            '}\n',
            encoding="utf-8",
        )

    # Record prep metadata for auditing
    meta = work_dir / "prepare.json"
    meta.write_text(
        (
            "{\n"
            f'  "cwd": {os.fspath(repo_dir)!r},\n'
            f'  "findings": {os.fspath(findings_path)!r},\n'
            f'  "report": {os.fspath(report_path)!r},\n'
            f'  "skills": {skills_installed!r}\n'
            "}\n"
        ),
        encoding="utf-8",
    )

    logger.info(
        "prepared workspace %s skills=%s",
        repo_dir,
        skills_installed or ["(none found on host)"],
    )
    return PreparedWorkspace(
        repo_dir=repo_dir,
        run_dir=run_dir,
        work_dir=work_dir,
        findings_path=findings_path,
        report_path=report_path,
        skills_installed=skills_installed,
    )


def collect_review_artifacts(prepared: PreparedWorkspace) -> dict[str, Path | None]:
    """Copy findings/report from the clone into the durable run_dir."""
    out: dict[str, Path | None] = {"findings": None, "report": None}
    dest_findings = prepared.run_dir / "findings.md"
    dest_report = prepared.run_dir / "report.html"

    if prepared.findings_path.exists() and prepared.findings_path.stat().st_size > 0:
        shutil.copy2(prepared.findings_path, dest_findings)
        out["findings"] = dest_findings
    if prepared.report_path.exists() and prepared.report_path.stat().st_size > 0:
        shutil.copy2(prepared.report_path, dest_report)
        out["report"] = dest_report

    # Also copy prepare.json for audit
    prep_meta = prepared.work_dir / "prepare.json"
    if prep_meta.exists():
        shutil.copy2(prep_meta, prepared.run_dir / "prepare.json")

    return out
