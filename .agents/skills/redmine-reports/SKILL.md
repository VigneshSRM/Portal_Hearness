---
name: redmine-reports
description: Build customised Redmine reports and export them to CSV (Excel-ready) - choose the dataset (tickets, tickets with SLA details, time entries, team summary), the date range and date field, filters (project, status, assignee, tracker, priority, SLA state, subject text), columns including custom fields, group-by totals and sorting, or run a saved preset. Use when the user wants a report, spreadsheet, CSV or export, a timesheet, or a breakdown of tickets or hours "by" something.
license: MIT
compatibility: Needs the Portal Harness repository (python harness.py) with Redmine configured, and network access to Redmine.
metadata:
  author: portal-harness
  version: "1.1"
---

# Custom reports and CSV export

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py export --dataset issues --days 30 --json
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher picks the project's
  virtualenv by itself.
- Exit codes: `0` ok, `2` error (e.g. unknown column), `4` not installed. For configuration or connection
  errors, switch to the `redmine-setup` skill.

## Building the command

`python harness.py export` plus the options below. Always add `--title` and `--json`.

- **Dataset**: `--dataset issues` (tickets), `--dataset sla` (tickets with SLA columns),
  `--dataset time_entries` or `--dataset team`.
- **Window**: `--days N`, or `--from YYYY-MM-DD --to YYYY-MM-DD` (default: the last 30 days).
- **Date field** (tickets): `--date-field created_on` (default), `closed_on`, `updated_on`, or `open_now`.
  `open_now` is the current backlog and ignores dates. Time entries always use the day the time was spent.
- **Filters**:
  - `--project`
  - `--status` (`open`, `closed` or `all`)
  - `--assignee` (display name or user id; `"Priya Sharma|Arjun Nair"` for several)
  - `--tracker` (`"Bug|Support"` for several)
  - `--priority`
  - `--sla-state` (`breached`, `at_risk`, `on_track`, `met` or `pending`)
  - `--text` (subject contains)
- **Columns**: `--columns id,subject,priority`. The default set is used if omitted. List them with
  `python harness.py export --list-columns --dataset issues`.
- **Totals**: `--group-by assigned_to,priority` gives one row per group, with counts, SLA % and hours.
- **Order**: `--sort <column-or-total> --desc`, `--limit N`.
- **Presets**: `--preset <name>`. List them with `python harness.py export --list-presets`.
- **Output**: `--out <file.csv>` (default `data/exports/<title>-<timestamp>.csv`).

Every column key and the totals each dataset produces: [references/columns.md](references/columns.md).

## Examples

| Request | Command |
|---|---|
| Weekly SLA report | `python harness.py export --dataset sla --from 2026-10-19 --to 2026-10-25 --date-field closed_on --status closed --title "Weekly SLA" --json` |
| Open backlog by assignee and priority | `python harness.py export --dataset issues --date-field open_now --status open --group-by assigned_to,priority --title "Open backlog" --json` |
| October timesheet by person and project | `python harness.py export --dataset time_entries --from 2026-10-01 --to 2026-10-31 --group-by user,project --title "October timesheet" --json` |
| Breached tickets in the last 30 days | `python harness.py export --dataset sla --days 30 --sla-state breached --title "SLA breaches" --json` |
| Tickets created per week by tracker | `python harness.py export --dataset issues --days 90 --group-by created_week,tracker --sort created_week --title "Weekly intake" --json` |
| Bugs with a custom field | `python harness.py export --dataset issues --days 30 --tracker Bug --columns id,subject,status,cf_2 --title "Bugs" --json` |
| Team summary | `python harness.py export --dataset team --days 30 --title "Team summary" --json` |

## After exporting

- Tell the user the `file` path, the number of `rows` and the `window`.
- Show the first few `preview_rows` as a small table.
- Offer adjustments: columns, filters, grouping, period.
- Never paste a whole large CSV into the chat; summarise instead.
- If the user wants the file somewhere specific, re-run with `--out <path>`.

## Gotchas

- CSV files are UTF-8 with a BOM, so they open correctly in Excel. Text cells starting with
  `= + - @` get a leading `'` to block spreadsheet formula injection.
- `--assignee` matches the display name ("Priya Sharma") or the user id. `--tracker` and `--priority`
  take names; `Bug|Support` means either.
- Custom fields are `cf_<id>` columns. Find their ids with `python harness.py api /issues/<issue-id>.json`
  (in `custom_fields`).
- Grouping replaces the columns with totals, and the `team` dataset cannot be grouped. For grouped
  reports, `--sort` takes a total key such as `count` or `hours`.
- Reports are local files. Creating them changes nothing in Redmine.

## Permission rules

- Exporting is allowed (local files only). Never change tickets yourself.
- Never run `close approve`, `close review`, `close reject` or `close execute`. Never work around the
  interactive-terminal or PIN checks, and never ask for an approver PIN.
- Never print secrets from `.env`.
