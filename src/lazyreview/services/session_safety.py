"""Safety helpers for interactive PR chat sessions."""

from __future__ import annotations

import re
from pathlib import Path

# Cheap preflight: block obviously destructive / exfil user messages before spawn.
_DESTRUCTIVE_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*f|--force).*(\$HOME|~|/home/)", re.I),
    re.compile(r"\brm\s+-rf\s+/", re.I),
    re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", re.I),  # fork bomb
    re.compile(r"\b(curl|wget)\b.*\|\s*(ba)?sh\b", re.I),
    re.compile(r"\bchmod\s+-R\s+777\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bdd\s+if=", re.I),
]

# Host / filesystem recon outside the PR worktree.
_HOST_RECON_PATTERNS = [
    re.compile(r"(?:^|[\s\"'`])(/home/|\$HOME(?:/|\b)|~/)", re.I),
    re.compile(r"(?:^|[\s\"'`])/etc/", re.I),
    re.compile(
        r"\b(list|show|print|cat|read|dump|explore)\b.{0,40}\b"
        r"(host|machine|server)\b.{0,40}\b(dir|directory|directories|filesystem|files)\b",
        re.I,
    ),
    re.compile(
        r"\b(directory tree|tree of)\b.{0,30}\b(machine|host|server|home)\b",
        re.I,
    ),
    re.compile(
        r"\b(files?|dirs?|directories)\b.{0,40}\boutside\b.{0,20}\b"
        r"(pr|worktree|repo|repository)\b",
        re.I,
    ),
]

# Secrets / env / prompt extraction (intent + target; avoid blocking PR code Qs).
_SECRETS_PATTERNS = [
    re.compile(
        r"\b(print|show|dump|reveal|exfiltrate|leak)\b.{0,40}\b"
        r"(env(ironment)?(\s+vars?)?|secrets?|credentials?|api[_ ]?keys?|tokens?)\b",
        re.I,
    ),
    re.compile(
        r"\b(print|show|dump|cat|read|reveal)\b.{0,40}\b"
        r"(credentials\.json|\.env)\b",
        re.I,
    ),
    re.compile(
        r"\b(print|show|dump|reveal|exfiltrate|leak)\b.{0,40}\b"
        r"(GH_TOKEN|ANTHROPIC_API_KEY|OPENAI_API_KEY|CURSOR_API_KEY|"
        r"CLAUDE_CODE_OAUTH_TOKEN|CODEX_API_KEY|GEMINI_API_KEY)\b",
        re.I,
    ),
    re.compile(
        r"\b(reveal|show|print|dump|repeat)\b.{0,50}\b"
        r"(system|developer|hidden)\b.{0,20}\b(prompt|instructions?|rules?)\b",
        re.I,
    ),
    re.compile(
        r"\b(what|show|print)\b.{0,30}\b(your|the)\b.{0,20}\b"
        r"(system prompt|hidden instructions?|developer message)\b",
        re.I,
    ),
]

# Jailbreak / role override.
_JAILBREAK_PATTERNS = [
    re.compile(r"\bignore\b.{0,40}\b(previous|prior|all|above)\b.{0,20}\binstructions?\b", re.I),
    re.compile(r"\byou are (now )?unrestricted\b", re.I),
    re.compile(r"\bdo anything now\b|\bDAN\b", re.I),
    re.compile(r"\bjailbreak\b", re.I),
    re.compile(r"\boverride\b.{0,30}\b(guardrails?|safety|system)\b", re.I),
    re.compile(r"\bpretend\b.{0,30}\b(no (rules|restrictions)|unfiltered)\b", re.I),
]

# Clearly off-business (high precision; avoid blocking PR questions).
_OFFTOPIC_PATTERNS = [
    re.compile(
        r"\b(what('s| is) the weather|weather (today|forecast)|temperature outside)\b",
        re.I,
    ),
    re.compile(
        r"\b(write|compose)\b.{0,20}\b(me )?(a |an )?(poem|sonnet|haiku|limerick)\b",
        re.I,
    ),
    re.compile(
        r"\b(recipe for|how (do|to) (I |you )?(cook|bake|make))\b.{0,40}\b"
        r"(cake|cookie|pizza|pasta|bread|soup)\b",
        re.I,
    ),
    re.compile(
        r"\b(explain|tell me about)\b.{0,20}\b"
        r"(quantum physics|general relativity|stock market|cryptocurrency trading)\b",
        re.I,
    ),
    re.compile(
        r"\b(help (me )?with|do )?(my )?(homework|math homework|essay)\b",
        re.I,
    ),
    re.compile(
        r"\b(tell me a joke|fun fact|who (won|is playing) the (game|match))\b",
        re.I,
    ),
]

