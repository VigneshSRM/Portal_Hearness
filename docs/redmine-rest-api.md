# Redmine REST API notes

The harness follows the official developer guide:
<https://www.redmine.org/projects/redmine/wiki/Rest_api>

## Enabling access

1. Administration → Settings → **API** → tick **Enable REST web service**. An admin must do this.
2. Each user finds their key under **My account** → *API access key* → **Show**.
3. Put the key in `.env` as `REDMINE_API_KEY`. The harness sends it in the `X-Redmine-API-Key`
   header, never in the URL, so it does not end up in proxy logs. HTTP Basic auth
   (`REDMINE_USERNAME` / `REDMINE_PASSWORD`) also works.

The harness acts *as the owner of the API key*. "My tickets" means the tickets assigned to that
account, and Redmine's role permissions limit what it can see and change.

Admins can impersonate another user with `REDMINE_IMPERSONATE=<login>`, which sends the
`X-Redmine-Switch-User` header. Redmine answers **412** if the login is unknown.

## Endpoints used

| Purpose | Endpoint | Since |
|---|---|---|
| Current user ("me") | `GET /users/current.json` | 1.1 |
| Tickets (lists, filters) | `GET /issues.json` | 1.0 |
| One ticket + history + workflow | `GET /issues/:id.json?include=journals,allowed_statuses` | journals 1.0+, allowed_statuses 5.0 |
| **Update/close ticket** (approved only) | `PUT /issues/:id.json` | 1.0 |
| Time spent | `GET /time_entries.json?from=&to=` | 1.1 |
| Statuses (`is_closed`) | `GET /issue_statuses.json` | 1.3 |
| Trackers | `GET /trackers.json` | 1.3 |
| Priorities, activities | `GET /enumerations/issue_priorities.json`, `/enumerations/time_entry_activities.json` | 2.2 |
| Projects | `GET /projects.json` | 1.0 |
| Memberships | `GET /projects/:id/memberships.json` | 1.4 |

The harness works with Redmine **4.1+**. Redmine **5.0+** is recommended, because it adds
`allowed_statuses`, which lets the harness check workflow permissions before review.

## Conventions the client implements

- **Pagination**: `offset` and `limit` (Redmine caps `limit` at 100), looping until `total_count`
  is reached. `redmine.max_items` sets a safety cap.
- **Filters**: `status_id=open|closed|*|<id>|<id>|<id>`, `assigned_to_id=me`, `project_id=<id>`,
  and date ranges `created_on=><2026-10-01|2026-10-31` (also `>=` and `<=`). Requests URL-encode
  the operators.
- **Open/closed** comes from `issue_statuses.is_closed`, not from `closed_on`. Redmine keeps
  `closed_on` when a ticket is reopened.
- **Windows**: the harness fetches every open ticket, plus the tickets created or closed in the
  window. The date filter is widened by ±1 day and the result is then trimmed precisely in your
  timezone, because Redmine compares dates in the API user's timezone.
- **Writes** send `Content-Type: application/json`, as Redmine requires for POST and PUT. A
  closure is one PUT:

  ```json
  PUT /issues/123.json
  {"issue": {"status_id": 5, "notes": "...", "done_ratio": 100,
             "private_notes": false, "custom_fields": [{"id": 1, "value": "Fixed"}]}}
  ```

  - Success: `204 No Content`.
  - Validation errors: `422` with `{"errors": ["..."]}`, shown to the reviewer as-is.
- **Silent workflow rejection**: if your role is not allowed to set the target status, Redmine
  still answers 204 and adds the note, but leaves the status unchanged. That is why the harness
  re-reads the ticket after every update.

## Errors and what they usually mean

| HTTP | Meaning | Typical fix |
|---|---|---|
| 401 | Bad credentials, or the REST API is disabled | Check `REDMINE_API_KEY`; enable the REST web service |
| 403 | Your role lacks permission (e.g. `/users.json` is admin-only) | Use an account with the needed role |
| 404 | Not found, or not visible to you | Check the project or ticket and its visibility |
| 412 | Impersonation login invalid | Fix `REDMINE_IMPERSONATE` |
| 422 | Validation failed | Read the listed errors, for example a required custom field |
| 5xx / timeout | Server or proxy problem | GETs are retried with backoff; writes are not |

If an internal certificate authority signs Redmine's certificate, set `redmine.ca_bundle` to the
PEM file. Proxies are picked up from the standard `HTTPS_PROXY` / `NO_PROXY` variables.
