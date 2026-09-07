"""Run cost extraction and estimation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# USD per 1M tokens: (input, output). Used only when the CLI did not report a cost.
_MODEL_RATES: list[tuple[re.Pattern[str], tuple[float, float]]] = [
    (re.compile(r"opus", re.I), (15.0, 75.0)),
    (re.compile(r"sonnet", re.I), (3.0, 15.0)),
    (re.compile(r"haiku", re.I), (1.0, 5.0)),
    (re.compile(r"gemini[-_]?3.*pro|gemini[-_]?2\.5[-_]?pro", re.I), (1.25, 10.0)),
    (re.compile(r"gemini.*flash|gemini[-_]?3\.?\d*[-_]?flash", re.I), (0.15, 0.60)),
    (re.compile(r"gemini", re.I), (1.25, 10.0)),
    (re.compile(r"composer|gpt|cursor", re.I), (2.0, 10.0)),
]
_DEFAULT_RATES = (3.0, 15.0)


def extract_cost_usd_from_json(data: Any) -> float | None:
    """Pull a reported USD cost from an agent result payload."""
    if not isinstance(data, dict):
        return None
    for key in ("total_cost_usd", "cost_usd", "costUSD", "totalCostUsd", "totalCostUSD"):
        val = data.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    model_usage = data.get("modelUsage") or data.get("model_usage")
    if isinstance(model_usage, dict):
        total = 0.0
        found = False
        for entry in model_usage.values():
            if not isinstance(entry, dict):
                continue
            for key in ("costUSD", "cost_usd", "total_cost_usd"):
                if entry.get(key) is None:
                    continue
                try:
                    total += float(entry[key])
                    found = True
                    break
                except (TypeError, ValueError):
                    continue
        if found:
            return total
    usage = data.get("usage")
    if isinstance(usage, dict):
        for key in ("cost_usd", "costUSD", "total_cost_usd"):
            if usage.get(key) is None:
                continue
            try:
                return float(usage[key])
            except (TypeError, ValueError):
                continue
    return None


def rates_for_model(model: str) -> tuple[float, float]:
    for pattern, rates in _MODEL_RATES:
        if pattern.search(model or ""):
            return rates
    return _DEFAULT_RATES


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
) -> float | None:
    if input_tokens is None and output_tokens is None:
        return None
    inp = int(input_tokens or 0)
    out = int(output_tokens or 0)
    if inp <= 0 and out <= 0:
        return None
    in_rate, out_rate = rates_for_model(model)
    return (inp * in_rate + out * out_rate) / 1_000_000.0


def cost_from_stdout(stdout: str) -> float | None:
    """Scan an NDJSON / JSON transcript for a terminal cost field."""
    if not stdout:
        return None
    # Prefer the last result-like object that carries a cost.
    best: float | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        if "cost" not in line.lower() and "Cost" not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # Unwrap stream-json / nested result envelopes.
        candidates = [obj]
        if isinstance(obj.get("result"), dict):
            candidates.append(obj["result"])
        for cand in candidates:
            cost = extract_cost_usd_from_json(cand)
            if cost is not None:
                best = cost
    if best is not None:
        return best
    # Whole-blob JSON fallback.
    try:
        blob = json.loads(stdout.strip())
    except json.JSONDecodeError:
        return None
    return extract_cost_usd_from_json(blob)


def cost_from_workdir(workdir: str | Path | None) -> float | None:
    if not workdir:
        return None
    path = Path(workdir) / "stdout.log"
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        # Cap read size for huge logs — cost is near the end for stream-json.
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > 2_000_000:
                fh.seek(max(0, size - 2_000_000))
                raw = fh.read().decode("utf-8", errors="replace")
                # Drop partial first line after seek.
                raw = raw.split("\n", 1)[-1]
            else:
                raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return cost_from_stdout(raw)


def resolve_run_cost_usd(run: dict[str, Any]) -> float | None:
    """Best available USD cost for a run: stored → CLI transcript → token estimate."""
    stored = run.get("cost_usd")
    if stored is not None:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    from_log = cost_from_workdir(run.get("workdir_path"))
    if from_log is not None:
        return from_log
    return estimate_cost_usd(
        model=str(run.get("model") or ""),
        input_tokens=run.get("input_tokens"),
        output_tokens=run.get("output_tokens"),
    )
