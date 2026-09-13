"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lazyreview import __version__
from lazyreview.api import (
    agents,
    analytics,
    consolidations,
    feedback,
    health,
    prs,
    runs,
    settings as settings_api,
)
from lazyreview.config import get_settings
from lazyreview.db.schema import ThreadLocalConnection, connect, init_db
from lazyreview.services.consolidation_jobs import ConsolidationRunner
from lazyreview.services.jobs import JobRunner
from lazyreview.services.analysis_jobs import AnalysisRunner
from lazyreview.services import lifecycle
from lazyreview.services.settings import get_display_timezone
from lazyreview.services.timezones import DEFAULT_DISPLAY_TIMEZONE
from lazyreview.services.pr_workspace import WorkspacePruner
from lazyreview.api import analyses as analyses_api
from lazyreview.services.pr_sync import PrSyncer
from lazyreview.services.retention import RetentionSweeper
from lazyreview.web.formatters import format_cost_usd, format_duration_ms
from lazyreview.agents.names import (
    AgentName,
    ClaudeModelAlias,
    EffortLevel,
    agent_colors_js_dict,
    agents_js_dict,
    claude_models_js_dict,
    efforts_js_dict,
)

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))

_display_timezone_var: ContextVar[str] = ContextVar(
    "display_timezone", default=DEFAULT_DISPLAY_TIMEZONE
)

def _template_display_timezone() -> str:
    return _display_timezone_var.get()

