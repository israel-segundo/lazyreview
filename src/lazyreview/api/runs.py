from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from lazyreview.agents import get_adapter
from lazyreview.db import create_run, get_pr, get_run, list_findings, upsert_pr
from lazyreview.services.gh import GhError, view_pr

router = APIRouter(tags=["runs"])


class CreateRunBody(BaseModel):
    pr_id: int | None = None
    url: str | None = None
    agent: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    effort: str | None = None
    skill: str = "pr-code-review-batch"


class BatchRunItem(BaseModel):
    agent: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    effort: str | None = None


class BatchCreateBody(BaseModel):
    pr_id: int
    runs: list[BatchRunItem] = Field(..., min_length=1, max_length=20)
    skill: str = "pr-code-review-batch"
    consolidate_when_done: bool = True


def _create_one_run(conn, settings, jobs, *, pr, agent: str, model: str, effort: str | None, skill: str):
    try:
        adapter = get_adapter(agent)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not adapter.available():
        raise HTTPException(
            status_code=400, detail=f"Agent {agent} binary not available on PATH"
        )
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
    from lazyreview.db import update_run

    run = update_run(conn, int(run["id"]), workdir_path=str(run_dir)) or run
    jobs.enqueue(int(run["id"]))
    return run


@router.post("/runs")
def create_review_run(body: CreateRunBody, request: Request):
    conn = request.app.state.db
    settings = request.app.state.settings
    pr = None
    if body.pr_id is not None:
        pr = get_pr(conn, body.pr_id)
        if not pr:
            raise HTTPException(status_code=404, detail="PR not found")
    elif body.url:
        try:
            data = view_pr(body.url)
        except GhError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        pr = upsert_pr(
            conn,
            owner=data["owner"],
            repo=data["repo"],
            number=data["number"],
            url=data["url"],
            title=data["title"],
            author=data.get("author") or "",
            base_ref=data["base_ref"],
            head_ref=data["head_ref"],
            head_sha=data["head_sha"],
            state=data.get("state") or "open",
            is_draft=bool(data.get("is_draft")),
            additions=data.get("additions"),
            deletions=data.get("deletions"),
            changed_files=data.get("changed_files"),
        )
    else:
        raise HTTPException(status_code=400, detail="pr_id or url required")

    settings.ensure_dirs()
    return _create_one_run(
        conn,
        settings,
        request.app.state.jobs,
        pr=pr,
        agent=body.agent,
        model=body.model,
        effort=body.effort,
        skill=body.skill,
    )


@router.post("/runs/batch")
def create_review_runs_batch(body: BatchCreateBody, request: Request):
    conn = request.app.state.db
    settings = request.app.state.settings
    pr = get_pr(conn, body.pr_id)
    if not pr:
        raise HTTPException(status_code=404, detail="PR not found")
    settings.ensure_dirs()

    consolidator_cfg = None
    if body.consolidate_when_done:
        from lazyreview.services.settings import get_consolidator_config

        consolidator_cfg = get_consolidator_config(conn)
        try:
            adapter = get_adapter(consolidator_cfg["agent"])
        except KeyError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not adapter.available():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Consolidator agent {consolidator_cfg['agent']} binary not available on PATH"
                ),
            )

    created = []
    for item in body.runs:
        created.append(
            _create_one_run(
                conn,
                settings,
                request.app.state.jobs,
                pr=pr,
                agent=item.agent,
                model=item.model,
                effort=item.effort,
                skill=body.skill,
            )
        )

    consolidation = None
    if body.consolidate_when_done:
        from lazyreview.db import create_consolidation, update_consolidation
        from lazyreview.services.consolidation_jobs import consolidations_dir

        assert consolidator_cfg is not None
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


@router.get("/runs/{run_id}")
def get_review_run(run_id: int, request: Request):
    run = get_run(request.app.state.db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    findings = list_findings(request.app.state.db, run_id)
    return {"run": run, "findings": findings}


@router.post("/runs/{run_id}/cancel")
def cancel_review_run(run_id: int, request: Request):
    run = get_run(request.app.state.db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    try:
        return request.app.state.jobs.cancel(run_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/runs/{run_id}")
def delete_review_run(run_id: int, request: Request):
    """Remove a run from the DB and delete its on-disk workdir."""
    import shutil

    from lazyreview.db import delete_run

    conn = request.app.state.db
    run = get_run(conn, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run["status"] in ("queued", "running"):
        try:
            request.app.state.jobs.cancel(run_id)
        except ValueError:
            pass

    workdir = Path(run["workdir_path"] or "")
    delete_run(conn, run_id)
    if workdir.is_dir():
        shutil.rmtree(workdir, ignore_errors=True)
    return {"ok": True, "id": run_id, "pr_id": run["pr_id"]}


@router.get("/runs/{run_id}/logs")
def get_run_logs(run_id: int, request: Request):
    """Return current status plus stdout/stderr log contents for live streaming."""
    run = get_run(request.app.state.db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    workdir = Path(run["workdir_path"] or "")
    stdout_path = Path(run["log_stdout_path"] or (workdir / "stdout.log"))
    stderr_path = Path(run["log_stderr_path"] or (workdir / "stderr.log"))

    def _read(path: Path, limit: int = 400_000) -> str:
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) > limit:
            return text[-limit:]
        return text

    return {
        "run_id": run_id,
        "status": run["status"],
        "stdout": _read(stdout_path),
        "stderr": _read(stderr_path),
        "error_message": run.get("error_message"),
    }


@router.get("/runs/{run_id}/report")
def get_report(run_id: int, request: Request):
    run = get_run(request.app.state.db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    path = run.get("report_html_path")
    if path and Path(path).exists():
        return HTMLResponse(Path(path).read_text(encoding="utf-8", errors="replace"))
    return HTMLResponse("<p>No HTML report for this run.</p>", status_code=404)


@router.get("/runs/{run_id}/artifacts/{name}")
def get_artifact(run_id: int, name: str, request: Request):
    run = get_run(request.app.state.db, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    workdir = Path(run["workdir_path"] or "")
    allowed = {
        "prompt.md",
        "cli.json",
        "stdout.log",
        "stderr.log",
        "findings.md",
        "report.html",
        "meta.json",
    }
    if name not in allowed:
        raise HTTPException(status_code=400, detail="Unknown artifact")
    path = workdir / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact not found")
    return FileResponse(path)
