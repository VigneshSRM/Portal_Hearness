---
name: redmine-rest-api
description: Query any Redmine REST API endpoint read-only (GET) through the harness - projects, versions, memberships, users, issue relations, watchers and journals, time entries, custom fields, saved queries and search - with filters and automatic pagination, following the official Redmine developer guide. Use when the user needs Redmine data the other Redmine skills do not cover, wants raw API data, or asks how the Redmine REST API works. Writing through the raw API is impossible by design.
license: MIT
compatibility: Needs the Portal Harness repository (python harness.py) with Redmine configured, and network access to Redmine.
metadata:
  author: portal-harness
  version: "1.1"
---

# Redmine REST API (read-only)

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py api /projects.json --all
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher picks the project's
  virtualenv by itself.
- The output is always JSON. Exit codes: `0` ok, `2` error (404, 403...), `4` not installed. For
  configuration errors, switch to `redmine-setup`.

## The command

`python harness.py api <path>` with these options:

| Option | Effect |
|---|---|
| `--param name=value` | Query parameter. Repeat it for several. |
| `--all` | Follow pagination (`offset`/`limit`, 100 per page) and return `{path, key, count, items}` |
| `--key <collection>` | Collection name for `--all`; guessed from the path, e.g. `issues` |
| `--max N` | Cap for `--all` (default 1000) |

Without `--all` you get Redmine's raw response, including `total_count` for collections.

## Examples

| Need | Command |
|---|---|
| Projects | `python harness.py api /projects.json --all` |
| Versions of a project | `python harness.py api /projects/<project>/versions.json` |
| Project members and roles | `python harness.py api /projects/<project>/memberships.json --all` |
| Ticket with relations, watchers, history | `python harness.py api /issues/<issue-id>.json --param include=relations,watchers,journals` |
| My time entries for a month | `python harness.py api /time_entries.json --param user_id=me --param from=2026-10-01 --param to=2026-10-31 --all` |
| Custom field definitions (admin) | `python harness.py api /custom_fields.json` |
| Saved queries | `python harness.py api /queries.json --all` |
| Full-text search | `python harness.py api /search.json --param q=backup --all --max 100` |

Issues with any filter (all statuses, tracker 1, created in October):

```bash
python harness.py api /issues.json --param "status_id=*" --param tracker_id=1 --param "created_on=><2026-10-01|2026-10-31" --all --max 500
```

**Quote parameters that contain `* > < |`.** The shell treats those characters specially, as in the
example above. PowerShell needs the same quotes.

Endpoints, filters, `include` options and error codes, from the official guide:
[references/endpoints.md](references/endpoints.md).

## How to work

- Prefer the dedicated skills: `redmine-my-tickets` for tickets, `redmine-dashboards` for metrics and
  `redmine-reports` for CSV. Use `api` to fill the gaps.
- Keep requests small: add filters, use `--max`, and summarise results instead of dumping JSON.
- `403` means the account's role may not see that resource (`/users.json` and `/custom_fields.json` are
  admin-only). `404` means it does not exist or is not visible.

## Permission rules

- GET only. This command has no way to POST, PUT or DELETE, by design. Never call the Redmine API any
  other way (e.g. `curl`) to change data.
- Ticket changes go through `redmine-ticket-closure`, with a person's approval.
- Never run `close approve`, `close review`, `close reject` or `close execute`. Never work around the
  interactive-terminal or PIN checks, and never ask for an approver PIN.
- Never print the API key or other secrets from `.env`.
