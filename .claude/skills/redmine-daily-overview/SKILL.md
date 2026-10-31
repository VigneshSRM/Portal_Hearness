---
name: redmine-daily-overview
description: Prepare, preview, send and schedule the daily Redmine overview e-mail - KPIs, SLA breaches and at-risk tickets, new and closed tickets of the last 24 hours, overdue items, the user's own tickets, team workload and pending closure approvals, with an open-tickets CSV attached. Use when the user asks for today's overview, a daily summary, digest or status mail, wants to send the overview to someone, or wants to set up or change the daily e-mail schedule.
license: MIT
compatibility: Needs the Portal Harness repository (python harness.py) with Redmine configured; sending needs SMTP settings in .env.
metadata:
  author: portal-harness
  version: "1.1"
---

# Daily overview e-mail

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py daily-overview --print --json
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher picks the project's
  virtualenv by itself.
- Exit codes: `0` ok, `2` error (e.g. SMTP not configured), `4` not installed. For configuration problems,
  switch to the `redmine-setup` skill.

## Preview (safe - sends nothing)

`python harness.py daily-overview --print --json` builds the e-mail and saves a copy in `data/outbox/`.
The output contains:
- `subject`
- `kpis`: open, new, closed, breached, at risk, overdue, unassigned, hours yesterday, SLA compliance
- `preview.html`: the saved HTML file
- `text`: the plain-text body

To answer "give me today's overview", summarise the numbers and the breached or at-risk tickets from
`text`. If the dashboard is running, point the user to `preview.html` or to its `/email-preview` page
to see the formatted mail.

## Send (only when the user explicitly asks)

1. Preview first, and tell the user the subject, the recipients and the key numbers.
2. Ask: "Send it to <recipients> now?" Continue only after a clear yes in this conversation.
3. Send to the configured recipients (`DAILY_RECIPIENTS`):
   `python harness.py daily-overview --send --json`
   Or to addresses the user gave you:
   `python harness.py daily-overview --send --to a@example.com,b@example.com --json`
4. Report `sent` and `recipients`. On an SMTP error, check the `SMTP_*` settings with the user
   (`redmine-setup`).

Never send repeatedly, and never send to addresses the user did not give or confirm.

## Schedule

- **Built-in scheduler.** `python harness.py schedule` keeps running and fires every weekday at
  `DAILY_SEND_TIME` (default 09:00, Asia/Kolkata). To test one run and stop:
  `python harness.py schedule --run-now --once`
- **What the job does.** First it drafts closure proposals for idle Resolved tickets. These are drafts
  only; a person still approves them. Then it sends the e-mail **only if** `EMAIL_ENABLED=true` and
  recipients are set. Otherwise it just saves a preview.
- **`EMAIL_ENABLED=true` is the user's standing permission** for the scheduled mail. Ask them to set it
  in `.env` themselves. Never switch it on unprompted.
- **Other schedulers.** cron, systemd timers, Windows Task Scheduler and Docker are covered in
  [references/scheduling.md](references/scheduling.md).

Related settings:
- in `.env`: `EMAIL_ENABLED`, `SMTP_*`, `DAILY_RECIPIENTS`, `DAILY_SEND_TIME`;
- in `config/settings.yaml` under `email`: `send_days`, `lookback_hours` (24), `sla_window_days` (30),
  `attach_csv`, `max_rows`, `dashboard_url`, `subject_prefix`.

## What the e-mail contains

The e-mail has these sections:
- KPI tiles;
- SLA compliance line;
- pending approvals banner;
- SLA breached;
- at risk;
- new tickets;
- closed tickets;
- overdue;
- assigned to me;
- team workload.

It carries a CSV attachment of open tickets with their SLA state. It is read-only: no ticket is
changed by building or sending it.

## Permission rules

- Previewing is always fine. Sending e-mail needs the user's explicit go-ahead each time, except for
  the schedule they enabled themselves.
- Never change tickets yourself. Never run `close approve`, `close review`, `close reject` or
  `close execute`. Never work around the interactive-terminal or PIN checks, and never ask for an
  approver PIN.
- Never print secrets from `.env` (SMTP password, API key).
