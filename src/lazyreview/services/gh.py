"""GitHub CLI wrappers."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
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


def _gh_error_message(stderr: str, stdout: str, *, fallback: str) -> str:
    """Prefer GitHub's JSON error body over opaque gh decoder failures."""
    stderr = (stderr or "").strip()
    stdout = (stdout or "").strip()
    opaque = "unexpected end of json input" in stderr.lower()
    parsed_bits: list[str] = []
    if stdout:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            msg = data.get("message")
            if isinstance(msg, str) and msg.strip():
                parsed_bits.append(msg.strip())
            errors = data.get("errors")
            if isinstance(errors, list):
                for item in errors:
                    if isinstance(item, str) and item.strip():
                        parsed_bits.append(item.strip())
                    elif isinstance(item, dict):
                        m = item.get("message")
                        if isinstance(m, str) and m.strip():
                            parsed_bits.append(m.strip())
            status = data.get("status")
            if status is not None and "http" not in stderr.lower():
                parsed_bits.append(f"HTTP {status}")
    if opaque and parsed_bits:
        return "; ".join(parsed_bits)
    if opaque and not parsed_bits:
        return (
            "GitHub API returned an empty error body "
            "(transient; unexpected end of JSON input)"
        )
    if stderr:
        if parsed_bits and "unprocessable" in stderr.lower():
            return f"{stderr} — {'; '.join(parsed_bits)}"
        return stderr
    if parsed_bits:
        return "; ".join(parsed_bits)
    if stdout:
        return stdout
    return fallback


def _is_transient_gh_error(err: GhError) -> bool:
    msg = str(err).lower()
    return any(
        token in msg
        for token in (
            "unexpected end of json input",
            "empty error body",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "timeout",
            "temporar",
            "connection reset",
            "connection refused",
            "i/o timeout",
        )
    )


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


