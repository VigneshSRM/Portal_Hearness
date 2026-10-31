from datetime import datetime
from email import message_from_bytes
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from redmine_agent import mailer
from redmine_agent.errors import ConfigError
from redmine_agent.scheduler import daily_job, next_run_at

IST = ZoneInfo("Asia/Kolkata")


def test_daily_context_and_rendering(harness):
    ctx = mailer.build_daily_context(harness)
    subject, html, text = mailer.render_daily(ctx, "[Redmine]")
    k = ctx["kpis"]
    assert subject.startswith("[Redmine] Daily overview") and f"{k['open']} open" in subject
    for section in ("SLA breached", "At risk", "New tickets", "Closed tickets", "Assigned to me", "Team workload"):
        assert section in html
    assert "SLA BREACHED" in text and "ASSIGNED TO ME" in text
    assert "<script" not in html


def test_preview_writes_outbox_with_csv_attachment(harness):
    result = mailer.run_daily_overview(harness, send=False)
    assert result["sent"] is False
    eml = Path(result["preview"]["eml"])
    assert eml.exists() and Path(result["preview"]["html"]).exists()
    msg = message_from_bytes(eml.read_bytes())
    attachments = [p.get_filename() for p in msg.walk() if p.get_filename()]
    assert any(name.endswith(".csv") for name in attachments)


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        pass

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_send_uses_smtp_and_audits(harness, monkeypatch):
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    cfg = harness.settings.email
    cfg.smtp_host, cfg.username, cfg.password, cfg.sender = "smtp.test", "bot", "pw", "bot@test"
    FakeSMTP.sent.clear()
    result = mailer.run_daily_overview(harness, send=True, recipients=["lead@test"], actor="Karthik")
    assert result["sent"] and FakeSMTP.sent and FakeSMTP.sent[0]["To"] == "lead@test"
    assert any(a["action"] == "email.daily_overview.sent" for a in harness.store.list_audit())


def test_send_without_recipients_fails(harness):
    harness.settings.email.smtp_host = "smtp.test"
    with pytest.raises(ConfigError, match="recipients"):
        mailer.run_daily_overview(harness, send=True)


def test_next_run_skips_weekends():
    friday_after = datetime(2026, 10, 30, 10, 0, tzinfo=IST)
    assert next_run_at(friday_after, "09:00", ["mon", "tue", "wed", "thu", "fri"]) == datetime(2026, 11, 2, 9, 0, tzinfo=IST)
    monday_before = datetime(2026, 11, 2, 8, 0, tzinfo=IST)
    assert next_run_at(monday_before, "09:00", ["mon"]) == datetime(2026, 11, 2, 9, 0, tzinfo=IST)


def test_daily_job_drafts_but_never_closes(harness, app_state):
    result = daily_job(harness)
    assert result["auto_draft"]["drafted"] >= 1
    assert result["email"]["sent"] is False  # email.enabled defaults to false -> preview only
    assert not any(m == "PUT" for m, _ in app_state.request_log)
    assert harness.store.count_proposals("pending") == result["auto_draft"]["drafted"]
