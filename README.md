# Portal Harness: Redmine Ticketing Automation Agent

A **permission-first** automation harness and AI agent for the [Redmine](https://www.redmine.org)
ticketing system. It connects through the official
[Redmine REST API](https://www.redmine.org/projects/redmine/wiki/Rest_api).

| The agent **can** | The agent **cannot** |
|---|---|
| ✉️ Send a **daily overview e-mail** built from Redmine data | ❌ **Change anything without your permission.** Every ticket update needs a recorded human approval that is bound to the exact change. |
| 📊 Show **dashboards**: Overview, **SLA**, **Team**, **Performance**, **Time spent** | ❌ Approve its own proposals. No agent or MCP tool can approve. |
| 📁 Export **customised reports to CSV**: datasets, filters, columns, group-by, presets | ❌ Delete tickets, time entries, projects or users. No such capability exists. |
| 🙋 List the **tickets assigned to me**, sorted by urgency and SLA | ❌ Write anything when `REDMINE_READ_ONLY=true` |
| 📝 Manage **closure templates** (variables, target status, % done, custom fields) | |
| ✅ **Automate ticket closing**: find candidates, draft, **you review and approve**, execute, verify | |
| 🤖 Run from **your AI terminal subscription** (Claude Code, Codex CLI, Gemini CLI, Copilot CLI) through **agent skills** | |

---

## Quick start: a 2-minute demo with no Redmine needed

The repo includes a mock Redmine server with realistic data: about 350 tickets, 7 people,
5 projects, SLAs and time entries.

```bash
git clone https://github.com/VigneshSRM/Portal_Hearness.git && cd Portal_Hearness
python -m venv .venv && source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python harness.py mock-server --port 3000            # terminal 1
```

```bash
# terminal 2  (Windows PowerShell: $env:REDMINE_URL="http://127.0.0.1:3000"; $env:REDMINE_API_KEY="demo-api-key")
export REDMINE_URL=http://127.0.0.1:3000 REDMINE_API_KEY=demo-api-key
python harness.py check          # connectivity report
python harness.py my-tickets     # tickets assigned to me
python harness.py dashboard      # http://localhost:8000
```

## Connect it to your Redmine

1. **Enable the REST API** (needs an admin): *Administration → Settings → API → Enable REST web
   service*.
2. **Get your API key**: *My account → API access key → Show*.
3. **Configure the harness:**

   ```bash
   python harness.py init      # creates .env and config/settings.yaml from the examples
   ```

   Edit `.env`:

   ```ini
   REDMINE_URL=https://redmine.your-company.com
   REDMINE_API_KEY=your-api-key
   REDMINE_READ_ONLY=true            # recommended while you evaluate; set to false to allow approved closures
   SMTP_HOST=smtp.office365.com
   SMTP_USERNAME=...
   SMTP_PASSWORD=...
   SMTP_SENDER=redmine-agent@your-company.com
   DAILY_RECIPIENTS=you@your-company.com,lead@your-company.com
   ```

4. **Verify** with `python harness.py check`. It shows your user, statuses, trackers,
   projects and access to time tracking.
5. **Tune** `config/settings.yaml`: SLA rules and business hours, holidays, team members, closure
   rules and report presets. Every option is commented.

---

## Features

### 1. Daily overview e-mail

```bash
python harness.py daily-overview              # preview -> data/outbox/daily-overview-*.html
python harness.py daily-overview --send       # send now via SMTP
python harness.py schedule                    # every weekday at email.send_time (default 09:00 IST)
```

The e-mail contains:

- KPIs
- SLA breaches and tickets at risk
- New and closed tickets from the last 24 h
- Overdue tickets
- My tickets
- Team workload
- Pending approvals

A CSV of open tickets with their SLA state is attached. For cron, systemd, Windows Task
Scheduler and Docker setups, see [docs/scheduling.md](docs/scheduling.md).

### 2. Dashboards: overview, SLA, team, performance, time spent

```bash
python harness.py dashboard      # http://localhost:8000
```

| Page | What you get |
|---|---|
| **Overview** | Open, created and closed counts; SLA compliance; breached and at-risk tickets; overdue and unassigned tickets; created-vs-closed trend; ticket age; breakdowns by status, priority and assignee |
| **SLA** | Resolution and first-response compliance; results by priority; open breaches and at-risk tickets; tickets closed late; the active rules. Uses business hours, holidays and timezone. |
| **Team** | Per person: open, high-priority, overdue, breached, closed, average resolution, SLA %, hours logged, utilisation against capacity |
| **Performance** | Throughput; weekly trend; average, median and P90 resolution; resolution distribution; first response; reopen rate; estimate accuracy |
| **Time spent** | Hours by person, project, activity and tracker; daily trend; most time-consuming tickets; tickets over estimate |
| **My tickets** | Your tickets sorted by SLA urgency, with a one-click **Draft closure** button |
| **Reports & CSV** | The report builder (see below) |
| **Closure templates** | Create, edit, preview and delete templates |
| **Approvals** | The review queue: edit the note, then approve, reject or execute. Also closure candidates and history. |
| **Audit log** | Every draft, approval, execution, denied attempt, export and e-mail |

Every page has period and project filters, CSV export and a JSON API (`/api/docs`). The same
numbers are available from the terminal with `overview`, `sla`, `team`, `performance` and `time`
(add `--json` for machine-readable output).

### 3. Custom CSV reports

You can build reports in the dashboard (*Reports & CSV*) or on the command line:

```bash
python harness.py export --list-columns --dataset issues
python harness.py export --dataset sla --days 7 --date-field closed_on --status closed
python harness.py export --dataset issues --date-field open_now --status open --group-by assigned_to,priority
python harness.py export --dataset time_entries --from 2026-10-01 --to 2026-10-31 --group-by user,project
python harness.py export --preset weekly-sla --out weekly.csv
```

- **Datasets:** `issues`, `sla` (tickets with SLA details), `time_entries`, `team`.
- **Filters:** project, status, assignee, tracker, priority, SLA state and subject text.
- **Columns:** any built-in column, plus your Redmine custom fields (`cf_<id>`).
- **Group-by:** gives totals such as count, open/closed, SLA %, average resolution, hours and
  estimates.
- **Presets:** save reusable reports under `reports.presets` in `settings.yaml`.

### 4. Tickets assigned to me

```bash
python harness.py my-tickets                 # open, most urgent first
python harness.py my-tickets --status all --csv my-tickets.csv
```

### 5. Closure templates

Templates live in `data/closure_templates.yaml`. On first run it is seeded from
`config/closure_templates.yaml`, which ships these templates:

- standard resolution
- bug fixed
- support completed
- incident with root cause
- no response
- duplicate

Each template defines the closing note, target status, % done, optional private note, custom
fields to set (e.g. `Resolution: Fixed`), variables for the reviewer to fill in, and the trackers
it is suggested for.

```bash
python harness.py templates list
python harness.py templates preview bug-fixed 1234 --var release="v2.4.1"
python harness.py templates create --file my-template.yaml
```

Notes are Jinja2 running in a sandbox. Example:

```jinja
Hi {{ issue.author }}, ticket #{{ issue.id }} "{{ issue.subject }}" is fixed: {{ fix_summary }}. Regards, {{ me.name }}
```

### 6. Ticket closing with review and approval

```
find candidates ──► draft proposal ──► YOU review (edit / approve / reject) ──► execute ──► verify ──► audit
 (Resolved, idle    (template note,       nothing changes in Redmine          (PUT /issues/:id.json,
  ≥ 3 days)          workflow-checked)     until you approve                   re-read to confirm)
```

```bash
python harness.py close candidates
python harness.py close draft 1234 --template bug-fixed --var release="v2.4.1"
python harness.py close auto-draft            # draft all candidates (the scheduler does this daily)
python harness.py close review                # interactive: approve & close / edit / reject / skip
```

You can also review on the dashboard's **Approvals** page. Built-in safety checks:

- Only a human can approve. With `python harness.py approver add "Your Name"`, every approval also needs
  your PIN, which an AI agent never sees.
- The approval covers the exact payload; editing it requires re-approval.
- Tickets that changed since drafting become *stale*.
- Redmine's workflow is checked before review, and the result is verified afterwards.

Full details are in [docs/permissions-and-approvals.md](docs/permissions-and-approvals.md).

---

## Use it from your AI terminal (agent skills)

Already using **Claude Code**, **Codex CLI**, **Gemini CLI** or **GitHub Copilot CLI** on a subscription?
Open it in this folder and it can operate the harness for you. No extra API key is needed.

The repo ships eight skills in the open [Agent Skills](https://agentskills.io) format:

- `.agents/skills/` is read by Codex, Gemini CLI, Copilot CLI, Cursor and OpenCode.
- `.claude/skills/` is the same set for Claude Code.

| Skill | Ask things like |
|---|---|
| `redmine-setup` | "connect the harness to our Redmine", "why does check fail?" |
| `redmine-my-tickets` | "what should I work on next?", "show #1234" |
| `redmine-dashboards` | "how is our SLA this month?", "team workload" |
| `redmine-reports` | "export last week's SLA report as CSV" |
| `redmine-daily-overview` | "give me today's overview", "send it to the team" |
| `redmine-closure-templates` | "create a template for duplicate tickets" |
| `redmine-ticket-closure` | "close my resolved tickets" → drafts proposals **you** approve |
| `redmine-rest-api` | "list the versions of the infra project" (read-only API) |

```bash
claude                                                     # Claude Code    -> /redmine-my-tickets
codex --config sandbox_workspace_write.network_access=true # Codex CLI      -> $redmine-my-tickets
gemini                                                     # Gemini CLI     -> /skills list
copilot                                                    # Copilot CLI    -> /skills list
python harness.py skills install --tool all                # optional: use the skills from any folder
```

The agent can read, export and draft. **Approving a ticket change is yours alone**: use the dashboard,
or `python harness.py close review` in your own terminal, plus your approver PIN if you registered one.

Full guide: [docs/agent-skills.md](docs/agent-skills.md).

## Other agent runtimes

The agent definition lives in [`agent/`](agent/):

- [`agent.yaml`](agent/agent.yaml): capabilities, restrictions and the tool list with permission
  levels.
- [`system_prompt.md`](agent/system_prompt.md): the agent's instructions.

Besides the skills above, there are three ways to use it.

**a) Built-in terminal chat with any OpenAI-compatible LLM API** (OpenAI, Azure OpenAI, Ollama, vLLM,
LM Studio, OpenRouter, and others):

```bash
# .env: LLM_BASE_URL=https://api.openai.com/v1  LLM_API_KEY=...  LLM_MODEL=gpt-4o-mini
# local alternative: LLM_BASE_URL=http://localhost:11434/v1  LLM_MODEL=llama3.1
python harness.py chat
you> which of my tickets are about to breach SLA?
you> close #1234 with the bug-fixed template, release v2.4.1
     ... APPROVAL REQUIRED - the agent wants to make a change: (exact diff shown) ... type 'yes'
```

**b) MCP server** for Claude Desktop or Claude Code, VS Code agent mode, Cursor and Windsurf.
`.mcp.json` is included:

```bash
python harness.py mcp
```

Over MCP the agent can read and draft, but it **cannot approve**. You approve in the dashboard,
and then the agent may call `execute_approved_proposal`.

**c) Coding agents working on this repo** follow [`AGENTS.md`](AGENTS.md).

