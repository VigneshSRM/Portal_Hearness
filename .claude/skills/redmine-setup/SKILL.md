---
name: redmine-setup
description: Install, configure and troubleshoot the Portal Harness for Redmine - create the virtualenv, .env and config/settings.yaml, set the Redmine URL and API key, SMTP e-mail and dashboard login, register an approver PIN, verify the connection with `check`, start the web dashboard, or try a demo with the built-in mock Redmine. Use when the user wants to connect or configure the Redmine agent, or when any harness command fails with a configuration, credentials, 401/403, SSL, proxy or connection error.
license: MIT
compatibility: Needs Python 3.10+ and network access to the Redmine server. Sandboxed agents (e.g. Codex CLI) must allow network access for shell commands.
metadata:
  author: portal-harness
  version: "1.1"
---

# Redmine harness: setup and troubleshooting

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py --version
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher uses the project's
  virtualenv by itself - never activate it and never install packages globally.
- Add `--json` for machine-readable output, then summarise it for the user.
- Exit codes: `0` ok, `1` checks failed, `2` error, `3` permission denied, `4` harness not installed.

## Quick health check

```bash
python harness.py check --json
```

`ok: true` means Redmine answered with the configured credentials. Read `checks[]` for anything that
failed and `config` for what is set (the API key is masked - never try to read it yourself).

## First-time setup

Go step by step and say what you are about to do before each step that writes files.

1. **Install** (only if a command exits with code 4 "not installed yet"):
   - macOS/Linux: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
   - Windows: `py -m venv .venv`, then `.venv\Scripts\pip install -r requirements.txt`
2. **Create the config files** (never overwrites existing ones):
   ```bash
   python harness.py init
   ```
3. **Credentials** - ask the user to open `.env` in their editor and fill in:
   - `REDMINE_URL` - the Redmine root, e.g. `https://redmine.example.com`
   - `REDMINE_API_KEY` - Redmine → *My account* → *API access key* → *Show*
   - `REDMINE_READ_ONLY=true` is a safe start; `false` lets approved closures apply.

   Do **not** ask for the API key in this chat - it would be sent to the AI provider. If the user pastes
   it anyway, write it only into `.env` and never repeat it.
4. **REST API switch** - a Redmine admin must tick *Administration → Settings → API → Enable REST web service*.
5. **Verify**: `python harness.py check`
6. **Approver PIN** - strongly recommended whenever an AI agent uses the harness. Ask the user to run this
   **in their own terminal window** (you cannot and must not run it):
   `python harness.py approver add "<their name>"`
   From then on every approval needs their PIN, which you never see. Check with
   `python harness.py approver list --json`.
7. Optional:
   - Daily e-mail: `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`starttls`, `ssl` or `none`), `SMTP_USERNAME`,
     `SMTP_PASSWORD`, `SMTP_SENDER`, `DAILY_RECIPIENTS`, `EMAIL_ENABLED=true` (see `redmine-daily-overview`).
   - Dashboard login when it is shared on a network: `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD`.
   - SLA rules, business hours, holidays, team, closure rules, report presets: `config/settings.yaml`
     ([references/settings.md](references/settings.md)).

## Web dashboard

`python harness.py dashboard` serves http://localhost:8000 and keeps running. Start it as a background
process if your tool supports that; otherwise ask the user to run it in another terminal. People review
and approve ticket closures on its **Approvals** page.

## Try it without a real Redmine

`python harness.py mock-server --port 3000` starts a demo Redmine (keeps running). Then set
`REDMINE_URL=http://127.0.0.1:3000` and `REDMINE_API_KEY=demo-api-key` in `.env`. The demo user is
Karthik Raja; the data is realistic but fake.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Exit code 4, "not installed yet" | No virtualenv | Step 1 |
| `No Redmine credentials configured` | `.env` incomplete | Step 3 |
| `401` | Wrong key, or the REST API is off | New key; step 4 |
| `403` on one call | The account's role lacks that permission (`/users.json` is admin-only) | Use a suitable account; normal use does not need admin |
| `Could not reach Redmine` | Wrong URL, VPN/proxy, or the agent sandbox blocks network | Check URL and VPN; set `HTTPS_PROXY`; Codex: see below |
| `TLS/SSL verification failed` | Company certificate authority | `redmine.ca_bundle: config/company-ca.pem` in `config/settings.yaml` |
| `Expected JSON ... got text/html` | URL points at a login page or proxy | Use the Redmine root URL |
| `412` | `REDMINE_IMPERSONATE` login unknown | Fix or remove it |

## Notes per AI tool

- **Codex CLI** runs shell commands in a sandbox without network by default. Start it with
  `codex --config sandbox_workspace_write.network_access=true`, or add to `~/.codex/config.toml`:
  ```toml
  [sandbox_workspace_write]
  network_access = true
  ```
- **Claude Code**: this repo's `.claude/settings.json` pre-approves the read-only harness commands, asks
  before drafting or e-mailing, and blocks approval commands and reading `.env`.
- **Gemini CLI, GitHub Copilot CLI, Cursor, OpenCode**: skills load from `.agents/skills`; confirm shell
  commands when prompted.
- Personal install (use the skills from any folder): `python harness.py skills install --tool all`.

## Permission rules

- Setup changes local files only. Never print, repeat or commit secrets from `.env`.
- Never run `approver add/remove/change-pin`, `close approve`, `close review`, `close reject`,
  `close execute` or `templates delete` - those are for the person, in their own terminal or the dashboard.
- Never ask the user for an approver PIN, and never work around the harness's interactive-terminal or
  PIN checks (no `script`, `expect`, `unbuffer`, pseudo-terminals, or editing files in `data/`).
