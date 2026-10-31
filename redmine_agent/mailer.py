"""Daily overview e-mail: build, render (HTML + plain text), preview and send via SMTP.

Sending the scheduled daily overview is the one *outbound* action the harness takes
on its own, and only when ``email.enabled: true`` and recipients are configured -
that configuration is the standing permission. A copy of every message is saved to
``<data_dir>/outbox`` for audit. Nothing in Redmine is modified.
"""

from __future__ import annotations

import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import analytics
from .config import EmailConfig
from .errors import ConfigError, HarnessError, RedmineError
from .reports import ReportSpec, run_report
from .timeutils import Window, fmt_dt, utcnow

if TYPE_CHECKING:  # pragma: no cover
    from .harness import Harness

TEMPLATE_DIR = Path(__file__).parent / "email_templates"
_env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html"]),
                   trim_blocks=True, lstrip_blocks=True)


def build_daily_context(h: "Harness", now=None) -> dict[str, Any]:
    s = h.settings
    cfg = s.email
    tz = s.tz
    now = now or utcnow()
    window = Window.last_days(cfg.sla_window_days, tz)
    snap = h.data.snapshot(window)
    ov = analytics.overview(snap)
    sla = analytics.sla_report(snap)
    team = analytics.team_report(snap)

    since = now - timedelta(hours=cfg.lookback_hours)
    new = sorted((i for i in snap.issues if i.created_on and i.created_on >= since), key=lambda i: -i.id)
    closed = sorted((i for i in snap.issues if i.is_closed and i.closed_on and i.closed_on >= since),
                    key=lambda i: -i.id)
    yesterday = (now.astimezone(tz) - timedelta(days=1)).date()
    hours_yesterday = round(sum(e.hours for e in snap.time_entries if e.spent_on == yesterday), 2)

    try:
        mine = analytics.my_tickets(h.data.my_issues("open"), h.data.sla, tz=tz, base_url=h.client.web_url, now=now)
    except RedmineError:
        mine = {"rows": [], "kpis": {}}
    me = snap.me or {}
    max_rows = cfg.max_rows
    dashboard_url = (cfg.dashboard_url or s.dashboard.public_url or "").rstrip("/")
    return {
        "title": f"Redmine daily overview - {now.astimezone(tz).strftime('%A, %d %b %Y')}",
        "date_label": now.astimezone(tz).strftime("%A, %d %b %Y"),
        "generated_at": fmt_dt(now, tz),
        "timezone": tz.key,
        "project": snap.project_label,
        "lookback_hours": cfg.lookback_hours,
        "window_label": window.label,
        "window_days": cfg.sla_window_days,
        "kpis": {
            "open": ov["kpis"]["open"],
            "new": len(new),
            "closed": len(closed),
            "breached": ov["kpis"]["sla_breached_open"],
            "at_risk": ov["kpis"]["sla_at_risk_open"],
            "overdue": ov["kpis"]["overdue"],
            "unassigned": ov["kpis"]["unassigned"],
            "hours_yesterday": hours_yesterday,
            "sla_compliance": sla["kpis"]["resolution_compliance"],
            "response_compliance": sla["kpis"]["response_compliance"],
            "avg_resolution": ov["kpis"]["avg_resolution"],
            "hours_window": ov["kpis"]["hours_spent"],
        },
        "new_tickets": analytics.rows_for(new, snap, limit=max_rows),
        "closed_tickets": analytics.rows_for(closed, snap, limit=max_rows),
        "breached": ov["breached"][:max_rows],
        "at_risk": ov["at_risk"][:max_rows],
        "overdue": ov["overdue"][:max_rows],
        "team": team["members"][:12],
        "my_tickets": mine["rows"][:max_rows],
        "my_kpis": mine.get("kpis", {}),
        "me_name": me.get("firstname", "") + (" " + me.get("lastname", "") if me.get("lastname") else ""),
        "pending_approvals": h.store.count_proposals("pending"),
        "approved_waiting": h.store.count_proposals("approved"),
        "dashboard_url": dashboard_url,
        "read_only": h.read_only,
    }


def render_daily(context: dict[str, Any], subject_prefix: str = "[Redmine]") -> tuple[str, str, str]:
    k = context["kpis"]
    subject = (f"{subject_prefix} Daily overview {context['date_label']}: {k['open']} open, {k['new']} new, "
               f"{k['closed']} closed, {k['breached']} SLA breached").strip()
    html = _env.get_template("daily_overview.html").render(**context)
    text = _env.get_template("daily_overview.txt").render(**context)
    return subject, html, text


def build_message(
    cfg: EmailConfig,
    subject: str,
    html: str,
    text: str,
    to: list[str],
    cc: list[str] | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender or cfg.username or "redmine-agent@localhost"
    msg["To"] = ", ".join(to) if to else "undisclosed-recipients:;"
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="redmine-agent.local")
    msg["X-Generated-By"] = "Portal Harness Redmine Agent"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for filename, content, mimetype in attachments or []:
        maintype, subtype = mimetype.split("/", 1)
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def send_message(cfg: EmailConfig, msg: EmailMessage) -> None:
    if not cfg.smtp_host:
        raise ConfigError("SMTP is not configured (email.smtp_host / SMTP_HOST).")
    context = ssl.create_default_context()
    try:
        if cfg.security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout, context=context)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout)
        with server:
            server.ehlo()
            if cfg.security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if cfg.username:
                server.login(cfg.username, cfg.password)
            server.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        raise HarnessError(f"Failed to send e-mail via {cfg.smtp_host}:{cfg.smtp_port}: {exc}") from exc


def save_preview(outbox: Path, msg: EmailMessage, html: str) -> dict[str, str]:
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    html_path = outbox / f"daily-overview-{stamp}.html"
    eml_path = outbox / f"daily-overview-{stamp}.eml"
    html_path.write_text(html, encoding="utf-8")
    eml_path.write_bytes(bytes(msg))
    return {"html": str(html_path), "eml": str(eml_path)}


def run_daily_overview(
    h: "Harness",
    *,
    send: bool,
    recipients: list[str] | None = None,
    actor: str = "scheduler",
    now=None,
) -> dict[str, Any]:
    cfg = h.settings.email
    context = build_daily_context(h, now)
    subject, html, text = render_daily(context, cfg.subject_prefix)
    attachments: list[tuple[str, bytes, str]] = []
    if cfg.attach_csv:
        try:
            report = run_report(ReportSpec(dataset="sla", date_field="open_now", status="open",
                                           title="Open tickets with SLA"), h.data)
            attachments.append((f"open-tickets-{utcnow().strftime('%Y%m%d')}.csv", report.to_csv_bytes(),
                                "text/csv"))
        except (HarnessError, RedmineError):
            pass
    to = [r for r in (recipients or cfg.recipients) if r]
    msg = build_message(cfg, subject, html, text, to, cfg.cc, attachments)
    paths = save_preview(h.settings.data_path / "outbox", msg, html)
    if send:
        if not to:
            raise ConfigError("No recipients configured (email.recipients / DAILY_RECIPIENTS).")
        send_message(cfg, msg)
        h.store.audit(actor=actor, action="email.daily_overview.sent", target=", ".join(to),
                      details={"subject": subject})
    return {"subject": subject, "recipients": to, "sent": send, "preview": paths, "text": text,
            "kpis": context["kpis"]}
