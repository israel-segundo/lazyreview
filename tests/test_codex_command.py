"""Codex adapter: exec argv, model catalog, and JSONL parsing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from lazyreview.agents.base import ReviewContext
from lazyreview.agents.codex import CodexAdapter
from lazyreview.services.consolidation_jobs import build_consolidate_command
from lazyreview.services.session_jobs import SessionRunner, _extract_assistant_text

JSONL = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "first"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 7}},
        {"type": "item.completed", "item": {"id": "i1", "type": "agent_message", "text": "final answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 3}},
    ]
)


def _ctx(tmp_path: Path, *, model: str, effort: str | None) -> ReviewContext:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    return ReviewContext(
        pr_url="https://github.com/o/r/pull/1",
        repo_dir=run_dir / "repo",
        run_dir=run_dir,
        prompt_path=run_dir / "prompt.md",
        model=model,
        effort=effort,
        skill="pr-code-review-batch",
        head_sha="abc",
        findings_path=run_dir / "findings.md",
        report_path=run_dir / "report.html",
    )


def test_codex_review_reads_prompt_from_stdin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("lazyreview.agents.codex.which_binary", lambda _name: "/usr/bin/codex")
    ctx = _ctx(tmp_path, model="gpt-5.5", effort="high")
    built = CodexAdapter().build_review_command(ctx)

    assert built.argv[:2] == ["/usr/bin/codex", "exec"]
    assert built.argv[-1] == "-"
    assert built.stdin_text and "https://github.com/o/r/pull/1" in built.stdin_text
    assert built.argv[built.argv.index("--cd") + 1] == str(ctx.repo_dir)
    assert built.argv[built.argv.index("--add-dir") + 1] == str(ctx.run_dir)
    assert built.argv[built.argv.index("--model") + 1] == "gpt-5.5"
    assert built.argv[built.argv.index("-c") + 1] == 'model_reasoning_effort="high"'


def test_codex_default_model_passes_no_flags():
    assert CodexAdapter().resolve_model_args("default", None) == []


def test_codex_parse_result_sums_turn_usage(tmp_path: Path):
    (tmp_path / "findings.md").write_text("# Findings\n", encoding="utf-8")
    parsed = CodexAdapter().parse_result(tmp_path, JSONL)
    assert parsed.findings_md == "# Findings\n"
    assert (parsed.input_tokens, parsed.output_tokens, parsed.total_tokens) == (150, 10, 160)
    assert parsed.cost_usd is None


def test_codex_list_models_uses_listed_catalog_entries(monkeypatch):
    catalog = {
        "models": [
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}],
            },
            {"slug": "internal", "visibility": "hide"},
        ]
    }
    monkeypatch.setattr("lazyreview.agents.codex.which_binary", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(
        "lazyreview.agents.codex.subprocess.run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=json.dumps(catalog), stderr=""),
    )
    models = CodexAdapter().list_models()
    assert [(m.id, m.label, m.efforts) for m in models] == [("gpt-5.5", "GPT-5.5", ["low", "high"])]


def test_chat_reply_falls_back_to_last_codex_message():
    assert _extract_assistant_text(JSONL) == "final answer"


def test_codex_consolidation_reads_prompt_from_stdin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("lazyreview.services.consolidation_jobs.which_binary", lambda _n: "/usr/bin/codex")
    built = build_consolidate_command(
        agent="codex", model="gpt-5.5", effort="medium", prompt="consolidate", work_dir=tmp_path
    )
    assert built.argv[:2] == ["/usr/bin/codex", "exec"]
    assert built.argv[built.argv.index("--cd") + 1] == str(tmp_path)
    assert "--dangerously-bypass-approvals-and-sandbox" in built.argv
    assert built.argv[-1] == "-"
    assert built.stdin_text == "consolidate"


def test_codex_chat_has_no_shell_tools(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("lazyreview.services.session_jobs.which_binary", lambda _n: "/usr/bin/codex")
    argv, stdin_text = SessionRunner._build_command(
        object.__new__(SessionRunner),
        agent="codex",
        model="gpt-5.5",
        effort="high",
        worktree=tmp_path / "wt",
        workdir=tmp_path / "session",
        prompt="question",
        cli_session_id=None,
    )
    assert argv[:2] == ["/usr/bin/codex", "exec"]
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {"shell_tool", "unified_exec"} <= disabled
    assert argv[argv.index("--cd") + 1] == str(tmp_path / "wt")
    assert stdin_text is not None
    assert stdin_text.startswith("question")
    assert "no shell or file access" in stdin_text
