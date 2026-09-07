"""Parse agent findings markdown into structured rows."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SEVERITY_MAP = {
    "high": 1,
    "sev1": 1,
    "1": 1,
    "medium": 2,
    "med": 2,
    "sev2": 2,
    "2": 2,
    "low": 3,
    "sev3": 3,
    "3": 3,
}

# ## F1 — [High] title  OR  ## F1 — High: title
_FINDING_HEADER = re.compile(
    r"^##\s+(?P<id>F\d+)\s*[—–-]\s*(?:\[(?P<sev1>[^\]]+)\]|(?P<sev2>\w+))\s*[:—–-]?\s*(?P<title>.+)\s*$",
    re.IGNORECASE,
)


def parse_findings_md(text: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    body_lines: list[str] = []

    def flush() -> None:
        nonlocal current, body_lines
        if current is None:
            return
        body = "\n".join(body_lines).strip()
        current["body"] = body
        m = re.search(r"`([^`:]+):(\d+)`", body) or re.search(
            r"\b([\w./-]+\.\w+):(\d+)\b", body
        )
        if m:
            current["path"] = m.group(1)
            try:
                current["line"] = int(m.group(2))
            except ValueError:
                pass
        findings.append(current)
        current = None
        body_lines = []

    for line in text.splitlines():
        m = _FINDING_HEADER.match(line.strip())
        if m:
            flush()
            sev_raw = (m.group("sev1") or m.group("sev2") or "medium").strip().lower()
            severity = SEVERITY_MAP.get(sev_raw, 2)
            current = {
                "external_id": m.group("id"),
                "severity": severity,
                "title": m.group("title").strip(),
                "path": None,
                "line": None,
                "disposition": None,
            }
            body_lines = []
            continue
        if current is not None:
            body_lines.append(line)
    flush()
    return findings


def load_findings_from_workdir(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "findings.md"
    if not path.exists():
        # Sometimes agents write under artifacts/
        alt = run_dir / "artifacts" / "findings.md"
        if alt.exists():
            path = alt
        else:
            return []
    return parse_findings_md(path.read_text(encoding="utf-8", errors="replace"))


def severity_counts(findings: list[dict[str, Any]]) -> tuple[int, int, int]:
    s1 = sum(1 for f in findings if f["severity"] == 1)
    s2 = sum(1 for f in findings if f["severity"] == 2)
    s3 = sum(1 for f in findings if f["severity"] == 3)
    return s1, s2, s3
