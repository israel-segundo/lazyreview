"""Duration and cost display helpers."""

from lazyreview.services.cost import (
    estimate_cost_usd,
    extract_cost_usd_from_json,
    resolve_run_cost_usd,
)
from lazyreview.web.formatters import format_cost_usd, format_duration_ms


def test_format_duration_ms():
    assert format_duration_ms(None) == "—"
    assert format_duration_ms(0) == "0s"
    assert format_duration_ms(45_000) == "45s"
    assert format_duration_ms(60_000) == "1m"
    assert format_duration_ms(90_000) == "1m 30s"
    assert format_duration_ms(3_661_000) == "1h 1m 1s"
    assert format_duration_ms(3_600_000) == "1h"


def test_format_cost_usd():
    assert format_cost_usd(None) == "—"
    assert format_cost_usd(0) == "$0.00"
    assert format_cost_usd(3.0159) == "$3.02"
    assert format_cost_usd(0.0042) == "$0.0042"


def test_extract_cost_from_claude_result():
    data = {
        "type": "result",
        "total_cost_usd": 3.0159195,
        "usage": {"input_tokens": 46, "output_tokens": 100},
    }
    assert extract_cost_usd_from_json(data) == 3.0159195


def test_estimate_cost_opus():
    cost = estimate_cost_usd(model="opus", input_tokens=1_000_000, output_tokens=0)
    assert cost == 15.0


def test_resolve_prefers_stored():
    run = {"cost_usd": 1.25, "model": "opus", "input_tokens": 10, "output_tokens": 10}
    assert resolve_run_cost_usd(run) == 1.25
