"""FastAPI dashboard: overview, SLA, team, performance, time spent, my tickets,
custom CSV reports, closure templates, the approval queue and the audit log.

Security
--------
* Optional HTTP Basic auth (``dashboard.username`` / ``dashboard.password``); when set,
  the logged-in user is the reviewer recorded on approvals.
* State-changing API calls require the ``X-Harness-Request: 1`` header (sent by the
  dashboard's JavaScript). Browsers cannot add custom headers cross-site without a
  CORS preflight, which this app never grants - this blocks CSRF.
* Approving is always an explicit click by a named person; the server re-checks every
  rule (human reviewer, digest, staleness) before anything is written to Redmine.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, analytics
from ..approvals import describe_changes
from ..closure_templates import ClosureTemplate, render_note, validate_template
from ..config import Settings, load_settings
from ..errors import (
    ConfigError,
    HarnessError,
    NotFoundError,
    PermissionDenied,
    RedmineConnectionError,
    RedmineError,
    StaleProposalError,
    TemplateError,
    WorkflowError,
)
from ..guard import is_human_actor
from ..reports import ReportSpec, available_columns, report_filename, run_report
from ..timeutils import fmt_dt, fmt_hours, utcnow

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

NAV = [
    ("/", "overview", "Overview"),
    ("/sla", "sla", "SLA"),
    ("/team", "team", "Team"),
    ("/performance", "performance", "Performance"),
    ("/time", "time", "Time spent"),
    ("/my-tickets", "my", "My tickets"),
    ("/reports", "reports", "Reports & CSV"),
    ("/templates", "templates", "Closure templates"),
    ("/approvals", "approvals", "Approvals"),
    ("/audit", "audit", "Audit log"),
]


def _pct(value: Any) -> str:
    return "-" if value is None else f"{value:g}%"


def create_app(settings: Settings | None = None, *, harness: Any = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="Redmine Agent Dashboard", version=__version__, docs_url="/api/docs", redoc_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["hours"] = fmt_hours
    templates.env.filters["pct"] = _pct
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    state: dict[str, Any] = {"harness": harness, "error": None}
    lock = threading.Lock()

    def get_harness():
        if state["harness"] is None:
            with lock:
                if state["harness"] is None:
                    from ..harness import Harness

                    state["harness"] = Harness.create(settings)
        return state["harness"]

    # ------------------------------------------------------------------ middleware
    @app.middleware("http")
    async def security(request: Request, call_next):
        cfg = settings.dashboard
        user = ""
        if cfg.username and request.url.path not in ("/healthz",):
            auth = request.headers.get("authorization", "")
            ok = False
            if auth.lower().startswith("basic "):
                try:
                    login, _, password = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                    ok = secrets.compare_digest(login, cfg.username) and secrets.compare_digest(password, cfg.password)
                    user = login
                except (binascii.Error, UnicodeDecodeError):
                    ok = False
            if not ok:
                return PlainTextResponse("Authentication required", status_code=401,
                                         headers={"WWW-Authenticate": 'Basic realm="Redmine Agent"'})
        if request.method in WRITE_METHODS and request.headers.get("x-harness-request") != "1":
            return JSONResponse({"error": "Missing X-Harness-Request header (CSRF protection)."}, status_code=403)
        request.state.user = user
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    def reviewer(request: Request) -> str:
        name = getattr(request.state, "user", "") or request.headers.get("x-harness-reviewer", "").strip()
        if not name:
            raise PermissionDenied("Enter your name as reviewer (top of the Approvals page) before approving.")
        if not is_human_actor(name):
            raise PermissionDenied(f"'{name}' is not accepted as a human reviewer name.")
        return name[:80]

    # ------------------------------------------------------------------ errors
    def status_for(exc: Exception) -> int:
        if isinstance(exc, PermissionDenied):
            return 403
        if isinstance(exc, StaleProposalError):
            return 409
        if isinstance(exc, (WorkflowError, TemplateError)):
            return 422
        if isinstance(exc, NotFoundError):
            return 404
        if isinstance(exc, RedmineConnectionError):
            return 502
        if isinstance(exc, RedmineError) and getattr(exc, "status", None):
            return 502 if exc.status >= 500 else 400
        return 400

    @app.exception_handler(HarnessError)
    async def harness_error(request: Request, exc: HarnessError):
        path = request.url.path
        if path.startswith(("/api/", "/export/")):
            body: dict[str, Any] = {"error": str(exc)}
            if isinstance(exc, RedmineError) and exc.errors:
                body["details"] = exc.errors
            return JSONResponse(body, status_code=status_for(exc))
        setup = isinstance(exc, (ConfigError, RedmineConnectionError)) or "credentials" in str(exc).lower() \
            or getattr(exc, "status", None) == 401
        return templates.TemplateResponse(request, "error.html", {
            **base_context(request, "", safe=True), "error": str(exc), "setup": setup,
            "details": getattr(exc, "errors", []),
        }, status_code=status_for(exc) if status_for(exc) != 400 else 500)

    # ------------------------------------------------------------------ context helpers
    def filters(request: Request) -> dict[str, Any]:
        q = request.query_params
        days = q.get("days") or str(settings.dashboard.default_days)
        return {"days": int(days) if days.isdigit() else settings.dashboard.default_days,
                "from": q.get("from") or "", "to": q.get("to") or "", "project": q.get("project") or ""}

    def base_context(request: Request, active: str, *, safe: bool = False) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "nav": NAV, "active": active, "filters": filters(request), "version": __version__,
            "settings": settings, "read_only": settings.redmine.read_only, "projects": [], "pending_count": 0,
            "me": {}, "redmine_url": settings.redmine.public_url or settings.redmine.url, "auth_user": getattr(request.state, "user", ""),
        }
        try:
            h = get_harness()
            ctx["read_only"] = h.read_only
            ctx["pending_count"] = h.store.count_proposals("pending")
            ctx["projects"] = h.data.projects()
            ctx["me"] = h.data.current_user()
        except HarnessError:
            if not safe:
                raise
        return ctx

    def snapshot(request: Request):
        f = filters(request)
        h = get_harness()
        window = h.data.window(f["days"], f["from"] or None, f["to"] or None)
        return h.data.snapshot(window, f["project"] or None)

    def page(request: Request, name: str, active: str, **extra: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {**base_context(request, active), **extra})

    # ------------------------------------------------------------------ pages
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def overview_page(request: Request):
        return page(request, "overview.html", "overview", data=analytics.overview(snapshot(request)))

    @app.get("/sla", response_class=HTMLResponse)
    def sla_page(request: Request):
        return page(request, "sla.html", "sla", data=analytics.sla_report(snapshot(request)))

    @app.get("/team", response_class=HTMLResponse)
    def team_page(request: Request):
        return page(request, "team.html", "team", data=analytics.team_report(snapshot(request)))

    @app.get("/performance", response_class=HTMLResponse)
    def performance_page(request: Request):
        return page(request, "performance.html", "performance", data=analytics.performance_report(snapshot(request)))

    @app.get("/time", response_class=HTMLResponse)
    def time_page(request: Request):
        return page(request, "time.html", "time", data=analytics.time_report(snapshot(request)))

    def my_data(request: Request) -> dict[str, Any]:
        h = get_harness()
        status = request.query_params.get("status", "open")
        project = request.query_params.get("project") or None
        return analytics.my_tickets(h.data.my_issues(status, project), h.data.sla, tz=settings.tz,
                                    base_url=h.client.web_url, now=utcnow())

    @app.get("/my-tickets", response_class=HTMLResponse)
    def my_page(request: Request):
        h = get_harness()
        return page(request, "my_tickets.html", "my", data=my_data(request),
                    status=request.query_params.get("status", "open"),
                    templates_list=[t.to_dict() for t in h.templates.list()],
                    busy={p["issue_id"]: p["id"] for p in h.workflow.list(["pending", "approved"])})

    @app.get("/reports", response_class=HTMLResponse)
    def reports_page(request: Request):
        return page(request, "reports.html", "reports", presets=settings.report_presets,
                    columns={ds: available_columns(ds) for ds in ("issues", "sla", "time_entries", "team")})

    @app.get("/templates", response_class=HTMLResponse)
    def templates_page(request: Request):
        h = get_harness()
        return page(request, "templates.html", "templates", templates_list=[t.to_dict() for t in h.templates.list()],
                    statuses=h.data.statuses(), trackers=h.data.trackers(), template_file=str(h.templates.path))

    @app.get("/approvals", response_class=HTMLResponse)
    def approvals_page(request: Request):
        h = get_harness()
        groups = {}
        for state_name in ("pending", "approved"):
            groups[state_name] = [dict(p, changes_view=describe_changes(p)) for p in h.workflow.list(state_name)]
        history = [dict(p, changes_view=describe_changes(p)) for p in
                   h.workflow.list(["executed", "rejected", "failed", "stale", "cancelled"])[:50]]
        return page(request, "approvals.html", "approvals", groups=groups, history=history,
                    templates_list=[t.to_dict() for t in h.templates.list()], statuses=h.data.statuses(),
                    closure=settings.closure, pin_required=h.workflow.pin_required)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request):
        return page(request, "audit.html", "audit", rows=get_harness().store.list_audit(300))

    @app.get("/email-preview", response_class=HTMLResponse)
    def email_preview(request: Request):
        from ..mailer import build_daily_context, render_daily

        _, html, _ = render_daily(build_daily_context(get_harness()), settings.email.subject_prefix)
        return HTMLResponse(html)

    # ------------------------------------------------------------------ JSON API: analytics
    @app.get("/api/overview")
    def api_overview(request: Request):
        return analytics.overview(snapshot(request))

    @app.get("/api/sla")
    def api_sla(request: Request):
        return analytics.sla_report(snapshot(request))

    @app.get("/api/team")
    def api_team(request: Request):
        return analytics.team_report(snapshot(request))

    @app.get("/api/performance")
    def api_performance(request: Request):
        return analytics.performance_report(snapshot(request))

    @app.get("/api/time")
    def api_time(request: Request):
        return analytics.time_report(snapshot(request))

    @app.get("/api/my-tickets")
    def api_my(request: Request):
        return my_data(request)

    @app.post("/api/cache/clear")
    def api_clear_cache():
        get_harness().data.clear_cache()
        return {"ok": True}

    # ------------------------------------------------------------------ reports & CSV
    def spec_from(data: dict[str, Any]) -> ReportSpec:
        base: dict[str, Any] = {}
        preset = data.get("preset")
        if preset:
            if preset not in settings.report_presets:
                raise HarnessError(f"Unknown preset '{preset}'.")
            base = dict(settings.report_presets[preset])
        base.update({k: v for k, v in data.items() if v not in (None, "", []) and k != "preset"})
        return ReportSpec.from_dict(base)

    @app.get("/api/reports/columns")
    def api_columns(dataset: str = "issues"):
        return available_columns(dataset)

    @app.post("/api/reports/preview")
    async def api_report_preview(request: Request):
        result = run_report(spec_from(await request.json()), get_harness().data)
        return result.preview(limit=300)

    def csv_response(result) -> Response:
        filename = report_filename(result)
        return Response(result.to_csv_bytes(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/export/report.csv")
    def export_report(request: Request):
        params: dict[str, Any] = dict(request.query_params)
        for key in ("columns", "group_by"):
            values = request.query_params.getlist(key)
            if values:
                params[key] = ",".join(values)
        h = get_harness()
        result = run_report(spec_from(params), h.data)
        h.store.audit(actor=getattr(request.state, "user", "") or "dashboard", action="report.exported",
                      target=result.title, details={"rows": len(result.rows)})
        return csv_response(result)

    @app.get("/export/{kind}.csv")
    def export_kind(kind: str, request: Request):
        f = filters(request)
        presets = {
            "my-tickets": {"dataset": "issues", "date_field": "open_now", "status": "open",
                           "assignee": str(get_harness().data.current_user().get("id")), "title": "My open tickets"},
            "sla": {"dataset": "sla", "date_field": "closed_on", "status": "closed", "title": "SLA report"},
            "sla-open": {"dataset": "sla", "date_field": "open_now", "status": "open", "title": "Open tickets SLA"},
            "team": {"dataset": "team", "title": "Team report"},
            "time": {"dataset": "time_entries", "title": "Time entries"},
            "issues": {"dataset": "issues", "title": "Tickets created"},
        }
        if kind not in presets:
            raise NotFoundError(f"Unknown export '{kind}'.")
        spec = {**presets[kind], "days": f["days"], "date_from": f["from"], "date_to": f["to"],
                "project": f["project"]}
        return csv_response(run_report(spec_from(spec), get_harness().data))

    # ------------------------------------------------------------------ templates
    @app.get("/api/templates")
    def api_templates():
        return [t.to_dict() for t in get_harness().templates.list()]

    @app.post("/api/templates")
    async def api_template_create(request: Request):
        actor = reviewer(request)
        h = get_harness()
        tpl = h.templates.create(await request.json(), actor=actor)
        h.store.audit(actor=actor, action="template.created", target=tpl.id)
        return tpl.to_dict()

    @app.put("/api/templates/{template_id}")
    async def api_template_update(template_id: str, request: Request):
        actor = reviewer(request)
        h = get_harness()
        tpl = h.templates.update(template_id, await request.json(), actor=actor)
        h.store.audit(actor=actor, action="template.updated", target=tpl.id)
        return tpl.to_dict()

    @app.delete("/api/templates/{template_id}")
    def api_template_delete(template_id: str, request: Request):
        actor = reviewer(request)
        h = get_harness()
        h.templates.delete(template_id, actor=actor)
        h.store.audit(actor=actor, action="template.deleted", target=template_id)
        return {"deleted": template_id}

    @app.post("/api/templates/preview")
    async def api_template_preview(request: Request):
        body = await request.json()
        h = get_harness()
        tpl = (ClosureTemplate.from_dict(body["template"]) if body.get("template")
               else h.templates.get(body.get("template_id", "")))
        problems = validate_template(tpl) if body.get("template") else []
        issue_id = body.get("issue_id")
        if not issue_id:
            mine = h.data.my_issues("open")
            if not mine:
                raise TemplateError("Enter a ticket number to preview against.")
            issue_id = mine[0].id
        issue = h.data.issue(int(issue_id), include=())
        note = render_note(tpl, issue=issue, me=h.data.current_user(), variables=body.get("variables") or {},
                           base_url=h.client.web_url, tz=settings.tz)
        return {"note": note, "issue_id": issue.id, "issue_subject": issue.subject, "problems": problems}

    # ------------------------------------------------------------------ proposals
    def proposal_view(p: dict[str, Any]) -> dict[str, Any]:
        return dict(p, changes_view=describe_changes(p))

    @app.get("/api/candidates")
    def api_candidates(scope: str | None = None):
        h = get_harness()
        return [c.to_dict(h.client.web_url, settings.tz) for c in h.workflow.candidates(scope)]

    @app.get("/api/proposals")
    def api_proposals(status: str | None = None):
        return [proposal_view(p) for p in get_harness().workflow.list(status if status != "all" else None)]

    @app.post("/api/proposals")
    async def api_draft(request: Request):
        body = await request.json()
        actor = getattr(request.state, "user", "") or request.headers.get("x-harness-reviewer", "").strip() or "dashboard"
        p = get_harness().workflow.draft(
            int(body["issue_id"]), created_by=actor, template_id=body.get("template_id") or None,
            variables=body.get("variables") or {}, note=body.get("note") or None,
            target_status=body.get("target_status") or None, reason="drafted in dashboard")
        return proposal_view(p)

    @app.post("/api/proposals/auto-draft")
    def api_auto_draft(request: Request):
        actor = getattr(request.state, "user", "") or request.headers.get("x-harness-reviewer", "").strip() or "dashboard"
        result = get_harness().workflow.auto_draft(created_by=actor)
        return {"drafted": len(result["drafted"]), "skipped": result["skipped"]}

    @app.put("/api/proposals/{proposal_id}")
    async def api_update(proposal_id: str, request: Request):
        body = await request.json()
        actor = reviewer(request)
        kwargs: dict[str, Any] = {}
        if "note" in body:
            kwargs["note"] = body["note"]
        if body.get("target_status"):
            kwargs["target_status"] = str(body["target_status"])
        if "done_ratio" in body:
            kwargs["done_ratio"] = None if body["done_ratio"] in (None, "") else int(body["done_ratio"])
        return proposal_view(get_harness().workflow.update_draft(proposal_id, actor=actor, **kwargs))

    @app.post("/api/proposals/{proposal_id}/approve")
    async def api_approve(proposal_id: str, request: Request):
        body = await request.json() if await request.body() else {}
        actor = reviewer(request)
        pin = body.get("pin") or request.headers.get("x-harness-pin") or None
        wf = get_harness().workflow
        if body.get("execute", True):
            p = wf.approve_and_execute(proposal_id, reviewer=actor, comment=body.get("comment", ""), pin=pin)
        else:
            p = wf.approve(proposal_id, reviewer=actor, comment=body.get("comment", ""), pin=pin)
        return proposal_view(p)

    @app.post("/api/proposals/{proposal_id}/execute")
    def api_execute(proposal_id: str, request: Request):
        actor = reviewer(request)
        return proposal_view(get_harness().workflow.execute(proposal_id, actor=actor))

    @app.post("/api/proposals/{proposal_id}/reject")
    async def api_reject(proposal_id: str, request: Request):
        body = await request.json() if await request.body() else {}
        actor = reviewer(request)
        return proposal_view(get_harness().workflow.reject(proposal_id, reviewer=actor, comment=body.get("comment", "")))

    @app.post("/api/proposals/{proposal_id}/cancel")
    def api_cancel(proposal_id: str, request: Request):
        actor = getattr(request.state, "user", "") or request.headers.get("x-harness-reviewer", "").strip() or "dashboard"
        return proposal_view(get_harness().workflow.cancel(proposal_id, actor=actor))

    # ------------------------------------------------------------------ e-mail
    @app.post("/api/daily/send")
    async def api_daily_send(request: Request):
        from ..mailer import run_daily_overview

        actor = reviewer(request)
        body = await request.json() if await request.body() else {}
        recipients = [r.strip() for r in str(body.get("recipients", "")).split(",") if r.strip()] or None
        result = run_daily_overview(get_harness(), send=True, recipients=recipients, actor=actor)
        return {"sent": True, "subject": result["subject"], "recipients": result["recipients"]}

    @app.get("/api/status")
    def api_status():
        h = get_harness()
        me = h.data.current_user()
        return {"version": __version__, "redmine_url": h.client.web_url, "user": me.get("login"),
                "read_only": h.read_only, "pin_required": h.workflow.pin_required,
                "pending": h.store.count_proposals("pending"),
                "approved": h.store.count_proposals("approved"), "time": fmt_dt(utcnow(), settings.tz)}

    return app
