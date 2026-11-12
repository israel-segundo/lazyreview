"""GitHub PR labels: normalize, persist shape, and match helpers."""

from __future__ import annotations

import json
import re
from typing import Any

_COLOR_RE = re.compile(r"^[0-9a-fA-F]{6}$")


def normalize_pr_labels(raw: Any) -> list[dict[str, str]]:
    """
    Normalize GitHub / stored label payloads to ``[{name, color?}, ...]``.

    Accepts ``gh pr view --json labels`` objects, plain name strings, or a JSON
    string. Dedupes by lowercased name; sorts by name for stable diffs.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            return [{"name": text}] if text else []
    if not isinstance(raw, list):
        return []

    by_key: dict[str, dict[str, str]] = {}
    for item in raw:
        name = ""
        color = ""
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("Name") or "").strip()
            color = str(item.get("color") or item.get("Color") or "").strip().lstrip("#")
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        if color and _COLOR_RE.match(color):
            entry["color"] = color.lower()
        by_key[name.casefold()] = entry
    return [by_key[k] for k in sorted(by_key.keys())]


def labels_to_json(labels: list[dict[str, str]] | None) -> str:
    return json.dumps(normalize_pr_labels(labels), separators=(",", ":"))


def labels_from_pr(pr: dict[str, Any] | None) -> list[dict[str, str]]:
    """Read labels from a PR dict (list or labels_json string)."""
    if not pr:
        return []
    if "labels" in pr and not isinstance(pr.get("labels"), str):
        return normalize_pr_labels(pr.get("labels"))
    return normalize_pr_labels(pr.get("labels_json"))


def attach_pr_labels(pr: dict[str, Any]) -> dict[str, Any]:
    """Ensure ``pr['labels']`` is a normalized list; keep ``labels_json`` in sync."""
    labels = labels_from_pr(pr)
    pr["labels"] = labels
    pr["labels_json"] = labels_to_json(labels)
    return pr


def pr_has_label(pr: dict[str, Any] | None, name: str) -> bool:
    """Case-insensitive membership check for a GitHub label name."""
    want = (name or "").strip().casefold()
    if not want:
        return False
    return any((lab.get("name") or "").casefold() == want for lab in labels_from_pr(pr))


def pr_has_any_label(pr: dict[str, Any] | None, names: list[str] | tuple[str, ...] | set[str]) -> bool:
    return any(pr_has_label(pr, n) for n in names)