TEMPLATES.env.globals["display_timezone"] = _template_display_timezone
TEMPLATES.env.filters["duration"] = format_duration_ms
TEMPLATES.env.filters["cost"] = format_cost_usd
TEMPLATES.env.globals["AgentName"] = AgentName
TEMPLATES.env.globals["EffortLevel"] = EffortLevel
TEMPLATES.env.globals["ClaudeModelAlias"] = ClaudeModelAlias
TEMPLATES.env.globals["LR_AGENT_JS"] = agents_js_dict()
TEMPLATES.env.globals["LR_EFFORT_JS"] = efforts_js_dict()
TEMPLATES.env.globals["LR_CLAUDE_MODEL_JS"] = claude_models_js_dict()
TEMPLATES.env.globals["LR_AGENT_COLORS_JS"] = agent_colors_js_dict()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Bootstrap schema once, then hand out per-thread connections.
        bootstrap = connect(settings.db_path)
        try:
            init_db(bootstrap)
        finally:
            bootstrap.close()
        conn = ThreadLocalConnection(settings.db_path)
        runner = JobRunner(settings, conn)  # type: ignore[arg-type]
        consolidation_runner = ConsolidationRunner(settings, conn)  # type: ignore[arg-type]
        analysis_runner = AnalysisRunner(settings, conn)  # type: ignore[arg-type]
        workspace_pruner = WorkspacePruner(settings, conn)  # type: ignore[arg-type]
        runner.on_run_finished = consolidation_runner.notify_run_finished
        sweeper = RetentionSweeper(
            settings,
            conn,  # type: ignore[arg-type]
            interval_seconds=settings.retention_sweep_seconds,
        )
        pr_syncer = PrSyncer(conn)  # type: ignore[arg-type]
        app.state.settings = settings
        app.state.db = conn
        app.state.jobs = runner
        app.state.consolidations = consolidation_runner
        app.state.analyses = analysis_runner
        app.state.workspace_pruner = workspace_pruner
        app.state.sweeper = sweeper
        app.state.pr_syncer = pr_syncer
        runner.start()
        consolidation_runner.start()
        lifecycle.bind(
            settings=settings,
            jobs=runner,
            consolidations=consolidation_runner,
            analyses=analysis_runner,
        )
        analysis_runner.start()
        workspace_pruner.start()
        sweeper.start()
        pr_syncer.start()
        logger.info(
            "lazyreview %s config=%s url=%s data_dir=%s",
            __version__,
            settings.config_path,
            settings.base_url,
            settings.data_dir,
        )
        yield
        pr_syncer.stop()
        sweeper.stop()
        workspace_pruner.stop()
        lifecycle.unbind()
        analysis_runner.stop()
        consolidation_runner.stop()
        runner.stop()
        conn.close()

    app = FastAPI(title="lazyreview", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def display_timezone_middleware(request: Request, call_next):
        tz = DEFAULT_DISPLAY_TIMEZONE
        try:
            db = getattr(request.app.state, "db", None)
            if db is not None:
                tz = get_display_timezone(db)
        except Exception:
            logger.exception("failed to resolve display_timezone")
        token = _display_timezone_var.set(tz)
        try:
            return await call_next(request)
        finally:
            _display_timezone_var.reset(token)


    static_dir = WEB_DIR / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    (static_dir / "css").mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(health.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.include_router(prs.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(feedback.router, prefix="/api")
    app.include_router(consolidations.router, prefix="/api")
    app.include_router(analytics.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(analyses_api.router, prefix="/api")

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        from lazyreview.db import get_setting, list_prs
        from lazyreview.services.gh import GhError, gh_user_login

        prs_list = list_prs(request.app.state.db)
        last_sync = get_setting(request.app.state.db, "pr_sync_last_at")
        gh_login = None
        try:
            gh_login = gh_user_login()
        except GhError:
            pass
        return TEMPLATES.TemplateResponse(
            request,
            "home.html",
            {
                "prs": prs_list,
                "last_sync": last_sync,
                "gh_login": gh_login,
                "version": __version__,
            },
        )

    @app.get("/prs/{pr_id}", response_class=HTMLResponse)
    async def pr_detail(request: Request, pr_id: int):
        from lazyreview.db import (
            active_consolidation_run_ids,
            get_pr,
            in_flight_consolidation_run_ids,
            list_consolidations_for_pr,
            list_feedback_actions,
            list_runs_for_pr,
            sync_run_deprecation_for_pr,
            update_run,
        )
        from lazyreview.services.cost import resolve_run_cost_usd

        pr = get_pr(request.app.state.db, pr_id)
        if not pr:
            return HTMLResponse("PR not found", status_code=404)
        sync_run_deprecation_for_pr(request.app.state.db, pr_id)
        locked_ids = active_consolidation_run_ids(request.app.state.db, pr_id)
        consolidating_ids = in_flight_consolidation_run_ids(request.app.state.db, pr_id)
        runs_list = list_runs_for_pr(request.app.state.db, pr_id)
        enriched: list[dict] = []
        total_cost = 0.0
        have_cost = False
        for run in runs_list:
            row = dict(run)
            cost = resolve_run_cost_usd(row)
            row["cost_usd"] = cost
            rid = int(row["id"])
            row["consolidating"] = rid in consolidating_ids
            if cost is not None:
                have_cost = True
                total_cost += cost
                # Persist backfill when we discovered a cost for an older run.
                if run.get("cost_usd") is None:
                    try:
                        update_run(request.app.state.db, rid, cost_usd=cost)
                    except Exception:
                        logger.exception("failed to backfill cost_usd for run %s", rid)
            enriched.append(row)
        feedback = list_feedback_actions(request.app.state.db, pr_id)
        consolidations_list = list_consolidations_for_pr(request.app.state.db, pr_id)
        from lazyreview.db import list_pr_analyses, list_pr_events

        pr_events = list_pr_events(request.app.state.db, pr_id)
        analyses_list = list_pr_analyses(request.app.state.db, pr_id)
        head = (pr.get("head_sha") or "").strip()
        runs_stale = bool(enriched) and bool(head) and not any(
            (r.get("head_sha") or "").strip() == head for r in enriched
        )
        return TEMPLATES.TemplateResponse(
            request,
            "pr_detail.html",
            {
                "pr": pr,
                "runs": enriched,
                "runs_stale": runs_stale,
                "consolidating_ids": sorted(consolidating_ids),
                "locked_run_ids": sorted(locked_ids),
                "consolidations": consolidations_list,
                "analyses": analyses_list,
                "pr_events": pr_events,
                "feedback": feedback,
                "total_cost_usd": total_cost if have_cost else None,
                "version": __version__,
            },
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: int):
        from lazyreview.db import get_pr, get_run, list_findings, update_run
        from lazyreview.services.cost import resolve_run_cost_usd

        run = get_run(request.app.state.db, run_id)
        if not run:
            return HTMLResponse("Run not found", status_code=404)
        stored_cost = run.get("cost_usd")
        run = dict(run)
        cost = resolve_run_cost_usd(run)
        run["cost_usd"] = cost
        if cost is not None and stored_cost is None:
            try:
                update_run(request.app.state.db, run_id, cost_usd=cost)
            except Exception:
                logger.exception("failed to backfill cost_usd for run %s", run_id)
        pr = get_pr(request.app.state.db, int(run["pr_id"]))
        findings = list_findings(request.app.state.db, run_id)
        workdir = Path(run["workdir_path"] or "")
        artifact_names = [
            "prompt.md",
            "cli.json",
            "stdout.log",
            "stderr.log",
            "findings.md",
            "report.html",
            "meta.json",
        ]
        artifacts = [name for name in artifact_names if (workdir / name).is_file()]

        def _read_log(name: str) -> str:
            path = workdir / name
            if not path.is_file():
                return ""
            text = path.read_text(encoding="utf-8", errors="replace")
            return text[-400_000:] if len(text) > 400_000 else text

        return TEMPLATES.TemplateResponse(
            request,
            "run_detail.html",
            {
                "run": run,
                "pr": pr,
                "findings": findings,
                "artifacts": artifacts,
                "initial_stdout": _read_log("stdout.log"),
                "initial_stderr": _read_log("stderr.log"),
                "version": __version__,
            },
        )

    @app.get("/consolidations/{consolidation_id}", response_class=HTMLResponse)
    async def consolidation_detail(request: Request, consolidation_id: int):
        from lazyreview.db import get_consolidation, get_pr, get_run

        consolidation = get_consolidation(request.app.state.db, consolidation_id)
        if not consolidation:
            return HTMLResponse("Consolidation not found", status_code=404)
        pr = get_pr(request.app.state.db, int(consolidation["pr_id"]))
        source_runs = []
        for rid in consolidation.get("source_run_ids") or []:
            run = get_run(request.app.state.db, int(rid))
            if run:
                source_runs.append(run)
            else:
                source_runs.append(
                    {
                        "id": int(rid),
                        "status": "",
                        "agent": "",
                        "model": "",
                        "effort": None,
                        "findings_sev1": 0,
                        "findings_sev2": 0,
                        "findings_sev3": 0,
                        "total_tokens": None,
                        "cost_usd": None,
                        "duration_ms": None,
                    }
                )
        return TEMPLATES.TemplateResponse(
            request,
            "consolidation_detail.html",
            {
                "consolidation": consolidation,
                "pr": pr,
                "source_runs": source_runs,
                "version": __version__,
            },
        )

    @app.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "analytics.html",
            {"version": __version__},
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        from lazyreview.services.settings import get_settings_view

        view = get_settings_view(request.app.state.db)
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            {"settings": view, "version": __version__},
        )

    @app.get("/why", response_class=HTMLResponse)
    async def why_page(request: Request):
        return TEMPLATES.TemplateResponse(
            request,
            "why.html",
            {"version": __version__},
        )

    return app


app = create_app()
