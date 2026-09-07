"""Display helpers for templates."""

from __future__ import annotations

from typing import Any


def format_duration_ms(ms: Any) -> str:
    """Human-readable duration: 45s, 1m, 1m 30s, 1h 2m 3s."""
    if ms is None:
        return "—"
    try:
        value = float(ms)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return "—"
    secs = int(round(value / 1000.0))
    if secs < 60:
        return f"{secs}s"
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"


def format_cost_usd(amount: Any) -> str:
    if amount is None:
        return "—"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return "—"
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"
