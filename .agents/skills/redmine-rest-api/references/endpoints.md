# Redmine REST API reference (read side)

Source: the official developer guide, https://www.redmine.org/projects/redmine/wiki/Rest_api

Authentication is handled by the harness. It sends the `X-Redmine-API-Key` header, or basic auth if
configured. An admin must enable *Administration → Settings → API → Enable REST web service*.

## Endpoints

| Resource | Path | Since | Notes |
|---|---|---|---|
| Issues | `/issues.json`, `/issues/<id>.json` | 1.0 | Filters below |
| Projects | `/projects.json`, `/projects/<id-or-identifier>.json` | 1.0 | `include=trackers,issue_categories,enabled_modules` |
| Memberships | `/projects/<project>/memberships.json` | 1.4 | User or group plus roles |
| Versions | `/projects/<project>/versions.json` | 1.3 | |
| Issue categories | `/projects/<project>/issue_categories.json` | 1.3 | |
| Issue relations | `/issues/<id>/relations.json` | 1.3 | |
| Time entries | `/time_entries.json` | 1.1 | `from`, `to`, `user_id` (id or `me`), `project_id`, `issue_id` |
| Users | `/users.json` (admin), `/users/<id>.json`, `/users/current.json` | 1.1 | `include=memberships,groups` |
| My account | `/my/account.json` | 4.1 | |
| Issue statuses | `/issue_statuses.json` | 1.3 | `is_closed` flag |
| Trackers | `/trackers.json` | 1.3 | |
| Enumerations | `/enumerations/issue_priorities.json`, `/enumerations/time_entry_activities.json`, `/enumerations/document_categories.json` | 2.2 | |
| Saved queries | `/queries.json` | 1.3 | Use `query_id=<id>` on `/issues.json` |
| Custom fields | `/custom_fields.json` | 2.4 | Admin only |
| Search | `/search.json?q=...` | 3.3 | Results under `results` |
| News, wiki, files | `/news.json`, `/projects/<p>/wiki/index.json`, `/projects/<p>/files.json` | 1.1+ | |

## Issue filters (`/issues.json`)

- `status_id`: `open` (default), `closed`, `*` (all), an id, or several ids such as `1|2`
- `assigned_to_id`: a user id, `me`, `!*` (unassigned) or `*` (anyone)
- `project_id`: a numeric id. Add `subproject_id=!*` to exclude subprojects
- `tracker_id`, `priority_id`, `author_id`, `category_id`, `fixed_version_id`, `parent_id`: ids; several
  as `1|2`
- `issue_id`: comma-separated ids such as `1,2,3`
- `created_on`, `updated_on`, `closed_on`: one of these forms
  - `><2026-10-01|2026-10-31` (between)
  - `>=2026-10-01`
  - `<=2026-10-31`
  - full timestamps such as `>=2026-10-01T09:00:00Z`
- `cf_<id>`: a custom field value, e.g. `cf_1=Fixed`
- `subject`: `~text` (contains)
- `sort`: e.g. `updated_on:desc` or `priority:desc,id`
- `include`: on single issues `relations`, `attachments`, `journals` and `watchers`; also
  `allowed_statuses` (Redmine 5.0+)

On the command line, quote values that contain `* > < |`:
`--param "status_id=*" --param "created_on=><2026-10-01|2026-10-31"`

## Pagination

Collections accept `offset` and `limit` (Redmine caps `limit` at 100) and return `total_count`. Add
`--all` to fetch every page automatically.

## Responses and errors

| HTTP | Meaning |
|---|---|
| 200 | JSON body |
| 401 | Bad credentials or REST API disabled |
| 403 | Not allowed for this account's role |
| 404 | Not found or not visible |
| 412 | Impersonation login invalid |
| 422 | Validation errors (`{"errors": [...]}`); writes only, not used by `api` |
| 5xx | Server or proxy error. GETs are retried automatically |

Dates in responses are UTC ISO-8601 (`2026-10-31T09:00:00Z`). `closed_on` stays set when a ticket is
reopened. To know whether a ticket is closed now, check its status's `is_closed`.

## Writes (for understanding only)

Redmine changes tickets with `PUT /issues/<id>.json` and a body like
`{"issue": {"status_id": 5, "notes": "..."}}`. The harness performs this **only** for closure proposals
a person has approved, through `redmine-ticket-closure`. One Redmine quirk: if the workflow does not
allow the new status, Redmine silently keeps the old one but still adds the note. That is why the harness
re-reads the ticket after every change.
