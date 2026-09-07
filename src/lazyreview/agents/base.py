"""Agent adapter protocol and shared types."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ModelInfo:
    id: str
    label: str
    efforts: list[str] = field(default_factory=list)


@dataclass
class ReviewContext:
    pr_url: str
    repo_dir: Path
    run_dir: Path
    prompt_path: Path
    model: str
    effort: str | None
    skill: str
    head_sha: str
    findings_path: Path
    report_path: Path
    skills_installed: list[str] = field(default_factory=list)


@dataclass
class BuiltCommand:
    """CLI invocation produced by an agent adapter."""

    argv: list[str]
    # When set, written to the process stdin (needed when CLIs use variadic flags
    # like --add-dir / --allowedTools that would otherwise swallow a trailing prompt).
    stdin_text: str | None = None


@dataclass
class ParsedReview:
    findings_md: str | None = None
    report_html: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class AgentAdapter(Protocol):
    name: str
    display_name: str
    binary: str

    def available(self) -> bool: ...
    def list_models(self) -> list[ModelInfo]: ...
    def effort_options(self) -> list[str]: ...
    def resolve_model_args(self, model: str, effort: str | None) -> list[str]: ...
    def build_review_command(self, ctx: ReviewContext) -> BuiltCommand: ...
    def parse_result(self, run_dir: Path, stdout: str) -> ParsedReview: ...


def which_binary(name: str) -> str | None:
    return shutil.which(name)


def run_capture(cmd: list[str], *, timeout: int = 60) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "command failed")
    return proc.stdout


DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh", "ultra"]

REVIEW_PROMPT_TEMPLATE = """You are performing an agentic code review for lazyreview.

PR: {pr_url}
Head SHA: {head_sha}
Skill / workflow: /{skill} (prefer /pr-code-review-batch for unattended runs; fall back to /pr-code-review only if batch is missing)
Skills installed in this worktree: {skills_list}

Instructions:
1. Invoke `/{skill}` via slash command if available. Prefer **/pr-code-review-batch** (unattended: write findings + HTML, then exit — no human challenge loop). Do not wait for questions.
2. Also follow the repository's own review guidance if present (AGENTS.md, CLAUDE.md, .cursor/skills/, constitutions, CONTRIBUTING).
3. Prefer the skill workflow: learn repo rules, verify claims against evidence, blast-radius scan, produce findings with stable IDs.
4. Write findings to this exact file (markdown) — create/overwrite it:
   {findings_path}
   Use sections like:
   ## F1 — [High] short title
   - what the gap is and the cheapest fix
   - ground it in a file:line or test/CI evidence
5. Also write an HTML report to:
   {report_path}
6. Do NOT post comments to GitHub. Do NOT approve or request changes. Do NOT ask the user anything — this is a batch run.
7. Stay inside this working tree. Do not modify tracked source files; writing under `.lazyreview/` is required and allowed.
8. When findings and report are written, stop. No challenge loop."""


def write_prompt(ctx: ReviewContext) -> str:
    skills_list = ", ".join(ctx.skills_installed) if ctx.skills_installed else "(none — use repo-local guidance)"
    text = REVIEW_PROMPT_TEMPLATE.format(
        pr_url=ctx.pr_url,
        head_sha=ctx.head_sha,
        skill=ctx.skill.lstrip("/"),
        skills_list=skills_list,
        findings_path=str(ctx.findings_path),
        report_path=str(ctx.report_path),
    )
    ctx.prompt_path.write_text(text, encoding="utf-8")
    return text


def read_artifact_text(*candidates: Path) -> str | None:
    for path in candidates:
        if path.exists() and path.stat().st_size > 0:
            return path.read_text(encoding="utf-8", errors="replace")
    return None


def extract_tokens_from_json(data: Any) -> tuple[int | None, int | None, int | None]:
    if not isinstance(data, dict):
        return None, None, None
    usage = data.get("usage") or data.get("token_usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    inp = (
        usage.get("input_tokens")
        or usage.get("inputTokens")
        or usage.get("prompt_tokens")
        or data.get("input_tokens")
        or data.get("inputTokens")
    )
    out = (
        usage.get("output_tokens")
        or usage.get("outputTokens")
        or usage.get("completion_tokens")
        or data.get("output_tokens")
        or data.get("outputTokens")
    )
    total = (
        usage.get("total_tokens")
        or usage.get("totalTokens")
        or data.get("total_tokens")
        or data.get("totalTokens")
    )
    if total is None and inp is not None and out is not None:
        try:
            total = int(inp) + int(out)
        except (TypeError, ValueError):
            total = None
    try:
        return (
            int(inp) if inp is not None else None,
            int(out) if out is not None else None,
            int(total) if total is not None else None,
        )
    except (TypeError, ValueError):
        return None, None, None


def parse_json_stdout(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        last: dict[str, Any] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                last = json.loads(line)
            except json.JSONDecodeError:
                continue
        return last


def map_effort_into_model_id(model: str, effort: str | None) -> str:
    """If model already encodes effort, leave it; else append -[effort] when effort set."""
    if not effort:
        return model
    if re.search(rf"(^|[-_]){re.escape(effort)}([-_]|$)", model, re.I):
        return model
    if "[" in model:
        return model
    return f"{model}-{effort}"
