---
name: redmine-dashboards
description: Report Redmine service metrics - overview KPIs (open, created vs closed, overdue, unassigned, ticket age), SLA compliance (resolution and first response, breaches and at-risk tickets by priority), team workload and utilisation, performance (throughput, resolution times, P90, reopen rate, estimate accuracy) and time spent (hours by person, project, activity, tracker). Use when the user asks how the team or service desk is doing, about SLA, backlog, workload, performance, hours logged or trends for a period or project, or wants to open the web dashboard.
license: MIT
compatibility: Needs the Portal Harness repository (python harness.py) with Redmine configured, and network access to Redmine.
metadata:
  author: portal-harness
  version: "1.1"
---

# Dashboards and metrics

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py overview --days 30
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher picks the project's
  virtualenv by itself.
- Exit codes: `0` ok, `2` error, `4` not installed. For configuration or connection errors, switch to the
  `redmine-setup` skill.

## Commands

Every command accepts either `--days N` (default 30) or `--from YYYY-MM-DD --to YYYY-MM-DD`, and also
`--project <project>`.

| Question | Command |
|---|---|
| Overall health | `python harness.py overview --days 30` |
| SLA compliance, breaches, at risk | `python harness.py sla --days 30` |
| Team workload and utilisation | `python harness.py team --days 30` |
| Speed and quality | `python harness.py performance --days 90` |
| Hours logged | `python harness.py time --from 2026-10-01 --to 2026-10-31` |
| One project | `python harness.py sla --days 30 --project <project>` |
| Full data for tables and trends | `python harness.py overview --days 30 --json` |

The text output is a compact summary and is usually enough. Use `--json` when you need every field: the
lists of breached, at-risk and overdue tickets, daily or weekly trends, and breakdowns.

## How to answer

- Start with a one-line verdict. Example: "Resolution SLA compliance is 84.6% over the last 30 days,
  below a 90% goal."
- Follow with 3–6 bullets carrying the numbers that matter.
- Then list the tickets that need action (breached or at risk) as `#id`.
- Always state the period and project scope you used.
- To compare periods, run the command for each window (for example `--days 7` and
  `--from ... --to ...` for the week before) and report the differences.
- Flag concerns:
  - breaches on high or urgent priorities;
  - a growing backlog (`net_flow` above 0);
  - overdue or unassigned tickets;
  - utilisation above 100% or very low;
  - poor estimate accuracy;
  - reopened tickets.

What each number means and its JSON field: [references/metrics.md](references/metrics.md).

## Web dashboard

`python harness.py dashboard` serves http://localhost:8000 and keeps running. Start it in the background
if your tool supports that, otherwise ask the user to run it in another terminal. It has these pages:
- Overview, SLA, Team, Performance, Time spent;
- My tickets;
- Reports & CSV;
- Closure templates;
- Approvals;
- Audit log.

## Gotchas

- SLA time counts business hours (default 09:00-18:00 Mon-Fri, Asia/Kolkata) and skips holidays from
  `config/settings.yaml`.
- First-response compliance needs ticket history. It reads "not tracked" when `sla.track_first_response`
  is off.
- Redmine does not record who closed a ticket, so team closures are credited to the assignee.
- Utilisation = hours logged ÷ (working days × capacity, 8 h by default).
- Results are cached for about 2 minutes.

## Permission rules

- This skill is read-only and changes nothing.
- Never run `close approve`, `close review`, `close reject` or `close execute`. Never work around the
  harness's interactive-terminal or PIN checks, and never ask for an approver PIN.
- Never print secrets from `.env`.
