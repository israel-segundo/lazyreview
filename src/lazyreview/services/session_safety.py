"""Safety helpers for interactive PR chat sessions."""

from __future__ import annotations

import re

# Cheap preflight: block obviously destructive / exfil user messages before spawn.
_BLOCK_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f|--force).*(\$HOME|~|/home/)", re.I),
    re.compile(r"\brm\s+-rf\s+/", re.I),
    re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", re.I),  # fork bomb
    re.compile(r"\b(curl|wget)\b.*\|\s*(ba)?sh\b", re.I),
    re.compile(r"\bchmod\s+-R\s+777\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
]

# Soft warn: cross-PR action language (still sent to model with scope reminder).
_CROSS_PR = re.compile(
    r"\b(approve|request[_ ]changes|review|merge|comment on)\b.{0,40}\bPR\s*#?\s*(\d+)\b",
    re.I,
)


def preflight_user_message(text: str) -> tuple[str | None, str | None]:
    """Return (hard_block_reason, soft_warn) — either may be None."""
    raw = text or ""
    for pat in _BLOCK_PATTERNS:
        if pat.search(raw):
            return (
                "Blocked: message looks like a destructive or exfil shell request. "
                "Chat only allows scoped tools inside this PR worktree.",
                None,
            )
    warn = None
    m = _CROSS_PR.search(raw)
    if m:
        warn = (
            f"Note: this session is locked to one PR. "
            f"Requests about other PRs (e.g. #{m.group(2)}) will be refused."
        )
    return None, warn


# Claude --allowedTools for session runs (tighter than review runs).
SESSION_ALLOWED_TOOLS = (
    "Read,Glob,Grep,"
    "Bash(git status *),Bash(git diff *),Bash(git log *),"
    "Bash(git show *),Bash(git blame *),Bash(git rev-parse *),"
    "Bash(pytest *),Bash(python -m pytest *),Bash(npm test *),"
    "Bash(npm run test *),Bash(cargo test *),Bash(go test *)"
)
