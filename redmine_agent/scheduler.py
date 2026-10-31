"""Built-in daily scheduler (no external dependencies).

Runs the daily job at ``email.send_time`` on ``email.send_days`` in the configured
timezone. The daily job:

1. drafts closure proposals for eligible tickets (``closure.auto_propose_on_schedule``)
   - drafts only; **nothing is closed** until a person approves it;
2. builds the daily overview e-mail and sends it when ``email.enabled`` is true
   (otherwise it is saved to ``<data_dir>/outbox`` as a preview).

Prefer cron / systemd timers / Windows Task Scheduler in production - see
``docs/scheduling.md``. This loop is handy for containers and quick setups.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .config import WEEKDAYS, Settings
from .errors import HarnessError

if TYPE_CHECKING:  # pragma: no cover
    from .harness import Harness

log = logging.getLogger(__name__)


def next_run_at(now_local: datetime, send_time: str, days: list[str]) -> datetime:
    hh, mm = (int(x) for x in send_time.split(":"))
    allowed = {WEEKDAYS.index(d.lower()[:3]) for d in days} or set(range(7))
    candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    for _ in range(8):
        if candidate.weekday() in allowed:
            return candidate
        candidate += timedelta(days=1)
    return candidate


def daily_job(h: "Harness", *, actor: str = "scheduler") -> dict[str, Any]:
    from .mailer import run_daily_overview

    result: dict[str, Any] = {}
    if h.settings.closure.auto_propose_on_schedule and not h.read_only:
        try:
            drafted = h.workflow.auto_draft(created_by=actor)
            result["auto_draft"] = {"drafted": len(drafted["drafted"]), "skipped": len(drafted["skipped"])}
            log.info("Drafted %s closure proposal(s) for review (%s skipped).",
                     len(drafted["drafted"]), len(drafted["skipped"]))
        except HarnessError as exc:
            log.error("Auto-draft failed: %s", exc)
            result["auto_draft_error"] = str(exc)
    email = run_daily_overview(h, send=h.settings.email.enabled, actor=actor)
    result["email"] = {"subject": email["subject"], "sent": email["sent"], "recipients": email["recipients"],
                       "preview": email["preview"]}
    log.info("Daily overview %s: %s", "sent" if email["sent"] else "saved (preview only)", email["subject"])
    return result


def run_scheduler(
    harness_factory: Callable[[], "Harness"],
    settings: Settings,
    *,
    once: bool = False,
    run_now: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    tz = settings.tz
    cfg = settings.email

    def run() -> None:
        h = harness_factory()
        h.data.clear_cache()
        try:
            daily_job(h)
        except Exception:  # keep the scheduler alive; errors are logged
            log.exception("Daily job failed")

    if run_now:
        run()
        if once:
            return
    while True:
        nxt = next_run_at(datetime.now(tz), cfg.send_time, cfg.send_days)
        log.info("Next daily run: %s (%s)", nxt.strftime("%a %d %b %Y %H:%M"), tz.key)
        while True:
            remaining = (nxt - datetime.now(tz)).total_seconds()
            if remaining <= 0:
                break
            if stop_event is not None:
                if stop_event.wait(min(remaining, 60)):
                    return
            else:
                time.sleep(min(remaining, 60))
        run()
        if once:
            return
