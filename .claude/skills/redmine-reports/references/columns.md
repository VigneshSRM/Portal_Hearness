# Report columns

Use the keys with `--columns` and `--group-by`. **Default** columns are used when `--columns` is omitted.
Columns marked **group** can be used with `--group-by`.

## Tickets (`--dataset issues`)

Date fields: `created_on`, `closed_on`, `updated_on`, `open_now`

| Key | Label | Group | Default |
|---|---|---|---|
| `id` | ID |  | default |
| `project` | Project | group | default |
| `tracker` | Tracker | group | default |
| `status` | Status | group | default |
| `is_closed` | Closed? | group |  |
| `priority` | Priority | group | default |
| `subject` | Subject |  | default |
| `author` | Author | group |  |
| `assigned_to` | Assignee | group | default |
| `category` | Category | group |  |
| `version` | Target version | group |  |
| `created_on` | Created |  | default |
| `updated_on` | Updated |  |  |
| `closed_on` | Closed |  | default |
| `created_date` | Created (day) | group |  |
| `created_week` | Created (week) | group |  |
| `created_month` | Created (month) | group |  |
| `closed_week` | Closed (week) | group |  |
| `closed_month` | Closed (month) | group |  |
| `start_date` | Start date |  |  |
| `due_date` | Due date |  | default |
| `overdue` | Overdue? | group |  |
| `done_ratio` | % Done |  |  |
| `estimated_hours` | Estimated (h) |  |  |
| `spent_hours` | Spent (h) |  | default |
| `age_days` | Age (days) |  |  |
| `resolution_hours` | Resolution (calendar h) |  |  |
| `sla_rule` | SLA rule | group |  |
| `sla_state` | SLA state | group | default |
| `sla_resolution_target_h` | SLA resolution target (h) |  |  |
| `sla_resolution_h` | SLA resolution elapsed (business h) |  |  |
| `sla_resolution_state` | SLA resolution | group |  |
| `sla_due` | SLA due |  |  |
| `sla_response_target_h` | SLA response target (h) |  |  |
| `sla_response_h` | First response (business h) |  |  |
| `sla_response_state` | SLA response | group |  |
| `url` | URL |  |  |

Custom fields are added automatically as `cf_<id>` (label = field name, groupable). Find the ids with `python harness.py api /issues/<issue-id>.json` (`custom_fields` lists `id` and `name`).

## Tickets with SLA details (`--dataset sla`)

Date fields: `created_on`, `closed_on`, `updated_on`, `open_now`

| Key | Label | Group | Default |
|---|---|---|---|
| `id` | ID |  | default |
| `project` | Project | group | default |
| `tracker` | Tracker | group |  |
| `status` | Status | group | default |
| `is_closed` | Closed? | group |  |
| `priority` | Priority | group | default |
| `subject` | Subject |  | default |
| `author` | Author | group |  |
| `assigned_to` | Assignee | group | default |
| `category` | Category | group |  |
| `version` | Target version | group |  |
| `created_on` | Created |  | default |
| `updated_on` | Updated |  |  |
| `closed_on` | Closed |  | default |
| `created_date` | Created (day) | group |  |
| `created_week` | Created (week) | group |  |
| `created_month` | Created (month) | group |  |
| `closed_week` | Closed (week) | group |  |
| `closed_month` | Closed (month) | group |  |
| `start_date` | Start date |  |  |
| `due_date` | Due date |  |  |
| `overdue` | Overdue? | group |  |
| `done_ratio` | % Done |  |  |
| `estimated_hours` | Estimated (h) |  |  |
| `spent_hours` | Spent (h) |  |  |
| `age_days` | Age (days) |  |  |
| `resolution_hours` | Resolution (calendar h) |  |  |
| `sla_rule` | SLA rule | group | default |
| `sla_state` | SLA state | group |  |
| `sla_resolution_target_h` | SLA resolution target (h) |  | default |
| `sla_resolution_h` | SLA resolution elapsed (business h) |  | default |
| `sla_resolution_state` | SLA resolution | group | default |
| `sla_due` | SLA due |  | default |
| `sla_response_target_h` | SLA response target (h) |  | default |
| `sla_response_h` | First response (business h) |  | default |
| `sla_response_state` | SLA response | group | default |
| `url` | URL |  |  |

Custom fields are added automatically as `cf_<id>` (label = field name, groupable). Find the ids with `python harness.py api /issues/<issue-id>.json` (`custom_fields` lists `id` and `name`).

## Time entries (`--dataset time_entries`)

Date fields: `spent_on`

| Key | Label | Group | Default |
|---|---|---|---|
| `id` | Entry ID |  |  |
| `spent_on` | Date | group | default |
| `spent_week` | Week | group |  |
| `spent_month` | Month | group |  |
| `user` | User | group | default |
| `project` | Project | group | default |
| `issue_id` | Issue | group | default |
| `issue_subject` | Issue subject |  | default |
| `tracker` | Tracker | group |  |
| `issue_status` | Issue status | group |  |
| `activity` | Activity | group | default |
| `hours` | Hours |  | default |
| `comments` | Comments |  | default |

## Team summary (`--dataset team`)

Date fields: `window`

| Key | Label | Group | Default |
|---|---|---|---|
| `name` | Member |  | default |
| `open` | Open |  | default |
| `open_high` | Open high-priority |  | default |
| `overdue` | Overdue |  | default |
| `breached_open` | SLA breached (open) |  | default |
| `at_risk_open` | SLA at risk (open) |  | default |
| `closed` | Closed in window |  | default |
| `avg_resolution_hours` | Avg resolution (h) |  | default |
| `sla_compliance` | SLA compliance % |  | default |
| `hours` | Hours logged |  | default |
| `capacity_hours` | Capacity (h) |  | default |
| `utilization` | Utilisation % |  | default |
| `oldest_open_days` | Oldest open (days) |  | default |

## Totals produced by `--group-by`

| Dataset | Totals (keys usable with `--sort`) |
|---|---|
| issues, sla | `count` (Tickets), `open` (Open), `closed` (Closed), `overdue` (Overdue), `sla_breached` (SLA breached), `sla_compliance_pct` (SLA compliance %), `avg_resolution_h` (Avg resolution (h)), `estimated_h` (Estimated (h)), `spent_h` (Spent (h)) |
| time_entries | `entries` (Entries), `hours` (Hours), `issues` (Issues), `people` (People) |
| team | (cannot be grouped - one row per person already) |
