# AGENTS.md - instructions for AI agents working in this repository

This repository is **Portal Harness**, an automation harness and agent for the Redmine
ticketing system. These instructions apply to any AI assistant that reads or changes this
repo or runs its commands: Codex, Claude Code, Copilot, Cursor and others.

## Prime directive: no changes without permission

The product rule is: **the agent must never change anything without the user's permission.**
The code enforces it in three places:

| Layer | File | What it guarantees |
|---|---|---|
| Write guard | `redmine_agent/guard.py` | Every POST/PUT needs an `Approval` whose SHA-256 digest matches the exact payload. The approver must be a human, not `agent`, `automation`, `scheduler` or `mcp`. |
| HTTP client | `redmine_agent/client.py` | `_request()` refuses any non-GET call that did not come through a guarded method. Writes are never retried automatically. |
| Workflow | `redmine_agent/approvals.py` | Only *approved* proposals execute. A ticket that changed since review becomes `stale`. Results are verified after the PUT. Everything is audited. |

When you modify this code:

- **Never** add a code path that writes to Redmine without going through `WriteGuard.authorize`.
- **Never** give an LLM-facing tool (`redmine_agent/agent/tools.py`, `agent/mcp_server.py`) the
  ability to approve proposals. Approval belongs to humans: the dashboard, or an interactive CLI
  with a TTY.
- New tools must declare a level (`read`, `draft` or `write`) and be added to `agent/agent.yaml`.
  The test `tests/test_agent_tools.py` fails if the manifest and the code drift apart.
- Keep `tests/test_guard.py` and `tests/test_approvals.py` green. They are the safety net.

When you *operate* this repo (running commands for a user):

- Read-only commands are always fine: `check`, `my-tickets`, `overview`, `sla`, `team`,
  `performance`, `time`, `export`, `close candidates`, `close list`, `close show`,
  `templates list|show|preview`, `daily-overview` (preview), `audit`.
- Drafting is fine when the user asks for it: `close draft`, `close auto-draft`. Drafting
  changes nothing in Redmine.
- **Do not** run `close review`, `close approve`, `close reject` or `close execute`, and do not
  run `daily-overview --send`, unless the user explicitly asks in this conversation. The approval
  commands require an interactive terminal by design. Do not work around that (for example with
  `script`, `expect` or a pseudo-TTY).
- Never print, log or commit secrets from `.env` or `config/settings.yaml`.

## Project map

```
redmine_agent/
  config.py            settings: YAML + ${ENV} interpolation + env overrides
  client.py            Redmine REST client (auth, pagination, retries, error hints)
  guard.py             WriteGuard + Approval (permission enforcement)
  models.py            Issue / Journal / TimeEntry views over API JSON
  data.py              cached read layer + Snapshot (issues, time entries, journals)
  sla.py               business-hours calendar + SLA engine (response & resolution)
  analytics.py         overview / sla / team / performance / time / my-tickets numbers
  reports.py           customisable reports -> CSV (formula-injection safe)
  closure_templates.py sandboxed Jinja2 closure templates + YAML store
  approvals.py         closure workflow: draft -> approve -> execute -> verify
  store.py             SQLite: proposals, audit log, journal cache
  mailer.py            daily overview e-mail (HTML + text, SMTP, outbox preview)
  scheduler.py         daily job (auto-draft + e-mail)
  cli.py               command-line interface
  web/                 FastAPI dashboard (Jinja templates, Chart.js vendored)
  agent/               LLM tools, OpenAI-compatible runtime, MCP server
  mock/                mock Redmine REST API (demos & tests)
agent/                 agent manifest + system prompt
config/                example settings + starter closure templates
docs/                  architecture, REST API notes, permissions, scheduling
tests/                 pytest suite (runs against the mock server)
```

## Dev workflow

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest                                              # all tests use the built-in mock Redmine
python -m redmine_agent mock-server --port 3000     # terminal 1
REDMINE_URL=http://127.0.0.1:3000 REDMINE_API_KEY=demo-api-key python -m redmine_agent dashboard   # terminal 2
```

Conventions: Python ≥ 3.10, type hints, no new heavy dependencies without a reason, keep
CLI output ASCII-only so it works in Windows consoles, and follow the Redmine REST API
conventions documented in `docs/redmine-rest-api.md`.
