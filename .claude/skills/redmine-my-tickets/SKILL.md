---
name: redmine-my-tickets
description: List and inspect Redmine tickets - the tickets assigned to the current user sorted by urgency (SLA breached, at risk, overdue, high priority), one ticket's full details with SLA, history and allowed next statuses, and ticket search by text, status, project, assignee, tracker, priority or creation date. Use when the user asks about "my tickets", their queue or workload, what to work on next, a specific ticket number such as #1234, or wants to find tickets.
license: MIT
compatibility: Needs the Portal Harness repository (python harness.py) with Redmine configured, and network access to Redmine.
metadata:
  author: portal-harness
  version: "1.1"
---

# My tickets and ticket lookup

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py my-tickets --json
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher picks the project's
  virtualenv by itself - never activate it or install packages globally.
- Prefer `--json` and summarise; never dump raw JSON on the user.
- Exit codes: `0` ok, `2` error, `3` permission denied, `4` not installed. For configuration or connection
  errors, switch to the `redmine-setup` skill.

## Commands

| Need | Command |
|---|---|
| My open tickets, most urgent first | `python harness.py my-tickets --json` |
| Include closed ones | `python harness.py my-tickets --status all --json` |
| One project only | `python harness.py my-tickets --project <project> --json` |
| Save my tickets as CSV | `python harness.py my-tickets --csv my-tickets.csv` |
| One ticket in full | `python harness.py ticket <issue-id> --json` |
| Search by words in the subject | `python harness.py search --text "<words>" --status all --json` |
| Someone else's open queue | `python harness.py search --assignee <user-id> --json` |
| Recent bugs | `python harness.py search --tracker Bug --created-from 2026-10-01 --json` |

`search` filters: `--status open|closed|all` (default open), `--project`, `--assignee me|<user-id>`,
`--tracker <name>`, `--priority <name>`, `--text`, `--created-from YYYY-MM-DD`, `--created-to YYYY-MM-DD`,
`--limit N` (default 50). `<project>` is a project id, identifier or exact name.

## How to answer

1. Lead with the counts from `kpis`: open, SLA breached, at risk, overdue, high priority.
2. Show a compact table of the top rows: `#id`, priority, status, subject, SLA, due date. Rows are
   already sorted most urgent first.
3. Recommend what to work on next, in this order: SLA breached → at risk (highest priority first) →
   overdue → oldest high-priority ticket.
4. Cite tickets as `#123`, and add the `url` when it helps. Offer details instead of printing everything.

For one ticket (`ticket <issue-id> --json`), report:
- status, priority, assignee and age;
- the SLA (`sla_label`, `sla.resolution_hours` of `sla.resolution_target` business hours, `sla.response_state`);
- what happened recently (`recent_history`, newest last);
- `allowed_next_statuses`;
- any `open_proposals`.

## Field guide

Rows from `my-tickets --json` have these fields:
- `id`, `subject`, `project`, `tracker`, `status`, `priority`, `high_priority`;
- `assigned_to`, `due_date`, `overdue`, `age`, `updated_on`, `done_ratio`;
- `sla_state`, `sla_label`, `sla_due`, `sla_consumed_pct`, `url`.

SLA states:

| State | Meaning |
|---|---|
| `breached` | Past its target |
| `at_risk` | At least 80% of the window used |
| `on_track` | Within target |
| `pending` | No first response yet |
| `met` | Target met |
| `unknown` | First response not evaluated in lists |
| `n/a` | No target applies |

## Gotchas

- "My tickets" means tickets assigned to the Redmine account whose API key is configured (`check` shows who).
- SLA time counts business hours (default 09:00-18:00 Mon-Fri, Asia/Kolkata), so tickets do not age at
  night or at weekends.
- Lists evaluate the resolution SLA. `ticket <issue-id>` also evaluates the first response from the
  ticket history.
- Results are cached for about 2 minutes, so a ticket edited seconds ago may still show old values.

## Related skills

- To close a ticket, use `redmine-ticket-closure`.
- For team-wide numbers, use `redmine-dashboards`.
- For CSV files, use `redmine-reports`.

## Permission rules

- This skill is read-only. Never change tickets yourself; closures go through `redmine-ticket-closure`,
  where a person approves.
- Never run `close approve`, `close review`, `close reject` or `close execute`. Never work around the
  interactive-terminal or PIN checks, and never ask for an approver PIN.
- Never print secrets from `.env`.
