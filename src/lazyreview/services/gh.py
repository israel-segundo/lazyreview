"""GitHub CLI wrappers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


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
            "number,title,url,baseRefName,headRefName,headRefOid,state,author,isDraft,additions,deletions,changedFiles",
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
    return {
        "owner": ref.owner,
        "repo": ref.repo,
        "number": data["number"],
        "url": data.get("url") or ref.url,
        "title": data.get("title") or "",
        "author": author,
        "base_ref": data.get("baseRefName") or "",
        "head_ref": data.get("headRefName") or "",
        "head_sha": data.get("headRefOid") or "",
        "state": state,
        "is_draft": bool(data.get("isDraft")),
        "additions": data.get("additions"),
        "deletions": data.get("deletions"),
        "changed_files": data.get("changedFiles"),
    }


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
) -> dict[str, Any]:
    """event: APPROVE | REQUEST_CHANGES | COMMENT"""
    payload = json.dumps({"body": body, "event": event})
    return _post_review(owner, repo, number, payload)


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
