# Metric definitions

Field names are the keys in the `--json` output.

## overview (`kpis`)

| Field | Meaning |
|---|---|
| `open` | Tickets open now, regardless of the window |
| `created` | Tickets created in the window |
| `closed` | Tickets closed in the window |
| `net_flow` | `created - closed`; above 0 means the backlog grows |
| `overdue` | Open tickets past their due date |
| `unassigned` | Open tickets with no assignee |
| `high_priority_open` | Open tickets with priority High, Urgent or Immediate |
| `sla_compliance` | % of tickets closed in the window that met their resolution target (`sla_judged` = how many had a target) |
| `sla_breached_open` | Open tickets already past their SLA |
| `sla_at_risk_open` | Open tickets close to their SLA limit |
| `avg_resolution` | Average calendar time from created to closed, for tickets closed in the window |
| `median_resolution` | Median of the same |
| `hours_spent` | Hours logged in the window (time entries) |

Lists:
- `breached`, `at_risk`, `overdue`: the top 10 tickets each.
- `trend`: per day, with `created`, `closed` and `hours`.
- `open_by_status`, `open_by_priority`, `open_by_tracker`, `open_by_project` and `open_by_assignee`: counts.
- `aging`: open tickets by age bucket.

## sla

| Field | Meaning |
|---|---|
| `kpis.resolution_compliance` | Met ÷ (met + breached) for tickets closed in the window |
| `kpis.response_compliance` | First response met ÷ judged, for tickets created in the window. Needs ticket history |
| `kpis.breached_open` | Open tickets past their SLA |
| `kpis.at_risk_open` | Open tickets near their SLA limit |
| `by_priority[]` | Per priority: rule, targets, `closed`, `met`, `breached`, `compliance`, `avg_resolution` (business hours), and open / open breached / open at risk |
| `breached_open[]` | Open tickets past their SLA, worst first |
| `at_risk_open[]` | Open tickets near their limit, worst first |
| `breached_closed[]` | Tickets closed late in the window |
| `rules[]` | The configured SLA rules |
| `calendar` | The business-hours description and the at-risk ratio |

How the SLA is measured:
- **Resolution** runs in business hours from creation to closure (or to now, while open).
- **First response** runs to the first public note or status change by someone other than the author.
  Closing the ticket also counts.
- A ticket is **at risk** once it has used at least `at_risk_ratio` (default 0.8) of its window.

## team (`members[]`, `totals`)

Per person:

| Field | Meaning |
|---|---|
| `open` | Open tickets |
| `open_high` | Open high-priority tickets |
| `overdue` | Open tickets past their due date |
| `breached_open` | Open tickets past their SLA |
| `at_risk_open` | Open tickets near their SLA limit |
| `oldest_open_days` | Age of their oldest open ticket |
| `closed` | Tickets closed in the window (credited to the assignee) |
| `avg_resolution` | Average resolution time of those tickets |
| `sla_compliance` | Their resolution compliance |
| `hours` | Hours logged |
| `capacity_hours` | Working days in the window × hours per day |
| `utilization` | `hours ÷ capacity_hours` |

`totals.unassigned_open` counts open tickets that nobody owns.

## performance (`kpis`)

| Field | Meaning |
|---|---|
| `throughput_per_day` | Tickets closed per working day |
| `backlog_change` | `created - closed` |
| `avg_resolution` | Average calendar resolution time |
| `median_resolution` | Median calendar resolution time |
| `p90_resolution` | 90% of tickets were resolved faster than this |
| `avg_first_response` | Average time to first response (business hours) |
| `reopened` | Tickets moved from a closed status back to an open one in the window |
| `reopen_rate` | `reopened ÷ closed` |
| `estimate_within_20pct` | Share of closed tickets whose spent time was within ±20% of the estimate |
| `spent_vs_estimate_ratio` | Average of spent ÷ estimated (1.0 = on estimate) |

Also included:
- `weekly`: created, closed, average resolution and SLA % per week.
- `resolution_distribution`: closed tickets by resolution time.
- `by_tracker`, `by_priority`, `by_project` and `top_closers`.

## time (`kpis`)

| Field | Meaning |
|---|---|
| `total_hours` | Hours logged in the window |
| `entries` | Number of time entries |
| `people` | People who logged time |
| `issues` | Tickets with time logged |
| `avg_hours_per_workday` | Hours per working day |

Also included:
- `by_user`, `by_project`, `by_activity` and `by_tracker`: hours.
- `trend.hours`: hours per day.
- `top_issues`: the most time-consuming tickets in the window.
- `over_budget`: tickets whose spent time exceeds the estimate.