| Level | Tools |
|---|---|
| read | `whoami`, `list_my_tickets`, `get_ticket`, `search_tickets`, `dashboard_summary`, `list_closure_templates`, `preview_closure_note`, `find_closure_candidates`, `list_closure_proposals`, `get_proposal`, `preview_daily_overview`, `list_report_columns` |
| draft | `draft_closure_proposal`, `cancel_proposal`, `export_report_csv` |
| write (**human approval per call**) | `close_ticket`, `create_closure_template`, `update_closure_template`, `delete_closure_template`, `send_daily_overview` |

---

## Project layout

```
harness.py              portable launcher used by humans and skills (finds .venv, works from any folder)
redmine_agent/          Python package (CLI, REST client, guard, analytics, reports, workflow, web, agent, mock)
.agents/skills/         agent skills (Codex, Gemini CLI, Copilot CLI, Cursor, OpenCode...)
.claude/skills/         the same skills for Claude Code (+ .claude/settings.json permission rules)
CLAUDE.md, GEMINI.md    point Claude Code / Gemini CLI at AGENTS.md
agent/                  agent manifest + system prompt
config/                 settings.example.yaml, closure_templates.yaml (starter templates)
docs/                   architecture, REST API notes, permissions & approvals, scheduling, agent skills
tests/                  pytest suite - runs against the built-in mock Redmine
AGENTS.md               rules for AI agents working in this repo
.mcp.json               MCP client configuration
Dockerfile, docker-compose.yml
```

Runtime state is kept in `data/`, which git ignores. It holds the approval queue and audit log
(`harness.db`), the editable templates, e-mail previews (`outbox/`) and CSV exports (`exports/`).

## Development

```bash
pip install -r requirements-dev.txt
pytest                      # ~190 tests, all against the built-in mock Redmine - no network needed
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `401` | Wrong API key, or the REST API is not enabled in Redmine |
| `403` | Your account's role lacks the permission. `/users.json` is admin-only; the harness does not need it. |
| SSL errors | Set `redmine.ca_bundle: config/company-ca.pem` |
| No first-response SLA | Needs journals: keep `sla.track_first_response: true` |
| Closure "failed: status unchanged" | Redmine's workflow does not let your role set that status. Choose another target status or ask an admin. |
| Windows console garbles characters | Output is ASCII-only; run in Windows Terminal or set `PYTHONIOENCODING=utf-8` |

## License

MIT. Chart.js is vendored under its own MIT licence (`redmine_agent/web/static/vendor/CHARTJS_LICENSE.md`).
