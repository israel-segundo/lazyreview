"""Closed finding taxonomy: category, labels, severity, provenance.

Models may propose values; persisted + published findings must pass through
``normalize_finding`` / ``normalize_findings_list`` so analytics stay on a
stable floor (no improvised categories).
"""

from __future__ import annotations

from typing import Any

CATEGORIES = frozenset(
    {
        "correctness",
        "security",
        "architecture",
        "quality",
        "housekeeping",
    }
)

LABELS = frozenset(
    {
        "bug",
        "edge-case",
        "security",
        "compliance",
        "arch-gap",
        "scalability",
        "test",
        "performance",
        "maintainability",
        "docs",
        "nit",
        "cleanup",
    }
)

CATEGORY_LABELS: dict[str, frozenset[str]] = {
    "correctness": frozenset({"bug", "edge-case"}),
    "security": frozenset({"security", "compliance"}),
    "architecture": frozenset({"arch-gap", "scalability"}),
    "quality": frozenset({"test", "performance", "maintainability"}),
    "housekeeping": frozenset({"docs", "nit", "cleanup"}),
}

LABEL_TO_CATEGORY: dict[str, str] = {
    label: cat for cat, labs in CATEGORY_LABELS.items() for label in labs
}

ORIGINS = frozenset({"model", "human"})

DEFAULT_CATEGORY = "quality"
DEFAULT_SEVERITY = 2
DEFAULT_ORIGIN = "model"

_SEVERITY_ALIASES: dict[str, int] = {
    "1": 1,
    "high": 1,
    "sev1": 1,
    "s1": 1,
    "critical": 1,
    "blocker": 1,
    "2": 2,
    "medium": 2,
    "med": 2,
    "sev2": 2,
    "s2": 2,
    "3": 3,
    "low": 3,
    "sev3": 3,
    "s3": 3,
    "nit": 3,
    "info": 3,
}

_CATEGORY_ALIASES: dict[str, str] = {
    "correctness": "correctness",
    "correct": "correctness",
    "bug": "correctness",
    "logic": "correctness",
    "security": "security",
    "sec": "security",
    "compliance": "security",
    "architecture": "architecture",
    "arch": "architecture",
    "design": "architecture",
    "scalability": "architecture",
    "quality": "quality",
    "test": "quality",
    "testing": "quality",
    "performance": "quality",
    "perf": "quality",
    "maintainability": "quality",
    "maintain": "quality",
    "housekeeping": "housekeeping",
    "docs": "housekeeping",
    "documentation": "housekeeping",
    "nit": "housekeeping",
    "cleanup": "housekeeping",
    "style": "housekeeping",
}

_LABEL_ALIASES: dict[str, str] = {
    "bug": "bug",
    "bugs": "bug",
    "edge-case": "edge-case",
    "edge_case": "edge-case",
    "edgecase": "edge-case",
    "edge case": "edge-case",
    "security": "security",
    "sec": "security",
    "compliance": "compliance",
    "arch-gap": "arch-gap",
    "arch_gap": "arch-gap",
    "archgap": "arch-gap",
    "architecture": "arch-gap",
    "scalability": "scalability",
    "scale": "scalability",
    "test": "test",
    "tests": "test",
    "testing": "test",
    "performance": "performance",
    "perf": "performance",
    "maintainability": "maintainability",
    "maintain": "maintainability",
    "docs": "docs",
    "doc": "docs",
    "documentation": "docs",
    "nit": "nit",
    "nits": "nit",
    "cleanup": "cleanup",
    "clean-up": "cleanup",
    "clean_up": "cleanup",
}

SEVERITY_LABELS = {1: "High", 2: "Medium", 3: "Low"}


def normalize_severity(raw: Any) -> int:
    if isinstance(raw, bool):
        return DEFAULT_SEVERITY
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        n = int(raw)
        return n if n in (1, 2, 3) else DEFAULT_SEVERITY
    key = str(raw or "").strip().lower()
    return _SEVERITY_ALIASES.get(key, DEFAULT_SEVERITY)


def normalize_category(raw: Any) -> str:
    key = str(raw or "").strip().lower().replace(" ", "-")
    if key in CATEGORIES:
        return key
    return _CATEGORY_ALIASES.get(key, DEFAULT_CATEGORY)


