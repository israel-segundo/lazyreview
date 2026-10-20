"""GitHub CLI wrappers."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class GhError(RuntimeError):
    pass


def _require_gh() -> str:
    path = shutil.which("gh")
    if not path:
        raise GhError("gh CLI not found on PATH")
    return path


def run_gh(args: list[str], *, cwd: str | None = None, timeout: int = 120) -> str:
    gh = _require_gh()
    proc = subprocess.run(
        [gh, *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise GhError(err or f"gh {' '.join(args)} failed ({proc.returncode})")
    return proc.stdout


def auth_status() -> dict[str, Any]:
    try:
        out = run_gh(["auth", "status"])
        return {"ok": True, "detail": out.strip()}
    except GhError as e:
        return {"ok": False, "detail": str(e)}
    except FileNotFoundError:
        return {"ok": False, "detail": "gh not found"}


_PR_URL_RE = re.compile(
    r"github\.com[/:](?P<owner>[^/]+)/(?P<repo>[^/#]+)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


@dataclass
class PrRef:
    owner: str
    repo: str
    number: int
    url: str


def parse_pr_url(url_or_ref: str) -> PrRef:
    text = url_or_ref.strip()
    m = _PR_URL_RE.search(text)
    if m:
        owner, repo, number = m.group("owner"), m.group("repo"), int(m.group("number"))
        repo = repo.removesuffix(".git")
        return PrRef(
            owner=owner,
            repo=repo,
            number=number,
            url=f"https://github.com/{owner}/{repo}/pull/{number}",
        )
    # owner/repo#123 or owner/repo/pulls/123
    m2 = re.match(
        r"(?P<owner>[^/]+)/(?P<repo>[^/#]+)(?:#|/pulls?/|/pull/)(?P<number>\d+)$",
        text,
    )
    if m2:
        owner, repo, number = m2.group("owner"), m2.group("repo"), int(m2.group("number"))
        return PrRef(
            owner=owner,
            repo=repo,
            number=number,
            url=f"https://github.com/{owner}/{repo}/pull/{number}",
        )
    raise GhError(f"Could not parse PR reference: {url_or_ref}")


def view_pr(url_or_ref: str) -> dict[str, Any]:
    ref = parse_pr_url(url_or_ref)
    raw = run_gh(
        [
            "pr",
            "view",
            str(ref.number),
            "--repo",
            f"{ref.owner}/{ref.repo}",
            "--json",
            "number,title,url,baseRefName,headRefName,headRefOid,state,author,isDraft,additions,deletions,changedFiles,commits,reviews",
        ]
    )
    data = json.loads(raw)
    state = (data.get("state") or "open").lower()
    author = ""
    author_obj = data.get("author")
    if isinstance(author_obj, dict):
        author = author_obj.get("login") or author_obj.get("name") or ""
    elif isinstance(author_obj, str):
        author = author_obj
    head_sha = data.get("headRefOid") or ""
    head_updated_at = _head_committed_at(data.get("commits") or [], head_sha)
    return {
        "owner": ref.owner,
        "repo": ref.repo,
        "number": data["number"],
        "url": data.get("url") or ref.url,
        "title": data.get("title") or "",
        "author": author,
        "base_ref": data.get("baseRefName") or "",
        "head_ref": data.get("headRefName") or "",
        "head_sha": head_sha,
        "head_updated_at": head_updated_at,
        "state": state,
        "is_draft": bool(data.get("isDraft")),
        "additions": data.get("additions"),
        "deletions": data.get("deletions"),
        "changed_files": data.get("changedFiles"),
        "reviews": data.get("reviews") or [],
    }


_GH_REVIEW_TO_ACTION = {
    "APPROVED": "approve",
    "CHANGES_REQUESTED": "request_changes",
    "COMMENTED": "comment",
}


def gh_user_login() -> str:
    """Login for the authenticated gh CLI user."""
    raw = run_gh(["api", "user", "--jq", ".login"])
    login = (raw or "").strip().strip('"')
    if not login:
        raise GhError("Could not resolve authenticated gh user login")
    return login


def _review_commit_oid(item: dict[str, Any]) -> str:
    """Commit SHA a review was submitted against (gh pr view / REST shapes)."""
    direct = (item.get("commitOID") or item.get("commit_id") or "").strip()
    if direct:
        return direct
    commit = item.get("commit")
    if isinstance(commit, dict):
        return (commit.get("oid") or commit.get("OID") or "").strip()
    return ""


def parse_user_review_for_head(
    reviews: list[Any],
    *,
    head_sha: str,
    login: str,
) -> dict[str, Any] | None:
    """
    Latest non-dismissed review by ``login`` on ``head_sha``.

    Returns gh_action (approve | request_changes | comment), review id, body, url.
    """
    if not login or not head_sha:
        return None
    candidates: list[dict[str, Any]] = []
    for item in reviews:
        if not isinstance(item, dict):
            continue
        author = item.get("author")
        author_login = ""
        if isinstance(author, dict):
            author_login = author.get("login") or ""
        elif isinstance(author, str):
            author_login = author
        if author_login != login:
            continue
        state = (item.get("state") or "").upper()
        if state in ("DISMISSED", "PENDING"):
            continue
        commit = _review_commit_oid(item)
        if commit and commit != head_sha:
            continue
        action = _GH_REVIEW_TO_ACTION.get(state)
        if not action:
            continue
        candidates.append(
            {
                "gh_action": action,
                "gh_review_id": str(item.get("id") or ""),
                "body": item.get("body") or "",
                "html_url": item.get("url") or item.get("html_url"),
                "submitted_at": item.get("submittedAt") or item.get("submitted_at") or "",
            }
        )
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.get("submitted_at") or "")


def _head_committed_at(commits: list[Any], head_sha: str) -> str | None:
    """Best-effort commit timestamp for the PR head SHA."""
    if not isinstance(commits, list) or not commits:
        return None
    matched: dict[str, Any] | None = None
    if head_sha:
        for item in commits:
            if not isinstance(item, dict):
                continue
            if (item.get("oid") or "") == head_sha:
                matched = item
                break
    if matched is None:
        last = commits[-1]
        matched = last if isinstance(last, dict) else None
    if not matched:
        return None
    raw = matched.get("committedDate") or matched.get("authoredDate")
    return str(raw) if raw else None


def search_pending_review_prs(*, limit: int = 100) -> list[dict[str, Any]]:
    """Open PRs where the authenticated user is a requested reviewer (not terminal)."""
    raw = run_gh(
        [
            "search",
            "prs",
            "--review-requested",
            "@me",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            "number,title,url,repository,state",
        ],
        timeout=120,
    )
    items = json.loads(raw or "[]")
    results: list[dict[str, Any]] = []
    for item in items:
        repo_info = item.get("repository") or {}
        name_with_owner = repo_info.get("nameWithOwner") or ""
        if "/" in name_with_owner:
            owner, repo = name_with_owner.split("/", 1)
        else:
            owner, repo = "", repo_info.get("name") or ""
        if not owner or not repo:
            continue
        state = (item.get("state") or "open").lower()
        if state not in ("open",):
            continue
        results.append(
            {
                "owner": owner,
                "repo": repo,
                "number": int(item["number"]),
                "url": item.get("url")
                or f"https://github.com/{owner}/{repo}/pull/{item['number']}",
                "title": item.get("title") or "",
                "state": state,
            }
        )
    return results


def list_pr_comments(owner: str, repo: str, number: int) -> list[dict[str, Any]]:
    """Issue comments + review bodies for dedup."""
    comments: list[dict[str, Any]] = []
    issue = run_gh(
        [
            "api",
            f"repos/{owner}/{repo}/issues/{number}/comments",
            "--paginate",
        ]
    )
    for c in json.loads(issue or "[]"):
        comments.append(
            {
                "kind": "issue",
                "user": (c.get("user") or {}).get("login"),
                "body": c.get("body") or "",
                "url": c.get("html_url"),
            }
        )
    reviews = run_gh(
        [
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/comments",
            "--paginate",
        ]
    )
    for c in json.loads(reviews or "[]"):
        comments.append(
            {
                "kind": "review_inline",
                "user": (c.get("user") or {}).get("login"),
                "body": c.get("body") or "",
                "url": c.get("html_url"),
                "path": c.get("path"),
                "line": c.get("line"),
            }
        )
    return comments


def post_pr_comment(owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
    out = run_gh(
        [
            "pr",
            "comment",
            str(number),
            "--repo",
            f"{owner}/{repo}",
            "--body",
            body,
        ]
    )
    return {"ok": True, "output": out.strip()}


def submit_pr_review(
    owner: str,
    repo: str,
    number: int,
    body: str,
    event: str,
    *,
    commit_id: str | None = None,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """event: APPROVE | REQUEST_CHANGES | COMMENT.

    When ``comments`` is non-empty, ``commit_id`` (PR head SHA) is required by GitHub.
    On 422 from a bad inline anchor, drop offending comments and retry once; if still
    failing, retry with summary body only.
    """
    cleaned = _normalize_review_comments(comments or [])
    payload_obj: dict[str, Any] = {"body": body, "event": event}
    if cleaned:
        if not (commit_id or "").strip():
            raise GhError("commit_id is required when posting inline review comments")
        payload_obj["commit_id"] = commit_id.strip()
        payload_obj["comments"] = cleaned

    try:
        result = _post_review(owner, repo, number, json.dumps(payload_obj))
        result["dropped_comments"] = []
        return result
    except GhError as e:
        if not cleaned or not _is_unprocessable(e):
            raise
        # Retry without comments that GitHub rejected (line not in diff, etc.).
        dropped = list(cleaned)
        payload_retry = {"body": body, "event": event}
        logger.warning(
            "PR review with %s inline comment(s) rejected (%s); retrying without comments",
            len(cleaned),
            e,
        )
        result = _post_review(owner, repo, number, json.dumps(payload_retry))
        result["dropped_comments"] = dropped
        return result


def _normalize_review_comments(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in comments:
        if not isinstance(c, dict):
            continue
        path = str(c.get("path") or "").strip()
        try:
            line = int(c.get("line"))
        except (TypeError, ValueError):
            continue
        body = str(c.get("body") or "").strip()
        if not path or line < 1 or not body:
            continue
        side = str(c.get("side") or "RIGHT").strip().upper() or "RIGHT"
        if side not in {"LEFT", "RIGHT"}:
            side = "RIGHT"
        item: dict[str, Any] = {
            "path": path,
            "line": line,
            "side": side,
            "body": body,
        }
        if c.get("start_line") is not None:
            try:
                start = int(c["start_line"])
                if start >= 1:
                    item["start_line"] = start
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def _is_unprocessable(err: GhError) -> bool:
    msg = str(err).lower()
    return "422" in msg or "unprocessable" in msg or "must be part of the diff" in msg


def _post_review(owner: str, repo: str, number: int, payload: str) -> dict[str, Any]:
    gh = _require_gh()
    proc = subprocess.run(
        [
            gh,
            "api",
            f"repos/{owner}/{repo}/pulls/{number}/reviews",
            "-X",
            "POST",
            "--input",
            "-",
        ],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GhError((proc.stderr or proc.stdout or "").strip())
    data = json.loads(proc.stdout or "{}")
    return {
        "ok": True,
        "id": str(data.get("id", "")),
        "html_url": data.get("html_url"),
    }


def clone_url(owner: str, repo: str) -> str:
    # Prefer HTTPS; gh auth handles credentials
    return f"https://github.com/{owner}/{repo}.git"
