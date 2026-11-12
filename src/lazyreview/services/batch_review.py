"""Batch review creation and optional auto-kick after analysis succeeds."""

from __future__ import annotations

import logging
import random
import sqlite3
from typing import Any

from lazyreview.agents import get_adapter
from lazyreview.db import (
    create_consolidation,
    create_run,
    get_pr,
    get_setting,
    list_runs_for_pr,
    update_consolidation,
    update_run,
)
from lazyreview.services.agent_eligibility import require_selectable_agent
from lazyreview.services.analysis_eligibility import review_blocked_reason
from lazyreview.services.analysis_gate import analysis_ready
from lazyreview.services.settings import (
    get_auto_kick_reviews,
    get_batch_randomize_models_effort,
    get_consolidator_config,
    parse_batch_review_defaults,
)

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SKILL = "pr-code-review-batch"


def randomize_batch_run_configs(
    runs: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Keep each row's agent; pick a random model (and effort) from that agent."""
    out: list[dict[str, str]] = []
    for row in runs:
        agent = str(row.get("agent") or "").strip().lower()
        model = str(row.get("model") or "").strip()
        effort = str(row.get("effort") or "").strip()
        if not agent:
            out.append({"agent": agent, "model": model, "effort": effort})
            continue
        try:
            adapter = get_adapter(agent)
            models = list(adapter.list_models() or [])
        except Exception:
            logger.exception("failed listing models for agent=%s; keeping defaults", agent)
            models = []
        if models:
            chosen = random.choice(models)
            model = str(chosen.id)
            efforts = [str(e) for e in (chosen.efforts or []) if str(e).strip()]
            effort = random.choice(efforts) if efforts else ""
        out.append({"agent": agent, "model": model, "effort": effort})
    return out


def resolve_batch_run_configs(
    conn: sqlite3.Connection,
    *,
    runs: list[dict[str, Any]] | None = None,
    apply_randomize: bool | None = None,
) -> list[dict[str, str]]:
    """Resolve run configs from explicit rows or stored batch defaults."""
    if runs is None:
        raw = get_setting(conn, "batch_review_defaults")
        configs = parse_batch_review_defaults(raw)
    else:
        configs = [
            {
                "agent": str(r.get("agent") or "").strip().lower(),
                "model": str(r.get("model") or "").strip(),
                "effort": "" if r.get("effort") is None else str(r.get("effort") or "").strip(),
            }
            for r in runs
        ]
    if apply_randomize is None:
        apply_randomize = get_batch_randomize_models_effort(conn)
    if apply_randomize:
        configs = randomize_batch_run_configs(configs)
    return configs


def _has_review_work_for_head(
    conn: sqlite3.Connection, pr_id: int, head_sha: str
) -> bool:
    head = (head_sha or "").strip()
    if not head:
        return False
    for run in list_runs_for_pr(conn, pr_id):
        if int(run.get("deprecated") or 0):
            continue
        if (run.get("head_sha") or "").strip() != head:
            continue
        return True
    return False


def create_batch_review(
    conn: sqlite3.Connection,
    settings: Any,
    jobs: Any,
    *,
    pr_id: int,
    runs: list[dict[str, Any]] | None = None,
    skill: str = DEFAULT_BATCH_SKILL,
    apply_randomize: bool | None = None,
) -> dict[str, Any]:
    """Create review runs + dormant consolidation for a PR.

    Raises ValueError on validation / gate failures.
    """
    pr = get_pr(conn, pr_id)
    if not pr:
        raise ValueError("PR not found")
    blocked = review_blocked_reason(pr)
    if blocked:
        raise ValueError(blocked)
    if not analysis_ready(conn, pr_id):
        raise ValueError("PR analysis has not completed for the current head")

    configs = resolve_batch_run_configs(
        conn, runs=runs, apply_randomize=apply_randomize
    )
    if not configs:
        raise ValueError("No batch review configurations")

    consolidator_cfg = get_consolidator_config(conn)
    try:
        get_adapter(consolidator_cfg["agent"])
    except KeyError as e:
        raise ValueError(str(e)) from e
    try:
        require_selectable_agent(settings, conn, consolidator_cfg["agent"])
    except ValueError:
        raise

    settings.ensure_dirs()
    created: list[dict[str, Any]] = []
    for item in configs:
        agent = item["agent"]
        model = item["model"]
        effort = item.get("effort") or None
        if not agent or not model:
            raise ValueError("Each run needs an agent and model")
        try:
            get_adapter(agent)
        except KeyError as e:
            raise ValueError(str(e)) from e
        require_selectable_agent(settings, conn, agent)
        run = create_run(
            conn,
            pr_id=int(pr["id"]),
            agent=agent,
            model=model,
            effort=effort,
            skill=skill,
            head_sha=pr["head_sha"],
            workdir_path="",
        )
        run_dir = settings.runs_dir / str(run["id"])
        run_dir.mkdir(parents=True, exist_ok=True)
        run = update_run(conn, int(run["id"]), workdir_path=str(run_dir)) or run
        jobs.enqueue(int(run["id"]))
        created.append(run)

    from lazyreview.services.consolidation_jobs import consolidations_dir

    consolidation = create_consolidation(
        conn,
        pr_id=int(pr["id"]),
        head_sha=pr.get("head_sha") or "",
        source_run_ids=[int(r["id"]) for r in created],
        agent=consolidator_cfg["agent"],
        model=consolidator_cfg["model"],
        effort=consolidator_cfg["effort"] or None,
        workdir_path="",
        status="dormant",
    )
    work_dir = consolidations_dir(settings) / str(consolidation["id"])
    work_dir.mkdir(parents=True, exist_ok=True)
    consolidation = (
        update_consolidation(
            conn, int(consolidation["id"]), workdir_path=str(work_dir)
        )
        or consolidation
    )
    return {
        "runs": created,
        "count": len(created),
        "consolidation": consolidation,
    }


def maybe_auto_kick_reviews(
    conn: sqlite3.Connection,
    settings: Any,
    jobs: Any,
    pr_id: int,
) -> dict[str, Any] | None:
    """Start batch review+consolidation when auto-kick is enabled and safe.

    Returns the batch result, or None when skipped.
    """
    if not get_auto_kick_reviews(conn):
        return None
    pr = get_pr(conn, pr_id)
    if not pr:
        return None
    blocked = review_blocked_reason(pr)
    if blocked:
        logger.info("auto-kick skipped for pr_id=%s: %s", pr_id, blocked)
        return None
    if not analysis_ready(conn, pr_id):
        logger.info("auto-kick skipped for pr_id=%s: analysis not ready", pr_id)
        return None
    head_sha = (pr.get("head_sha") or "").strip()
    if _has_review_work_for_head(conn, pr_id, head_sha):
        logger.info(
            "auto-kick skipped for pr_id=%s: review work already exists for head %s",
            pr_id,
            head_sha[:12],
        )
        return None
    defaults = parse_batch_review_defaults(get_setting(conn, "batch_review_defaults"))
    if not defaults:
        logger.info("auto-kick skipped for pr_id=%s: no batch defaults", pr_id)
        return None
    try:
        result = create_batch_review(
            conn,
            settings,
            jobs,
            pr_id=pr_id,
            runs=defaults,
            apply_randomize=get_batch_randomize_models_effort(conn),
        )
    except Exception:
        logger.exception("auto-kick failed for pr_id=%s", pr_id)
        return None
    logger.info(
        "auto-kick started %s review run(s) for pr_id=%s consolidation_id=%s",
        result.get("count"),
        pr_id,
        (result.get("consolidation") or {}).get("id"),
    )
    return result