def normalize_labels(raw: Any, *, category: str | None = None) -> list[str]:
    if raw is None:
        items: list[Any] = []
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            items = []
        elif text.startswith("["):
            try:
                import json

                parsed = json.loads(text)
                items = parsed if isinstance(parsed, list) else [text]
            except Exception:
                items = [p.strip() for p in text.split(",")]
        else:
            items = [p.strip() for p in text.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]

    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = str(item or "").strip().lower().replace("_", "-").replace(" ", "-")
        label = _LABEL_ALIASES.get(key, key if key in LABELS else "")
        if not label or label in seen:
            continue
        if category and label not in CATEGORY_LABELS.get(category, frozenset()):
            # Allow label if it maps to another known category only when category
            # was defaulted and we can still keep closed-set tokens — drop mismatch.
            continue
        seen.add(label)
        out.append(label)
    return out


def infer_category_from_labels(labels: list[str]) -> str | None:
    for label in labels:
        cat = LABEL_TO_CATEGORY.get(label)
        if cat:
            return cat
    return None


def normalize_origin(raw: Any) -> str:
    key = str(raw or "").strip().lower()
    if key in ORIGINS:
        return key
    if key in {"agent", "llm", "ai", "reviewer"}:
        return "model"
    if key in {"user", "chat", "manual", "reviewer-human"}:
        return "human"
    return DEFAULT_ORIGIN


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    key = str(raw or "").strip().lower()
    return key in {"1", "true", "yes", "y", "on"}


def normalize_finding(
    finding: dict[str, Any] | None,
    *,
    default_origin: str = DEFAULT_ORIGIN,
    preserve_origin: str | None = None,
    force_human_edited: bool | None = None,
) -> dict[str, Any]:
    """Return a shallow-copied finding with taxonomy fields coerced."""
    src = dict(finding or {})
    severity = normalize_severity(src.get("severity") if "severity" in src else src.get("Severity"))

    raw_labels = src.get("labels")
    if raw_labels is None:
        raw_labels = src.get("Labels")
    # First pass without category filter so we can infer category from labels.
    tentative_labels = normalize_labels(raw_labels, category=None)

    raw_cat = src.get("category") if "category" in src else src.get("Category")
    if raw_cat is None or str(raw_cat).strip() == "":
        category = infer_category_from_labels(tentative_labels) or DEFAULT_CATEGORY
    else:
        cat_key = str(raw_cat).strip().lower().replace(" ", "-")
        if cat_key in CATEGORIES or cat_key in _CATEGORY_ALIASES:
            category = normalize_category(raw_cat)
        else:
            # Unknown category → prefer label inference over defaulting then dropping labels.
            category = (
                infer_category_from_labels(tentative_labels) or DEFAULT_CATEGORY
            )

    labels = normalize_labels(raw_labels, category=category)

    if preserve_origin is not None:
        origin = normalize_origin(preserve_origin)
    elif "origin" in src or "Origin" in src:
        origin = normalize_origin(src.get("origin", src.get("Origin")))
    else:
        origin = normalize_origin(default_origin)

    if force_human_edited is not None:
        human_edited = bool(force_human_edited)
    else:
        human_edited = _as_bool(src.get("human_edited", src.get("humanEdited", False)))

    out = dict(src)
    out["severity"] = severity
    out["category"] = category
    out["labels"] = labels
    out["origin"] = origin
    out["human_edited"] = human_edited
    return out


