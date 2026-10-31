# Architecture

```
                ┌──────────────────────── Surfaces ────────────────────────┐
                │  CLI (cli.py)   Dashboard (web/)   Agent chat   MCP server │
                │                 FastAPI + Chart.js  (agent/)    (agent/)   │
                └───────┬──────────────┬──────────────┬─────────────┬───────┘
                        │              │              │             │
                        ▼              ▼              ▼             ▼
   ┌────────────── Services (harness.py wires everything) ──────────────────┐
   │ analytics.py   reports.py   mailer.py   scheduler.py   approvals.py      │
   │ (overview/SLA/ (CSV builder) (daily      (daily job)   (draft→approve→   │
   │  team/perf/                  e-mail)                    execute→verify)  │
   │  time)                                                                   │
   │        ▲              ▲                                   │               │
   │        └──── data.py (TTL cache, Snapshot) ◄── sla.py      │               │
   │                    │                                       ▼               │
   │                    │                      closure_templates.py (Jinja2    │
   │                    │                      sandbox, YAML store)            │
   └────────────────────┼───────────────────────────────────────┼─────────────┘
                        ▼                                       ▼
            client.py (Redmine REST)  ◄──────── guard.py (WriteGuard: no write without
                        │                                   human approval + digest)
                        ▼
                 Redmine REST API            store.py (SQLite: proposals, audit, journal cache)
```

## Key design decisions

- **Permission-first.** Writes are impossible without an `Approval` that matches the payload's
  SHA-256 digest and comes from a human. See [permissions-and-approvals.md](permissions-and-approvals.md).
- **One composition root.** `Harness.create(settings)` builds the client, store, data service,
  template store and workflow, and connects the guard's verifier to the approval store.
- **Snapshots.** Analytics are pure functions over a `Snapshot`. A snapshot holds:
  - every open ticket, plus the tickets created or closed in the window;
  - the window's time entries;
  - optional journals, used to measure first response.

  The snapshot is cached for `dashboard.cache_ttl_seconds`. Journals are fetched in parallel and
  stored in SQLite under each ticket's `updated_on`, so they are only downloaded again after the
  ticket changes.
- **SLA engine.** It uses a business-hours calendar (workdays, hours, holidays, timezone).
  - The *resolution* SLA runs from `created_on` to `closed_on`, or to now if the ticket is open.
  - The *first-response* SLA ends at the first journal by someone other than the author that adds
    a public note or changes the status.
  - Rules match on priority, tracker or project. The first matching rule wins.
- **Reports.** A column registry per dataset feeds both flat exports and grouped aggregates.
  - Custom fields become `cf_<id>` columns automatically.
  - CSV files are UTF-8 with a BOM, so they open in Excel.
  - Values that start with `= + - @` are neutralised to block formula injection.
- **Templates.** Jinja2 runs in a `SandboxedEnvironment` with `StrictUndefined`. Templates are
  validated on save: syntax, and every placeholder must be declared. They are written atomically
  to YAML.
- **Agent tools.** Each tool has a permission level.
  - `read` and `draft` tools run freely.
  - `write` tools go through an approver callback: a TTY prompt in the CLI chat, and
    *deny-all* for MCP.
  - The tool list in `agent/agent.yaml` is checked against the code by a test.
- **Provider-agnostic LLM.** The chat runtime speaks the OpenAI-compatible Chat Completions
  protocol with plain `requests`, so it works with OpenAI, Azure OpenAI, Ollama, vLLM, LM Studio,
  OpenRouter, and others.
- **Offline-friendly.** Chart.js is vendored and there is no CDN dependency, so the dashboard
  works on an intranet.

## Data flow: closing a ticket

1. **Find candidates.** `candidates()` looks for tickets in `closure.candidate_statuses` that have
   been idle for at least `closure.idle_days` (scope `me` or `all`).
2. **Draft.** `draft()` re-reads the ticket with `allowed_statuses` and renders the template. It
   maps custom field names to ids, then stores a *pending* proposal with a baseline
   (`status_id`, `updated_on`).
3. **Review.** A human approves. `approve()` records the reviewer and the SHA-256 of the exact
   payload.
4. **Execute.** `execute()` re-checks the digest and re-reads the ticket; if it changed, the
   proposal becomes *stale*. It then calls `client.update_issue(approval=...)`, which the guard
   checks again.
5. **Verify.** The ticket is re-read to confirm the status really changed, then the result is
   written to the audit log.