_BLOCK_GROUPS: list[tuple[str, list[re.Pattern[str]]]] = [
    (
        "Blocked: message looks like a destructive or exfil shell request. "
        "Chat only allows scoped tools inside this PR worktree.",
        _DESTRUCTIVE_PATTERNS,
    ),
    (
        "Blocked: chat cannot inspect host paths or directories outside this PR worktree. "
        "Ask about files in the PR checkout instead.",
        _HOST_RECON_PATTERNS,
    ),
    (
        "Blocked: chat cannot reveal secrets, environment variables, credentials, "
        "or system/developer prompts.",
        _SECRETS_PATTERNS,
    ),
    (
        "Blocked: jailbreak or role-override requests are ignored. "
        "Ask about this PR’s code, findings, or draft instead.",
        _JAILBREAK_PATTERNS,
    ),
    (
        "Blocked: that question is outside LazyReview chat. "
        "Ask about this PR’s diff, analysis, findings, or GitHub draft.",
        _OFFTOPIC_PATTERNS,
    ),
]

# Soft warn: cross-PR action language (still sent to model with scope reminder).
_CROSS_PR = re.compile(
    r"\b(approve|request[_ ]changes|review|merge|comment on)\b.{0,40}\bPR\s*#?\s*(\d+)\b",
    re.I,
)

# Output redaction.
_SECRET_TOKEN_PATTERNS = [
    re.compile(r"\bghp_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgho_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bghu_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bghr_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9\-_]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(
        r"\b(?:API_KEY|SECRET|TOKEN|PASSWORD|GH_TOKEN|ANTHROPIC_API_KEY|"
        r"OPENAI_API_KEY|CURSOR_API_KEY|CODEX_API_KEY|GEMINI_API_KEY)"
        r"\s*=\s*['\"]?[^\s'\"]{8,}",
        re.I,
    ),
]
_HOME_PATH = re.compile(r"(?<![A-Za-z0-9_])/home/[^\s`'\"<>\])},]+")


def preflight_user_message(text: str) -> tuple[str | None, str | None]:
    """Return (hard_block_reason, soft_warn) — either may be None."""
    raw = text or ""
    for reason, patterns in _BLOCK_GROUPS:
        for pat in patterns:
            if pat.search(raw):
                return reason, None
    warn = None
    m = _CROSS_PR.search(raw)
    if m:
        warn = (
            f"Note: this session is locked to one PR. "
            f"Requests about other PRs (e.g. #{m.group(2)}) will be refused."
        )
    return None, warn


def redact_session_reply(
    text: str,
    *,
    allow_path_prefixes: list[str] | None = None,
) -> str:
    """Mask credential-shaped tokens and host home paths outside allowed prefixes."""
    out = text or ""
    for pat in _SECRET_TOKEN_PATTERNS:
        out = pat.sub("[redacted-secret]", out)

    allowed: list[str] = []
    for p in allow_path_prefixes or []:
        s = str(p or "").rstrip("/")
        if s:
            allowed.append(s)

    def _replace_home(m: re.Match[str]) -> str:
        path = m.group(0)
        for prefix in allowed:
            if path == prefix or path.startswith(prefix + "/"):
                return path
        return "[redacted-path]"

    out = _HOME_PATH.sub(_replace_home, out)
    return out


def session_allow_path_prefixes(*paths: Path | str | None) -> list[str]:
    """Normalize worktree / session workdir paths for reply redaction allowlists."""
    out: list[str] = []
    for p in paths:
        if p is None:
            continue
        try:
            out.append(str(Path(p).resolve()).rstrip("/"))
        except OSError:
            out.append(str(p).rstrip("/"))
    return out


# Claude --allowedTools for session runs (tighter than review runs).
SESSION_ALLOWED_TOOLS = (
    "Read,Glob,Grep,"
    "Bash(git status *),Bash(git diff *),Bash(git log *),"
    "Bash(git show *),Bash(git blame *),Bash(git rev-parse *),"
    "Bash(pytest *),Bash(python -m pytest *),Bash(npm test *),"
    "Bash(npm run test *),Bash(cargo test *),Bash(go test *)"
)
