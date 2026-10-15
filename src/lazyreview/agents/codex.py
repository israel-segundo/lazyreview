"""OpenAI Codex CLI adapter."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from lazyreview.agents.base import (
    BuiltCommand,
    ModelInfo,
    ParsedReview,
    ReviewContext,
    read_artifact_text,
    which_binary,
    write_prompt,
)
from lazyreview.agents.names import AgentName


def codex_exec_argv(
    path: str,
    *,
    cwd: Path,
    add_dirs: list[Path],
    model_args: list[str],
    read_only: bool = False,
) -> list[str]:
    """`codex exec` in JSONL mode with the prompt read from stdin.

    Codex's sandbox needs bubblewrap user namespaces, which Docker's default
    seccomp profile blocks, so it cannot restrict commands in the container.
    Full mode bypasses it (the container is the boundary, as with the other
    agents' skip-permissions flags); read-only mode removes the shell tools
    instead, leaving Codex to answer from the prompt alone.
    Consolidation dirs are not git repos.
    """
    if read_only:
        access = ["--sandbox", "read-only", "--disable", "shell_tool", "--disable", "unified_exec"]
    else:
        access = ["--dangerously-bypass-approvals-and-sandbox"]
    argv = [
        path,
        "exec",
        "--json",
        *access,
        "--skip-git-repo-check",
        "--cd",
        str(cwd),
    ]
    for d in add_dirs:
        argv.extend(["--add-dir", str(d)])
    argv.extend(model_args)
    argv.append("-")
    return argv


def iter_codex_events(stdout: str):
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def last_agent_message(stdout: str) -> str:
    text = ""
    for obj in iter_codex_events(stdout):
        item = obj.get("item")
        if (
            obj.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            text = item["text"]
    return text


class CodexAdapter:
    name = AgentName.CODEX.value
    display_name = "Codex"
    binary = "codex"

    def available(self) -> bool:
        return which_binary(self.binary) is not None

    def list_models(self) -> list[ModelInfo]:
        path = which_binary(self.binary)
        if not path:
            return []
        proc = subprocess.run(
            [path, "debug", "models"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        try:
            catalog = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []
        models: list[ModelInfo] = []
        for entry in catalog.get("models") or []:
            if not isinstance(entry, dict) or not entry.get("slug"):
                continue
            if entry.get("visibility", "list") != "list":
                continue
            efforts = [
                str(level["effort"])
                for level in entry.get("supported_reasoning_levels") or []
                if isinstance(level, dict) and level.get("effort")
            ]
            models.append(
                ModelInfo(
                    id=str(entry["slug"]),
                    label=str(entry.get("display_name") or entry["slug"]),
                    efforts=efforts,
                )
            )
        return models

    def effort_options(self) -> list[str]:
        # Per-model levels come from the catalog (ModelInfo.efforts).
        return []

    def resolve_model_args(self, model: str, effort: str | None) -> list[str]:
        args: list[str] = []
        if model and model != "default":
            args.extend(["--model", model])
        if effort:
            args.extend(["-c", f"model_reasoning_effort={json.dumps(effort)}"])
        return args

    def build_review_command(self, ctx: ReviewContext) -> BuiltCommand:
        path = which_binary(self.binary)
        if not path:
            raise RuntimeError("codex not found on PATH")
        write_prompt(ctx)
        prompt = ctx.prompt_path.read_text(encoding="utf-8")
        return BuiltCommand(
            argv=codex_exec_argv(
                path,
                cwd=ctx.repo_dir,
                add_dirs=[ctx.run_dir],
                model_args=self.resolve_model_args(ctx.model, ctx.effort),
            ),
            stdin_text=prompt,
        )

    def parse_result(self, run_dir: Path, stdout: str) -> ParsedReview:
        inp = out = 0
        saw_usage = False
        final: dict[str, Any] = {}
        for obj in iter_codex_events(stdout):
            if obj.get("type") != "turn.completed":
                continue
            final = obj
            usage = obj.get("usage")
            if isinstance(usage, dict):
                saw_usage = True
                inp += int(usage.get("input_tokens") or 0)
                out += int(usage.get("output_tokens") or 0)
        findings = read_artifact_text(
            run_dir / "findings.md",
            run_dir / "repo" / ".lazyreview" / "findings.md",
        )
        report = read_artifact_text(
            run_dir / "report.html",
            run_dir / "repo" / ".lazyreview" / "report.html",
        )
        return ParsedReview(
            findings_md=findings,
            report_html=report,
            input_tokens=inp if saw_usage else None,
            output_tokens=out if saw_usage else None,
            total_tokens=(inp + out) if saw_usage else None,
            cost_usd=None,
            raw=final,
        )
