"""Claude Code CLI adapter."""

from __future__ import annotations

import json
from pathlib import Path

from lazyreview.agents.base import (
    DEFAULT_EFFORTS,
    BuiltCommand,
    ModelInfo,
    ParsedReview,
    ReviewContext,
    extract_tokens_from_json,
    parse_json_stdout,
    read_artifact_text,
    which_binary,
    write_prompt,
)
from lazyreview.agents.names import AgentName, ClaudeModelAlias, EffortLevel
from lazyreview.services.cost import extract_cost_usd_from_json

CLAUDE_MODELS = [
    ModelInfo(
        id=ClaudeModelAlias.SONNET.value,
        label="Sonnet (alias)",
        efforts=list(DEFAULT_EFFORTS),
    ),
    ModelInfo(
        id=ClaudeModelAlias.OPUS.value,
        label="Opus (alias)",
        efforts=list(DEFAULT_EFFORTS),
    ),
    ModelInfo(
        id=ClaudeModelAlias.HAIKU.value,
        label="Haiku (alias)",
        efforts=list(DEFAULT_EFFORTS),
    ),
]


class ClaudeAdapter:
    name = AgentName.CLAUDE.value
    display_name = "Claude Code"
    binary = "claude"

    def available(self) -> bool:
        return which_binary(self.binary) is not None

    def list_models(self) -> list[ModelInfo]:
        return list(CLAUDE_MODELS)

    def effort_options(self) -> list[str]:
        return list(DEFAULT_EFFORTS)

    def resolve_model_args(self, model: str, effort: str | None) -> list[str]:
        args = ["--model", model]
        if effort:
            mapped = (
                EffortLevel.HIGH.value
                if effort == EffortLevel.ULTRA.value
                else effort
            )
            args.extend(["--effort", mapped])
        return args

    def build_review_command(self, ctx: ReviewContext) -> BuiltCommand:
        path = which_binary(self.binary)
        if not path:
            raise RuntimeError("claude not found on PATH")
        write_prompt(ctx)
        prompt = ctx.prompt_path.read_text(encoding="utf-8")
        # Prompt via stdin: --add-dir and --allowedTools are variadic and would
        # swallow a trailing positional prompt.
        # stream-json + verbose: emit NDJSON events live (plain json buffers until exit).
        return BuiltCommand(
            argv=[
                path,
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
                "--dangerously-skip-permissions",
                "--permission-mode",
                "bypassPermissions",
                *self.resolve_model_args(ctx.model, ctx.effort),
                "--allowedTools",
                "Read,Glob,Grep,Write,Edit,Bash(git *),Bash(gh *),Skill,SlashCommand",
                "--add-dir",
                str(ctx.run_dir),
            ],
            stdin_text=prompt,
        )

    def parse_result(self, run_dir: Path, stdout: str) -> ParsedReview:
        data = parse_json_stdout(stdout)
        if not data or data.get("type") != "result":
            for line in reversed((stdout or "").splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict) and (
                    obj.get("type") == "result" or obj.get("subtype") == "success"
                ):
                    data = obj
                    break
        inp, out, total = extract_tokens_from_json(data)
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
            input_tokens=inp,
            output_tokens=out,
            total_tokens=total,
            cost_usd=extract_cost_usd_from_json(data),
            raw=data if isinstance(data, dict) else {},
        )
