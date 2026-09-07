"""Shallow clone of a PR head into a run workdir."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from lazyreview.services.gh import GhError, clone_url


def clone_pr_head(
    *,
    owner: str,
    repo: str,
    head_sha: str,
    dest: Path,
    depth: int = 1,
) -> Path:
    """Clone repo into dest and checkout head_sha. Returns dest."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = clone_url(owner, repo)

    # Shallow clone then fetch the specific commit if needed
    proc = subprocess.run(
        ["git", "clone", "--depth", str(depth), url, str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GhError(f"git clone failed: {(proc.stderr or proc.stdout).strip()}")

    # Ensure we have the PR head commit
    fetch = subprocess.run(
        ["git", "fetch", "--depth", str(depth), "origin", head_sha],
        cwd=dest,
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch.returncode != 0:
        # Try fetching PR refs
        fetch2 = subprocess.run(
            ["git", "fetch", "origin", f"+refs/pull/*/head:refs/remotes/origin/pr/*"],
            cwd=dest,
            capture_output=True,
            text=True,
            check=False,
        )
        if fetch2.returncode != 0:
            # Last resort: deepen / unshallow fetch of sha
            subprocess.run(
                ["git", "fetch", "--depth", "50", "origin", head_sha],
                cwd=dest,
                capture_output=True,
                text=True,
                check=False,
            )

    checkout = subprocess.run(
        ["git", "checkout", "--force", head_sha],
        cwd=dest,
        capture_output=True,
        text=True,
        check=False,
    )
    if checkout.returncode != 0:
        raise GhError(
            f"git checkout {head_sha} failed: {(checkout.stderr or checkout.stdout).strip()}"
        )
    return dest


def remove_repo_dir(repo_dir: Path) -> None:
    if repo_dir.exists():
        shutil.rmtree(repo_dir, ignore_errors=True)
