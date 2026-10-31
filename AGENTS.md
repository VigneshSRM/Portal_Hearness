# AGENTS.md - instructions for AI agents working in this repository

This repository is **Portal Harness**, an automation harness for the Redmine ticketing system. These
rules apply to every AI assistant that reads or changes the repo or runs its commands: Claude Code,
Codex, Gemini CLI, GitHub Copilot, Cursor, OpenCode and others. `CLAUDE.md` and `GEMINI.md` import
this file.

## Prime directive: no changes without permission

**Never change anything in Redmine without the user's explicit permission.** The code enforces this:

| Layer | File | What it guarantees |
|---|---|---|
| Write guard | `redmine_agent/guard.py` | Every POST/PUT needs an `Approval` whose SHA-256 digest matches the exact payload. The approver must be a human, never `agent`, `automation`, `scheduler` or `mcp`. |
| HTTP client | `redmine_agent/client.py` | `_request()` refuses any non-GET call that did not come through a guarded method. Writes are never retried. |
| Workflow | `redmine_agent/approvals.py` | Only *approved* proposals run. A ticket that changed since drafting becomes `stale`. Results are verified after the PUT, and everything is audited. |
| Approver PIN | `redmine_agent/approvers.py` | When approvers are registered, every approval needs a registered person's PIN, which the agent never sees. |
| CLI | `redmine_agent/cli.py` | Approval commands need an interactive terminal. |

## Operating the harness (skills)

Skills in the open Agent Skills format teach you how to operate the harness:

- `.agents/skills/` is the canonical copy, read by Codex, Gemini CLI, Copilot CLI, Cursor and OpenCode.
- `.claude/skills/` is an identical mirror for Claude Code.

| Skill | Use it for |
|---|---|
| `redmine-setup` | install, configure, check connectivity, troubleshoot |
| `redmine-my-tickets` | my tickets, ticket details, search |
| `redmine-dashboards` | overview, SLA, team, performance, time spent |
| `redmine-reports` | custom CSV exports |
| `redmine-daily-overview` | preview, send and schedule the daily e-mail |
| `redmine-closure-templates` | create, edit and preview closure templates |
| `redmine-ticket-closure` | find, preview and draft closures, hand over for approval, verify |
| `redmine-rest-api` | read-only access to any REST endpoint |

Always call the harness through the launcher, from the repository root:
`python harness.py <command> [--json]`. Use `python3` or `py` if needed. It picks `.venv` by itself.

What you may run, by permission level:

- **Read freely:** `check`, `my-tickets`, `ticket`, `search`, `api` (GET only), `overview`, `sla`,
  `team`, `performance`, `time`, `close candidates|list|show`, `templates list|show|preview|validate`,
  `approver list`, `audit`, `skills list|validate`.
- **Local outputs** (nothing changes in Redmine): `export` writes CSV files.
  `daily-overview` without `--send` saves an e-mail preview.
- **Only when the user asks in this conversation:** `close draft`, `close auto-draft`, `close cancel`,
  `templates create|update` (show the content first), and `daily-overview --send` (confirm the
  recipients first).
- **Never** (for the person only, in their terminal or the dashboard): `close approve`, `close review`,
  `close reject`, `close execute`, `templates delete`, `approver add|remove|change-pin`.
  - Never work around the interactive-terminal or PIN checks. That means no `script`, `expect`,
    `unbuffer`, pseudo-terminals, piping answers, or editing `data/`.
  - Never ask for, accept or store an approver PIN.
  - Never call the Redmine API directly (curl or similar) to change data.
- Never print, log or commit secrets from `.env` or `config/settings.yaml`. Ask the user to edit `.env`
  themselves; do not ask them to paste keys into the chat.
- Never claim a ticket was closed unless `close show <id> --json` reports `"status": "executed"`.

## Changing the code

- **Never** add a code path that writes to Redmine without going through `WriteGuard.authorize`.
- **Never** give an LLM-facing tool (`redmine_agent/agent/tools.py`, `agent/mcp_server.py`) or a skill
  the ability to approve proposals.
- New agent tools must declare a level (`read`, `draft` or `write`) and be listed in `agent/agent.yaml`.
  `tests/test_agent_tools.py` checks that the manifest and the code agree.
- Skills: edit `.agents/skills/`, then run `python harness.py skills sync` and
  `python harness.py skills validate`. `tests/test_skills.py` checks three things:
  - the mirror is in sync;
  - every command in a skill parses;
  - the permission rules stay in place.
- Keep `tests/test_guard.py`, `tests/test_approvals.py` and `tests/test_approvers.py` green. They are the
  safety net.

## Project map

```
harness.py             portable launcher (finds .venv; works from any folder) - used by the skills
redmine_agent/
  config.py            settings: YAML + ${ENV} interpolation + env overrides
  client.py            Redmine REST client (auth, pagination, retries, error hints)
  guard.py             WriteGuard + Approval (permission enforcement)
  approvers.py         registered approvers with salted-hash PINs
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
  skills.py            skills validation, .claude mirror sync, personal installs
  cli.py               command-line interface (--json everywhere)
  web/                 FastAPI dashboard (Jinja templates, Chart.js vendored)
  agent/               LLM tools, OpenAI-compatible runtime, MCP server
  mock/                mock Redmine REST API (demos & tests)
.agents/skills/        Agent Skills (canonical)      .claude/skills/   mirror for Claude Code
.claude/settings.json  Claude Code permission rules  CLAUDE.md, GEMINI.md  import this file
agent/                 manifest + system prompt for the built-in chat / MCP agent
config/                example settings + starter closure templates
docs/                  architecture, REST API notes, permissions, scheduling, agent skills
tests/                 pytest suite (runs against the mock server)
```

## Dev workflow

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt   # Windows: py -m venv .venv; .venv\Scripts\pip ...
.venv/bin/python -m pytest                                               # all tests use the built-in mock Redmine
python harness.py mock-server --port 3000                                # demo Redmine (API key: demo-api-key)
```

Conventions:
- Python ≥ 3.10, with type hints.
- No new heavy dependencies without a reason.
- CLI output stays ASCII-only so it works in Windows consoles.
- Follow the Redmine REST API conventions in `docs/redmine-rest-api.md`.
