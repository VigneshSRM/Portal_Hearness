# Settings reference

Two files configure the harness:

- **`.env`** holds secrets and per-machine values. It is never committed. `python harness.py init`
  creates it from `.env.example`.
- **`config/settings.yaml`** holds team policy: SLA, calendar, closure rules and reports. `init` creates
  it from `config/settings.example.yaml`. YAML values may use `${ENV_VAR}` or `${ENV_VAR:-default}`.

Environment variables override YAML.

## `.env` variables

| Variable | Meaning |
|---|---|
| `REDMINE_URL` | Redmine root URL used by the harness |
| `REDMINE_PUBLIC_URL` | URL people open in a browser, for ticket links, if it differs from `REDMINE_URL` |
| `REDMINE_API_KEY` | API key. Alternative: `REDMINE_USERNAME` with `REDMINE_PASSWORD` |
| `REDMINE_READ_ONLY` | `true` blocks every change, even approved ones |
| `REDMINE_CA_BUNDLE` | PEM file of your company certificate authority |
| `REDMINE_PROJECTS` | Limit the harness to these projects (code from the project's address, number or name; comma-separated) |
| `REDMINE_IMPERSONATE` | Admin keys only: act as this login (`X-Redmine-Switch-User`) |
| `HARNESS_TIMEZONE` | IANA timezone (default `Asia/Kolkata`) |
| `EMAIL_ENABLED` | `true` lets the scheduler really send the daily e-mail |
| `SMTP_HOST` | Mail server, for example `smtp.office365.com` (port 587, `starttls`) |
| `SMTP_PORT` | Mail server port |
| `SMTP_SECURITY` | `starttls`, `ssl` or `none` |
| `SMTP_USERNAME` | Mail server login |
| `SMTP_PASSWORD` | Mail server password |
| `SMTP_SENDER` | From address of the daily e-mail |
| `DAILY_RECIPIENTS` | Comma-separated recipients of the daily e-mail |
| `DAILY_SEND_TIME` | `HH:MM` (default `09:00`) |
| `DASHBOARD_HOST` | Dashboard bind address. Use `0.0.0.0` to share it on the network (set a login) |
| `DASHBOARD_PORT` | Dashboard port |
| `DASHBOARD_USERNAME` | Basic-auth login for the dashboard |
| `DASHBOARD_PASSWORD` | Basic-auth password for the dashboard |
| `LLM_BASE_URL` | Optional, only for the built-in `chat` command |
| `LLM_API_KEY` | Optional, only for the built-in `chat` command |
| `LLM_MODEL` | Optional, only for the built-in `chat` command |

## `config/settings.yaml` highlights

```yaml
sla:
  rules:                      # first match wins; match on priority / tracker / project ("A|B" = either)
    - { priority: Urgent, response_hours: 2, resolution_hours: 8 }
    - { name: Features, tracker: Feature, resolution_hours: 240 }
  default_response_hours: 8
  default_resolution_hours: 72
  at_risk_ratio: 0.8          # "at risk" once 80% of the window is used
  business_hours: { enabled: true, start: "09:00", end: "18:00", workdays: [mon, tue, wed, thu, fri],
                    holidays: ["2026-12-25"] }
closure:
  scope: me                   # candidates: me | all
  candidate_statuses: [Resolved]
  idle_days: 3
  close_status: Closed
  template_rules:             # automatic template per tracker
    - { tracker: Bug, template: bug-fixed }
team:
  members: []                 # names or user ids for the Team view ([] = everyone assigned)
  capacity_hours_per_day: 8
reports:
  presets:                    # reusable reports: python harness.py export --preset weekly-sla
    weekly-sla: { title: Weekly SLA report, dataset: sla, days: 7, date_field: closed_on, status: closed }
```

After editing, run `python harness.py check` to validate. Invalid values produce a clear error, for
example an unknown timezone, a bad `HH:MM` time or an unknown weekday.