def normalize_findings_list(
    findings: list[Any] | None,
    *,
    default_origin: str = DEFAULT_ORIGIN,
    prior_by_id: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Normalize a list of findings; enforce immutable origin vs prior snapshot."""
    prior = prior_by_id or {}
    path_origin = normalize_origin(default_origin)
    out: list[dict[str, Any]] = []
    for item in findings or []:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or item.get("external_id") or "").strip()
        prev = prior.get(fid) if fid else None
        if prev:
            prev_origin = normalize_origin(prev.get("origin"))
            if prev_origin == "model":
                edited = bool(prev.get("human_edited")) or _finding_content_changed(
                    prev, item
                )
                norm = normalize_finding(
                    item,
                    preserve_origin="model",
                    force_human_edited=edited,
                )
            else:
                norm = normalize_finding(
                    item,
                    preserve_origin="human",
                    force_human_edited=_as_bool(prev.get("human_edited")),
                )
        else:
            origin = "human" if path_origin == "human" else "model"
            norm = normalize_finding(
                item,
                preserve_origin=origin,
                force_human_edited=False,
            )
        out.append(norm)
    return out


def _finding_content_changed(prior: dict[str, Any], current: dict[str, Any]) -> bool:
    keys = ("severity", "title", "body", "path", "line", "disposition", "category", "labels")
    for key in keys:
        a = prior.get(key)
        b = current.get(key)
        if key == "labels":
            a = normalize_labels(a, category=None)
            b = normalize_labels(b, category=None)
        elif key == "severity":
            a = normalize_severity(a)
            b = normalize_severity(b)
        elif key == "category":
            # Missing category is treated as default so old rows aren't "edited".
            a = normalize_category(a) if a not in (None, "") else DEFAULT_CATEGORY
            b = normalize_category(b) if b not in (None, "") else DEFAULT_CATEGORY
        elif key in ("title", "body", "disposition", "path"):
            a = "" if a is None else str(a).strip()
            b = "" if b is None else str(b).strip()
        elif key == "line":
            try:
                a = int(a) if a not in (None, "") else None
            except (TypeError, ValueError):
                a = None
            try:
                b = int(b) if b not in (None, "") else None
            except (TypeError, ValueError):
                b = None
        if a != b:
            return True
    return False


def findings_by_id(findings: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id") or f.get("external_id") or "").strip()
        if fid:
            out[fid] = f
    return out


def severity_word(sev: Any) -> str:
    return SEVERITY_LABELS.get(normalize_severity(sev), "Medium")


def parse_taxonomy_bracket(text: str) -> tuple[str | None, list[str]]:
    """Parse ``correctness:bug,edge-case`` or ``correctness`` from a bracket token."""
    raw = (text or "").strip()
    if not raw:
        return None, []
    if ":" in raw:
        cat_raw, labs_raw = raw.split(":", 1)
        return normalize_category(cat_raw), normalize_labels(labs_raw, category=None)
    # Could be category alone or a single label.
    key = raw.lower().replace("_", "-").replace(" ", "-")
    if key in CATEGORIES or key in _CATEGORY_ALIASES:
        return normalize_category(key), []
    labs = normalize_labels(raw, category=None)
    return infer_category_from_labels(labs), labs


def coerce_findings_for_persist(
    findings: list[Any] | str | None,
    *,
    default_origin: str = DEFAULT_ORIGIN,
    prior: list[Any] | str | None = None,
) -> list[dict[str, Any]]:
    """Parse + normalize findings for DB / workdir JSON persistence."""
    import json

    def _as_list(raw: list[Any] | str | None) -> list[Any]:
        if raw is None:
            return []
        if isinstance(raw, list):
            return raw
        text = str(raw).strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return data if isinstance(data, list) else []

    prior_list = [f for f in _as_list(prior) if isinstance(f, dict)]
    return normalize_findings_list(
        _as_list(findings),
        default_origin=default_origin,
        prior_by_id=findings_by_id(prior_list),
    )


def findings_to_json(
    findings: list[dict[str, Any]],
    *,
    indent: int | None = 2,
) -> str:
    """Serialize findings for findings.json / findings_json columns."""
    import json

    clean: list[dict[str, Any]] = []
    for f in findings:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("id") or f.get("external_id") or "").strip()
        row: dict[str, Any] = {
            "id": fid or f.get("id"),
            "severity": normalize_severity(f.get("severity")),
            "title": f.get("title") or "",
            "body": f.get("body") or "",
            "category": normalize_category(f.get("category")),
            "labels": list(f.get("labels") or []),
            "origin": normalize_origin(f.get("origin")),
            "human_edited": bool(f.get("human_edited")),
        }
        if f.get("path") not in (None, ""):
            row["path"] = f.get("path")
        if f.get("line") not in (None, ""):
            try:
                row["line"] = int(f["line"])
            except (TypeError, ValueError):
                pass
        if f.get("disposition") not in (None, ""):
            row["disposition"] = f.get("disposition")
        # Preserve verify class / extras used by publish heuristics.
        for key in ("class", "verify_class", "file"):
            if key in f and f[key] not in (None, ""):
                row[key] = f[key]
        clean.append(row)
    if indent is None:
        return json.dumps(clean)
    return json.dumps(clean, indent=indent) + "\n"