def _pr_in_merge_queue(owner: str, repo: str, number: int) -> bool:
    """GitHub merge-queue membership (not exposed by ``gh pr view --json``)."""
    query = (
        "query($owner:String!,$repo:String!,$number:Int!){"
        "repository(owner:$owner,name:$repo){"
        "pullRequest(number:$number){isInMergeQueue}"
        "}}"
    )
    try:
        raw = run_gh(
            [
                "api",
                "graphql",
                "-F",
                f"owner={owner}",
                "-F",
                f"repo={repo}",
                "-F",
                f"number={int(number)}",
                "-f",
                f"query={query}",
            ]
        )
        data = json.loads(raw or "{}")
        pr = ((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
        return bool(pr.get("isInMergeQueue"))
    except (GhError, TypeError, ValueError, json.JSONDecodeError) as e:
        logger.warning(
            "Could not resolve merge-queue state for %s/%s#%s: %s",
            owner,
            repo,
            number,
            e,
        )
        return False


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
    in_merge_queue = False
    if state == "open":
        in_merge_queue = _pr_in_merge_queue(ref.owner, ref.repo, int(data["number"]))
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
        "in_merge_queue": in_merge_queue,
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


def _review_author_login(item: dict[str, Any]) -> str:
    author = item.get("author")
    if isinstance(author, dict):
        return author.get("login") or ""
    if isinstance(author, str):
        return author
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
        if _review_author_login(item) != login:
            continue
        state = (item.get("state") or "").upper()
        if state in ("DISMISSED", "PENDING"):
            continue
        commit = _review_commit_oid(item)
        # Require an explicit head match when GitHub reports a commit. Reviews
        # with no commit oid are ignored so a stale approval cannot attach to a
        # newer head after "dismiss stale approvals" fires.
        if not commit or commit != head_sha:
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


def user_has_dismissed_review(reviews: list[Any], *, login: str) -> bool:
    """True if ``login`` has at least one DISMISSED review on the PR."""
    if not login:
        return False
    for item in reviews:
        if not isinstance(item, dict):
            continue
        if _review_author_login(item) != login:
            continue
        if (item.get("state") or "").upper() == "DISMISSED":
            return True
    return False


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


def list_pr_changed_paths(owner: str, repo: str, number: int) -> frozenset[str]:
    """Paths touched by the PR (for inline review comment anchoring)."""
    from lazyreview.services.pr_diff import list_pr_file_patches

    return frozenset(list_pr_file_patches(owner, repo, number).keys())


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
    changed_paths: set[str] | frozenset[str] | None = None,
    commentable_lines: dict[str, frozenset[int]] | None = None,
) -> dict[str, Any]:
    """event: APPROVE | REQUEST_CHANGES | COMMENT.

    When ``comments`` is non-empty, ``commit_id`` (PR head SHA) is required by GitHub.
    ``changed_paths`` / ``commentable_lines`` pre-filter anchors GitHub rejects
    ("Path could not be resolved", "Line could not be resolved"). On 422 for the
    remaining batch, posts the summary review then attaches all inlines in one
    COMMENT follow-up review.
    """
    cleaned = _normalize_review_comments(comments or [])
    skipped_off_diff: list[dict[str, Any]] = []
    skipped_bad_line: list[dict[str, Any]] = []
    if cleaned and changed_paths is not None:
        kept: list[dict[str, Any]] = []
        for c in cleaned:
            if c["path"] in changed_paths:
                kept.append(c)
            else:
                skipped_off_diff.append(c)
        if skipped_off_diff:
            logger.info(
                "Skipping %s inline comment(s) on paths outside PR diff",
                len(skipped_off_diff),
            )
        cleaned = kept
    if cleaned and commentable_lines is not None:
        from lazyreview.services.pr_diff import filter_comments_for_diff_lines

        cleaned, skipped_bad_line = filter_comments_for_diff_lines(
            cleaned, commentable_lines, snap=True
        )
        if skipped_bad_line:
            logger.info(
                "Skipping %s inline comment(s) on non-commentable diff lines",
                len(skipped_bad_line),
            )

    sha = (commit_id or "").strip()
    payload_obj: dict[str, Any] = {"body": body, "event": event}
    if cleaned:
        if not sha:
            raise GhError("commit_id is required when posting inline review comments")
        payload_obj["commit_id"] = sha
        payload_obj["comments"] = cleaned

    # A leftover PENDING review blocks every subsequent create-review call.
    _discard_pending_reviews(owner, repo, number)

    try:
        result = _post_review(owner, repo, number, json.dumps(payload_obj))
        result["dropped_comments"] = list(skipped_off_diff) + list(skipped_bad_line)
        result["posted_inline_count"] = len(cleaned)
        result["skipped_off_diff_count"] = len(skipped_off_diff)
        result["skipped_bad_line_count"] = len(skipped_bad_line)
        return result
    except GhError as e:
        if _is_pending_review_conflict(e):
            _discard_pending_reviews(owner, repo, number)
            result = _post_review(owner, repo, number, json.dumps(payload_obj))
            result["dropped_comments"] = list(skipped_off_diff) + list(skipped_bad_line)
            result["posted_inline_count"] = len(cleaned)
            result["skipped_off_diff_count"] = len(skipped_off_diff)
            result["skipped_bad_line_count"] = len(skipped_bad_line)
            return result
        if not cleaned or not _is_unprocessable(e):
            raise
        logger.warning(
            "PR review with %s inline comment(s) rejected (%s); "
            "posting summary then attaching comments individually",
            len(cleaned),
            e,
        )
        payload_retry = {"body": body, "event": event}
        result = _post_review(owner, repo, number, json.dumps(payload_retry))
        posted, attach_dropped = _post_inline_comments_one_review(
            owner,
            repo,
            number,
            commit_id=sha,
            comments=cleaned,
        )
        all_dropped = list(skipped_off_diff) + list(skipped_bad_line) + attach_dropped
        result["dropped_comments"] = all_dropped
        result["posted_inline_count"] = len(posted)
        result["skipped_off_diff_count"] = len(skipped_off_diff)
        result["skipped_bad_line_count"] = len(skipped_bad_line)
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
    if _is_pending_review_conflict(err):
        return False
    return "422" in msg or "unprocessable" in msg or "must be part of the diff" in msg


def _is_pending_review_conflict(err: GhError) -> bool:
    msg = str(err).lower()
    return "pending review" in msg


def _discard_pending_reviews(owner: str, repo: str, number: int) -> int:
    """Delete the caller's PENDING reviews so create-review is not blocked."""
    try:
        raw = run_gh(
            ["api", f"repos/{owner}/{repo}/pulls/{number}/reviews", "--paginate"]
        )
        reviews = json.loads(raw or "[]")
    except (GhError, json.JSONDecodeError) as e:
        logger.warning("Could not list reviews before post: %s", e)
        return 0
    if not isinstance(reviews, list):
        return 0
    deleted = 0
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if str(review.get("state") or "").upper() != "PENDING":
            continue
        rid = review.get("id")
        if rid is None:
            continue
        try:
            run_gh(
                [
                    "api",
                    "-X",
                    "DELETE",
                    f"repos/{owner}/{repo}/pulls/{number}/reviews/{rid}",
                ]
            )
            deleted += 1
            logger.info("Deleted PENDING review %s on %s/%s#%s", rid, owner, repo, number)
        except GhError as e:
            logger.warning("Could not delete PENDING review %s: %s", rid, e)
    return deleted


_INLINE_FOLLOWUP_BODY = (
    "Inline review comments for the review above (posted as one follow-up)."
)

_POST_REVIEW_ATTEMPTS = 3
_POST_REVIEW_RETRY_DELAY_SEC = 0.75


def _post_inline_comments_one_review(
    owner: str,
    repo: str,
    number: int,
    *,
    commit_id: str,
    comments: list[dict[str, Any]],
    review_body: str = _INLINE_FOLLOWUP_BODY,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Attach all inlines under a single COMMENT review (never one review per inline)."""
    if not comments:
        return [], []
    payload_obj: dict[str, Any] = {
        "body": review_body,
        "event": "COMMENT",
        "commit_id": commit_id,
        "comments": comments,
    }
    try:
        _post_review(owner, repo, number, json.dumps(payload_obj))
        return list(comments), []
    except GhError as err:
        if not _is_unprocessable(err) or len(comments) <= 1:
            logger.warning("Inline follow-up review failed (%s)", err)
            return [], list(comments)
    # Drop one comment at a time until GitHub accepts the batch (small N).
    posted: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    remaining = list(comments)
    while remaining:
        payload_obj["comments"] = remaining
        try:
            _post_review(owner, repo, number, json.dumps(payload_obj))
            posted.extend(remaining)
            return posted, dropped
        except GhError as err:
            if not _is_unprocessable(err):
                dropped.extend(remaining)
                return posted, dropped
            dropped.append(remaining.pop())
    return posted, dropped


def _post_review(owner: str, repo: str, number: int, payload: str) -> dict[str, Any]:
    """POST a pull-request review via ``gh api``.

    Writes the JSON body to a temp file (not stdin). Empty ``--input -`` creates a
    PENDING review on GitHub and then blocks every later submit with 422.
    Retries when ``gh`` reports ``unexpected end of JSON input`` — that usually
    means GitHub returned an empty error body (transient 5xx).
    """
    if not (payload or "").strip():
        raise GhError("refusing to post empty review payload")
    try:
        json.loads(payload)
    except json.JSONDecodeError as e:
        raise GhError(f"invalid review payload JSON: {e}") from e

    gh = _require_gh()
    last_err: GhError | None = None
    for attempt in range(1, _POST_REVIEW_ATTEMPTS + 1):
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".json",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp_path = Path(tmp.name)
        try:
            proc = subprocess.run(
                [
                    gh,
                    "api",
                    f"repos/{owner}/{repo}/pulls/{number}/reviews",
                    "-X",
                    "POST",
                    "--input",
                    str(tmp_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

        if proc.returncode != 0:
            last_err = GhError(
                _gh_error_message(
                    proc.stderr,
                    proc.stdout,
                    fallback=f"gh api review post failed ({proc.returncode})",
                )
            )
            if attempt < _POST_REVIEW_ATTEMPTS and _is_transient_gh_error(last_err):
                logger.warning(
                    "Transient GitHub review post failure (attempt %s/%s): %s",
                    attempt,
                    _POST_REVIEW_ATTEMPTS,
                    last_err,
                )
                time.sleep(_POST_REVIEW_RETRY_DELAY_SEC * attempt)
                continue
            raise last_err

        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as e:
            last_err = GhError(
                f"GitHub review response was not valid JSON: {e}; "
                f"stdout={proc.stdout[:200]!r}"
            )
            if attempt < _POST_REVIEW_ATTEMPTS:
                logger.warning(
                    "Unparseable review response (attempt %s/%s): %s",
                    attempt,
                    _POST_REVIEW_ATTEMPTS,
                    last_err,
                )
                time.sleep(_POST_REVIEW_RETRY_DELAY_SEC * attempt)
                continue
            raise last_err from e

        if not isinstance(data, dict):
            data = {}
        return {
            "ok": True,
            "id": str(data.get("id", "")),
            "html_url": data.get("html_url"),
        }

    assert last_err is not None
    raise last_err


def clone_url(owner: str, repo: str) -> str:
    # Prefer HTTPS; gh auth handles credentials
    return f"https://github.com/{owner}/{repo}.git"
