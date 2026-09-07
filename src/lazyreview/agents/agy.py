"""Agy CLI adapter."""

from __future__ import annotations

import json
import re
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
from lazyreview.services.cost import extract_cost_usd_from_json

# `agy models` returns ids that already encode effort/variant, e.g.
# gemini-3.1-pro-high, gemini-3.8-flash-medium.
_EFFORT_SUFFIX = re.compile(r"-(low|medium|high)$", re.I)


class AgyAdapter:
    name = "agy"
    display_name = "Agy"
    binary = "agy"

    def available(self) -> bool:
        return which_binary(self.binary) is not None

    def list_models(self) -> list[ModelInfo]:
        path = which_binary(self.binary)
        if not path:
            return []
        import subprocess

        proc = subprocess.run(
            [path, "models"],
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
            parts = line.split()
            if not parts:
                continue
            mid = parts[0]
            # Effort is baked into the model id; a separate --effort flag conflicts.
            models.append(ModelInfo(id=mid, label=line, efforts=[]))
        if not models:
            models.append(ModelInfo(id="default", label="default", efforts=[]))
        return models

    def effort_options(self) -> list[str]:
        # Agy model ids from `agy models` already encode effort/variant.
        return []

    def resolve_model_args(self, model: str, effort: str | None) -> list[str]:
        args: list[str] = []
        if model and model != "default":
            args.extend(["--model", model])
        # Never combine --effort with ids like gemini-3.1-pro-high — agy rejects it.
        if effort and not _EFFORT_SUFFIX.search(model or ""):
            mapped = effort if effort in ("low", "medium", "high") else "high"
            args.extend(["--effort", mapped])
        return args

    def build_review_command(self, ctx: ReviewContext) -> BuiltCommand:
        path = which_binary(self.binary)
        if not path:
            raise RuntimeError("agy not found on PATH")
        write_prompt(ctx)
        prompt = ctx.prompt_path.read_text(encoding="utf-8")
        # agy's -p/--print takes the prompt as its flag value (unlike Claude,
        # which reads stdin after a bare -p). Put the prompt immediately after
        # -p so later flags like --output-format are not swallowed as the prompt.
        # Keep --add-dir last: it is repeatable and would eat a trailing prompt.
        # stream-json: NDJSON init/step_update/result events so the job pump can
        # show live logs (plain json buffers one blob until exit).
        return BuiltCommand(
            argv=[
                path,
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--mode",
                "accept-edits",
                "--dangerously-skip-permissions",
                *self.resolve_model_args(ctx.model, ctx.effort),
                "--add-dir",
                str(ctx.run_dir),
            ],
        )

    def parse_result(self, run_dir: Path, stdout: str) -> ParsedReview:
        data = self._final_result_payload(stdout)
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

    @staticmethod
    def _final_result_payload(stdout: str) -> dict:
        """Unwrap the terminal stream-json result event (or a plain json blob)."""
        # Prefer an explicit result event from an NDJSON transcript.
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
                return obj["result"]
        data = parse_json_stdout(stdout)
        if isinstance(data, dict) and data.get("event") == "result":
            inner = data.get("result")
            if isinstance(inner, dict):
                return inner
        return data if isinstance(data, dict) else {}
