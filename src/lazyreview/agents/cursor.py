"""Cursor agent CLI adapter."""

from __future__ import annotations

import json
from pathlib import Path

from lazyreview.agents.base import (
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
from lazyreview.agents.names import AgentName
from lazyreview.services.cost import extract_cost_usd_from_json


class CursorAdapter:
    name = AgentName.CURSOR.value
    display_name = "Cursor Agent"
    binary = "agent"

    def available(self) -> bool:
        return which_binary(self.binary) is not None

    def list_models(self) -> list[ModelInfo]:
        path = which_binary(self.binary)
        if not path:
            return []
        import subprocess

        proc = subprocess.run(
            [path, "--list-models"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        models: list[ModelInfo] = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("available"):
                continue
            if " - " in line:
                mid, label = line.split(" - ", 1)
                # Cursor model IDs already encode effort/variant (e.g. composer-2.5-fast).
                # Do not offer a separate effort axis — appending -[effort] invents invalid IDs.
                models.append(ModelInfo(id=mid.strip(), label=label.strip(), efforts=[]))
            else:
                models.append(ModelInfo(id=line, label=line, efforts=[]))
        return models

    def effort_options(self) -> list[str]:
        return []

    def resolve_model_args(self, model: str, effort: str | None) -> list[str]:
        del effort  # effort is baked into Cursor model ids from --list-models
        return ["--model", model]

    def build_review_command(self, ctx: ReviewContext) -> BuiltCommand:
        path = which_binary(self.binary)
        if not path:
            raise RuntimeError("agent (Cursor) not found on PATH")
        write_prompt(ctx)
        prompt = ctx.prompt_path.read_text(encoding="utf-8")
        # -f/--force + --trust: non-interactive tool approval
        # no --mode plan: we must write findings under .lazyreview/
        return BuiltCommand(
            argv=[
                path,
                "-p",
                "--force",
                "--trust",
                "--workspace",
                str(ctx.repo_dir),
                "--add-dir",
                str(ctx.run_dir),
                "--output-format",
                "stream-json",
                "--stream-partial-output",
                *self.resolve_model_args(ctx.model, ctx.effort),
                prompt,
            ]
        )

    def parse_result(self, run_dir: Path, stdout: str) -> ParsedReview:
        data = parse_json_stdout(stdout)
        # Prefer the final result event from a stream-json transcript
        if not data or data.get("type") != "result":
            for line in reversed((stdout or "").splitlines()):
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "result":
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
