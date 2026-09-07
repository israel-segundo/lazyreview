"""Agy adapter builds a valid -p prompt argv."""

from __future__ import annotations

from pathlib import Path

from lazyreview.agents.agy import AgyAdapter
from lazyreview.agents.base import ReviewContext


def test_agy_prompt_is_flag_value_not_stdin(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "lazyreview.agents.agy.which_binary",
        lambda _name: "/usr/bin/agy",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    prompt_path = run_dir / "prompt.md"
    ctx = ReviewContext(
        pr_url="https://github.com/o/r/pull/1",
        repo_dir=run_dir / "repo",
        run_dir=run_dir,
        prompt_path=prompt_path,
        model="gemini-3.1-pro-high",
        effort="medium",
        skill="pr-code-review-batch",
        head_sha="abc",
        findings_path=run_dir / "findings.md",
        report_path=run_dir / "report.html",
    )
    built = AgyAdapter().build_review_command(ctx)
    assert built.stdin_text is None
    assert built.argv[0] == "/usr/bin/agy"
    assert built.argv[1] == "-p"
    # Prompt must be the -p value; otherwise agy treats the next flag as the prompt.
    assert built.argv[2] == prompt_path.read_text(encoding="utf-8")
    assert "--output-format" in built.argv
    fmt_i = built.argv.index("--output-format")
    assert built.argv[fmt_i + 1] == "stream-json"
    assert built.argv.index("--output-format") > 2
    assert built.argv[built.argv.index("-p") + 1] != "--output-format"
    # Model id already encodes effort — do not pass a conflicting --effort.
    assert "--effort" not in built.argv
    assert "--model" in built.argv
    assert built.argv[built.argv.index("--model") + 1] == "gemini-3.1-pro-high"


def test_agy_effort_only_when_model_lacks_suffix(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "lazyreview.agents.agy.which_binary",
        lambda _name: "/usr/bin/agy",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ctx = ReviewContext(
        pr_url="https://github.com/o/r/pull/1",
        repo_dir=run_dir / "repo",
        run_dir=run_dir,
        prompt_path=run_dir / "prompt.md",
        model="some-custom-model",
        effort="high",
        skill="pr-code-review-batch",
        head_sha="abc",
        findings_path=run_dir / "findings.md",
        report_path=run_dir / "report.html",
    )
    built = AgyAdapter().build_review_command(ctx)
    assert "--effort" in built.argv
    assert built.argv[built.argv.index("--effort") + 1] == "high"


def test_agy_parse_result_unwraps_stream_json_result(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stdout = "\n".join(
        [
            '{"event":"init","tools":[]}',
            '{"event":"step_update","step_type":"thinking"}',
            '{"event":"result","result":{"status":"OK","usage":{"input_tokens":11,"output_tokens":22,"total_tokens":33}}}',
        ]
    )
    parsed = AgyAdapter().parse_result(run_dir, stdout)
    assert parsed.input_tokens == 11
    assert parsed.output_tokens == 22
    assert parsed.total_tokens == 33
    assert parsed.raw.get("status") == "OK"
