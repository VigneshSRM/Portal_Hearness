# Portal Harness - Redmine Ticketing Agent: Complete Guide

Portal Harness is an automation **agent for the [Redmine](https://www.redmine.org) ticketing system**.
It connects through the official [Redmine REST API](https://www.redmine.org/projects/redmine/wiki/Rest_api).
You can use it from your **AI terminal** (Claude Code, Codex CLI, Gemini CLI, GitHub Copilot CLI), from
a **web dashboard**, or from the **command line**.

| The agent **can** | The agent **cannot** |
|---|---|
| ✉️ Send a **daily overview e-mail** built from Redmine data | ❌ **Change anything without your permission.** Every ticket change needs a person's recorded approval. |
| 📊 Show **dashboards**: overview, **SLA**, **team**, **performance**, **time spent** | ❌ Approve its own proposals. No AI tool or skill can approve. |
| 📁 Export **customised reports to CSV** | ❌ Delete tickets, time entries, projects or users. No such capability exists. |
| 🙋 List the **tickets assigned to you**, most urgent first | ❌ Write anything while `REDMINE_READ_ONLY=true` |
| 📝 Create and manage **ticket-closure templates** | |
| ✅ **Close tickets**: it prepares the closure, **you approve**, then it applies and verifies | |

---

## Contents

1. [Quick start (10 minutes)](#1-quick-start-10-minutes)
2. [How it works](#2-how-it-works)
3. [Requirements](#3-requirements)
4. [Install](#4-install)
5. [Configure: where everything goes](#5-configure-where-everything-goes)
   - [5.1 Create the configuration files](#51-create-the-configuration-files)
   - [5.2 Redmine API connection](#52-redmine-api-connection)
   - [5.3 Projects and speed (local copy)](#53-projects-and-speed-local-copy)
   - [5.4 E-mail / SMTP settings](#54-e-mail--smtp-settings)
   - [5.5 Web dashboard settings](#55-web-dashboard-settings)
   - [5.6 Team policies (SLA, closure rules, team, reports)](#56-team-policies-sla-closure-rules-team-reports)
   - [5.7 All settings at a glance](#57-all-settings-at-a-glance)
6. [Register yourself as an approver (PIN)](#6-register-yourself-as-an-approver-pin)
7. [Use the agent from your AI terminal](#7-use-the-agent-from-your-ai-terminal)
8. [Use the web dashboard](#8-use-the-web-dashboard)
9. [Daily overview e-mail and scheduling](#9-daily-overview-e-mail-and-scheduling)
10. [Closing tickets safely (approval workflow)](#10-closing-tickets-safely-approval-workflow)
11. [Closure templates](#11-closure-templates)
12. [Reports and CSV export](#12-reports-and-csv-export)
13. [Command cheat sheet](#13-command-cheat-sheet)
14. [Security and permissions](#14-security-and-permissions)
15. [Try it without a real Redmine (demo mode)](#15-try-it-without-a-real-redmine-demo-mode)
16. [Run it on a server](#16-run-it-on-a-server)
17. [Troubleshooting](#17-troubleshooting)
18. [FAQ](#18-faq)
19. [Project layout and further reading](#19-project-layout-and-further-reading)

> **About `python`:** commands in this guide start with `python harness.py`. If `python` is not found,
> use `python3` on macOS/Linux or `py` on Windows. The `harness.py` launcher finds the project's
> virtualenv by itself, so you never need to "activate" anything.

---

## 1. Quick start (10 minutes)

```bash
# 1. Get the code and install it (Windows: see section 4)
git clone https://github.com/VigneshSRM/Portal_Hearness.git
cd Portal_Hearness
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. Create your settings files
python harness.py init
```

3. Open **`.env`** in a text editor and fill in at least `REDMINE_URL` and `REDMINE_API_KEY`
   ([section 5.2](#52-redmine-api-connection)). On a big Redmine, also set `REDMINE_PROJECTS` to your
   team's projects ([section 5.3](#53-projects-and-speed-local-copy)).
4. Check the connection, register yourself as approver, and open the dashboard:

   ```bash
   python harness.py check
   python harness.py approver add "Your Name"
   python harness.py dashboard
   ```

   The dashboard is at http://localhost:8000. The first time, it downloads your tickets once (with a
   progress bar); after that every page opens instantly.
5. Optional: open your AI terminal **in this folder** (`claude`, `codex`, `gemini` or `copilot`) and ask:
   *"What are my most urgent tickets?"* ([section 7](#7-use-the-agent-from-your-ai-terminal)).

No Redmine to test with yet? Use the [demo mode](#15-try-it-without-a-real-redmine-demo-mode).

---

## 2. How it works

```
 YOU ─┬─ AI terminal (Claude Code / Codex / Gemini CLI / Copilot CLI) ─┐  uses the skills in
      │                                                                │  .agents/skills, .claude/skills
      ├─ Web dashboard   python harness.py dashboard                   │
      ├─ Command line    python harness.py <command>                   ├──► the harness ──► Redmine REST API
      └─ Scheduler       python harness.py schedule                    ┘         │          (your API key)
                                                                               ├──► SMTP server (daily e-mail)
                                                                               └──► data/ (local copy of Redmine,
                                                                                    approval queue, audit log,
                                                                                    templates, CSVs)
```

Pages never wait for Redmine: they are built from a **local copy** of your tickets that a background
thread keeps up to date, fetching only what changed ([section 5.3](#53-projects-and-speed-local-copy)).

There are three permission levels:

| Level | Examples | Who |
|---|---|---|
| **Read** | My tickets, dashboards, SLA, previews, reports | Anyone, any time |
| **Draft** | Prepare a closure proposal or a template, export a CSV | The AI agent, when you ask |
| **Approve** | Actually close a ticket in Redmine | **Only you**: in the dashboard, or in your own terminal with your PIN |

Where things are stored:

| File / folder | What it holds | Committed to git? |
|---|---|---|
| `.env` | **Your secrets and personal settings**: Redmine URL and API key, SMTP login, dashboard login | **Never** (git-ignored) |
| `config/settings.yaml` | **Team policies**: SLA targets, business hours, holidays, closure rules, team, report presets | No (git-ignored); the template is `config/settings.example.yaml` |
| `data/` | Local copy of Redmine (`redmine_cache.db`, safe to delete), approval queue and audit log (`harness.db`), closure templates, approver PINs (hashed), e-mail previews (`outbox/`), CSV exports (`exports/`) | Never |
| `.agents/skills/`, `.claude/skills/` | The agent skills your AI terminal reads | Yes |

---

## 3. Requirements

| Need | Details |
|---|---|
| **Python 3.10 or newer** | `python3 --version` (Windows: `py --version`) |
| **Git** | To download and update the code |
| **Redmine 4.1 or newer** | 5.0+ recommended: it lets the harness check workflow rules before you review |
| **REST API enabled in Redmine** | An admin ticks *Administration → Settings → API → Enable REST web service* |
| **Your Redmine API key** | *My account → API access key → Show* |
| **Network access** | From this computer to Redmine (and to your mail server for e-mails) |
| Optional: **SMTP account or relay** | For the daily overview e-mail |
| Optional: **AI terminal tool** | Claude Code, Codex CLI, Gemini CLI or GitHub Copilot CLI (your existing subscription) |

---

## 4. Install

**macOS / Linux**

```bash
git clone https://github.com/VigneshSRM/Portal_Hearness.git
cd Portal_Hearness
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
python3 harness.py --version
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/VigneshSRM/Portal_Hearness.git
cd Portal_Hearness
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
py harness.py --version
```

If you see `redmine-agent 1.0.0`, the install worked. If you see *"not installed yet"*, run the
`venv` and `pip` lines again.

**Updating later:** run `git pull`, then `.venv/bin/pip install -r requirements.txt` (Windows:
`.venv\Scripts\pip ...`), then restart the dashboard and scheduler.

---

## 5. Configure: where everything goes

### 5.1 Create the configuration files

```bash
python harness.py init
```

This creates two files. It never overwrites files that already exist.

| File | Edit it for |
|---|---|
| **`.env`** | Redmine URL and **API key**, **SMTP** e-mail server and login, recipients, dashboard login |
| **`config/settings.yaml`** | SLA rules, business hours, holidays, closure rules, team, report presets |

Settings are applied in this order: real environment variables first, then `.env`, then
`config/settings.yaml`. After any change, run `python harness.py check`.

### 5.2 Redmine API connection

**Step 1. Enable the REST API (once, by a Redmine admin).**
In Redmine, go to *Administration → Settings → API*, tick **Enable REST web service**, then click
**Save**.

**Step 2. Get your API key.**
Click **My account** (top right). In the right sidebar, under **API access key**, click **Show** and
copy the key.

**Step 3. Put the URL and key in `.env`.**

```ini
# .env
REDMINE_URL=https://redmine.yourcompany.com     # the Redmine root, without /issues at the end
REDMINE_API_KEY=paste-your-40-character-key-here
REDMINE_READ_ONLY=true                           # safe start: nothing can be changed yet
REDMINE_PROJECTS=service-desk,customer-portal    # optional: only these projects (empty = all)
```

- **"My tickets" means the tickets assigned to the owner of this API key.** Use your own key. For a
  shared team bot, create a dedicated Redmine user and use that user's key.
- **`REDMINE_READ_ONLY=true`** blocks every change, even approved ones. It is the default in a new
  `.env`. Set it to `false` when you want approved closures to be applied.
- **Never paste the API key into an AI chat.** The harness reads it from `.env` itself, and the agent
  never needs to see it.
- **`REDMINE_PROJECTS`** limits everything to your team's projects. See the next section.

**Step 4. Check the connection.**

```bash
python harness.py check
```

Expected output (shortened):

```
Redmine connectivity
  [ok  ] current user: your.login (id 42, admin=False)
  [ok  ] issue statuses: New, In Progress, Resolved, Feedback, Closed*, Rejected*  (* = closed)
  [ok  ] projects: 12 visible
All good.
```

Special cases:

| Situation | Add to `.env` |
|---|---|
| Your company uses its own HTTPS certificate | `REDMINE_CA_BUNDLE=config/company-ca.pem` (put the PEM file there) |
| You are behind a proxy | `HTTPS_PROXY=http://proxy.yourcompany.com:8080` |
| People open Redmine at a different address than this machine uses (Docker, internal DNS) | `REDMINE_PUBLIC_URL=https://redmine.yourcompany.com` |
| You must log in with a username and password instead of a key | `REDMINE_USERNAME=...` and `REDMINE_PASSWORD=...` |

### 5.3 Projects and speed (local copy)

The dashboard does **not** ask Redmine for data when you open a page. It keeps a **local copy** of your
tickets in `data/redmine_cache.db` and builds every page from it, so pages open in milliseconds even
when Redmine is slow or far away.

How the copy stays up to date:

| When | What is downloaded |
|---|---|
| **First start** (once) | Every open ticket, every ticket updated in the last `SYNC_HISTORY_DAYS` days, their time entries, and statuses/projects/users. The dashboard shows a progress bar meanwhile. |
| **Every `SYNC_INTERVAL_MINUTES`** (default 5) | Only the tickets that changed since the last update, plus today's and yesterday's time entries. Usually a handful of requests. |
| **Every 12 hours** | Everything again, which also removes deleted or moved tickets. |
| **In between** | Ticket histories for the first-response SLA, newest tickets first. |

Settings, in **`.env`**:

```ini
# .env
REDMINE_PROJECTS=service-desk,customer-portal   # only these projects (ids or identifiers, comma-separated)
SYNC_INTERVAL_MINUTES=5                         # how often changes are fetched in the background
SYNC_HISTORY_DAYS=180                           # how far back the copy goes (open tickets: always)
```

- **`REDMINE_PROJECTS`** is the most important setting for a big Redmine. Only these projects (and
  their sub-projects) are downloaded and shown, so the first download is much smaller. Use the short
  name from the project's web address (`/projects/<identifier>`) or its number. Empty means every project
  you can see.
- **`SYNC_HISTORY_DAYS`** decides how far back the copy goes. Choosing a longer dashboard period than this
  shows a note that older closed tickets are missing.
- The **header** shows when the data was last updated, for example *Updated 2 min ago*. The **↻** button
  fetches the latest changes right away. If Redmine can't be reached, pages keep working from the copy and
  a red note says so.
- From the terminal, `python harness.py sync` updates the copy now and shows progress. Use
  `python harness.py sync --full` to download everything again, and `python harness.py sync --status` to
  see what the copy holds.
- The command line, the daily e-mail and AI tools use the same copy. They fetch the latest changes first
  if it is more than a minute old.
- Deleting `data/redmine_cache.db` is safe. It only causes a new first download.

### 5.4 E-mail / SMTP settings

The daily overview e-mail is sent through your mail server (SMTP). Put these in `.env`:

```ini
# .env
EMAIL_ENABLED=false                    # true = the scheduler really sends the daily e-mail
SMTP_HOST=smtp.office365.com           # your mail server
SMTP_PORT=587
SMTP_SECURITY=starttls                 # starttls | ssl | none
SMTP_USERNAME=redmine-agent@yourcompany.com
SMTP_PASSWORD=your-smtp-password
SMTP_SENDER=redmine-agent@yourcompany.com      # the "From" address
DAILY_RECIPIENTS=you@yourcompany.com,lead@yourcompany.com
DAILY_SEND_TIME=09:00                  # 24h clock, in HARNESS_TIMEZONE (default Asia/Kolkata)
```

**Common mail providers:**

| Provider | `SMTP_HOST` | `SMTP_PORT` | `SMTP_SECURITY` | Login |
|---|---|---|---|---|
| Microsoft 365 / Office 365 | `smtp.office365.com` | `587` | `starttls` | Mailbox address and password. Authenticated SMTP must be enabled for that mailbox (see note) |
| Gmail / Google Workspace | `smtp.gmail.com` | `587` | `starttls` | Your address and a Google **App Password** (needs 2-Step Verification), not your normal password |
| Company relay | e.g. `relay.yourcompany.com` | `25` | `none` | Leave `SMTP_USERNAME` and `SMTP_PASSWORD` empty |
| SendGrid, Mailgun, Amazon SES, Brevo... | the provider's SMTP host | `587` | `starttls` | The SMTP username and password from the provider |
| Servers that only offer SSL | your host | `465` | `ssl` | As given by your mail admin |

> **Microsoft 365 note:** Microsoft is phasing out username/password ("Basic") authentication for
> `smtp.office365.com`. It becomes **disabled by default at the end of December 2026**, though admins
> can re-enable it, and it will be removed completely later
> ([Microsoft timeline](https://techcommunity.microsoft.com/blog/exchange/updated-exchange-online-smtp-auth-basic-authentication-deprecation-timeline/4489835)).
> The harness uses standard SMTP login, not OAuth. If you get
> `550 5.7.30 Basic authentication is not supported`, ask your admin to enable Authenticated SMTP for
> the sending mailbox. For the long term, use a company SMTP relay or an SMTP service from the table
> above.

**Test the e-mail step by step:**

```bash
python harness.py daily-overview                              # 1. preview only - nothing is sent
python harness.py daily-overview --send --to you@yourcompany.com   # 2. send one test e-mail to yourself
```

1. The preview is saved as an HTML file in `data/outbox/`. Open it in a browser.
2. If the test e-mail arrives, set `EMAIL_ENABLED=true` and start the scheduler
   ([section 9](#9-daily-overview-e-mail-and-scheduling)).

### 5.5 Web dashboard settings

```ini
# .env
DASHBOARD_HOST=127.0.0.1         # 127.0.0.1 = this computer only; 0.0.0.0 = everyone on your network
DASHBOARD_PORT=8000
DASHBOARD_USERNAME=              # set both to require a login (do this if you use 0.0.0.0)
DASHBOARD_PASSWORD=
# DASHBOARD_PUBLIC_URL=https://redmine-agent.yourcompany.com   # used for "Review now" links in e-mails
```

When a login is set, the login name is recorded as the reviewer on approvals. For use across the
network, put the dashboard behind an HTTPS reverse proxy (nginx, IIS, Caddy...).

### 5.6 Team policies (SLA, closure rules, team, reports)

Edit **`config/settings.yaml`**. Every option in the file is commented. The ones teams usually change:

```yaml
timezone: Asia/Kolkata

sla:
  rules:                                   # first matching rule wins
    - { priority: Immediate, response_hours: 1,  resolution_hours: 4 }
    - { priority: Urgent,    response_hours: 2,  resolution_hours: 8 }
    - { priority: High,      response_hours: 4,  resolution_hours: 24 }
    - { priority: Normal,    response_hours: 8,  resolution_hours: 72 }
    - { priority: Low,       response_hours: 24, resolution_hours: 120 }
    # - { name: Features, tracker: Feature, resolution_hours: 240 }   # match on tracker or project too
  at_risk_ratio: 0.8                       # "at risk" once 80% of the SLA time is used
  business_hours:
    start: "09:00"
    end: "18:00"
    workdays: [mon, tue, wed, thu, fri]
    holidays: ["2026-11-08", "2026-12-25"] # SLA clocks pause on these days

closure:
  scope: me                                # candidates: me (my tickets) | all (whole team)
  candidate_statuses: [Resolved]           # which statuses are "ready to close"
  idle_days: 3                             # ...after this many days without activity
  close_status: Closed
  template_rules:                          # choose a closure template per tracker
    - { tracker: Bug, template: bug-fixed }
    - { tracker: Support, template: support-resolved }

team:
  members: []                              # names or user ids for the Team page ([] = everyone)
  capacity_hours_per_day: 8

reports:
  presets:                                 # reusable reports: python harness.py export --preset weekly-sla
    weekly-sla: { title: Weekly SLA report, dataset: sla, days: 7, date_field: closed_on, status: closed }
```

### 5.7 All settings at a glance

Everything below goes in **`.env`**. Values in `config/settings.yaml` are the team policies shown above.

| Variable | Required? | What it does |
|---|---|---|
| `REDMINE_URL` | **Yes** | Redmine root URL |
| `REDMINE_API_KEY` | **Yes** (or username + password) | Your API key from *My account* |
| `REDMINE_READ_ONLY` | | `true` blocks every change, even approved ones |
| `REDMINE_PUBLIC_URL` | | Browser address used for ticket links, if it differs from `REDMINE_URL` |
| `REDMINE_USERNAME` | | Login instead of an API key (not recommended); use with `REDMINE_PASSWORD` |
| `REDMINE_PASSWORD` | | Password for `REDMINE_USERNAME` |
| `REDMINE_CA_BUNDLE` | | Company certificate authority file (PEM) |
| `REDMINE_VERIFY_SSL` | | `false` disables certificate checks (avoid; use `REDMINE_CA_BUNDLE`) |
| `REDMINE_PROJECTS` | Recommended for a big Redmine | Only these projects (ids or identifiers, comma-separated; sub-projects included) |
| `REDMINE_IMPERSONATE` | | Admin keys only: act as another user |
| `SYNC_INTERVAL_MINUTES` | | How often the local copy fetches changes from Redmine (default `5`) |
| `SYNC_HISTORY_DAYS` | | How far back the local copy goes (default `180`; open tickets are always included) |
| `HARNESS_TIMEZONE` | | Timezone for reports, SLA and scheduling (default `Asia/Kolkata`) |
| `HARNESS_DATA_DIR` | | Where local state is kept (default `data`) |
| `EMAIL_ENABLED` | | `true` lets the scheduler send the daily e-mail |
| `SMTP_HOST` | For e-mail | Mail server host |
| `SMTP_PORT` | For e-mail | Mail server port |
| `SMTP_SECURITY` | For e-mail | `starttls`, `ssl` or `none` |
| `SMTP_USERNAME` | For e-mail | Mail server login |
| `SMTP_PASSWORD` | For e-mail | Mail server password |
| `SMTP_SENDER` | For e-mail | From address |
| `DAILY_RECIPIENTS` | For e-mail | Comma-separated list of recipients |
| `DAILY_SEND_TIME` | | Send time, `HH:MM` |
| `DASHBOARD_HOST` | | Dashboard bind address |
| `DASHBOARD_PORT` | | Dashboard port |
| `DASHBOARD_USERNAME` | | Dashboard login name |
| `DASHBOARD_PASSWORD` | | Dashboard login password |
| `DASHBOARD_PUBLIC_URL` | | Dashboard link used in e-mails |
| `LLM_BASE_URL` | | Only for the built-in `chat` command ([section 7](#7-use-the-agent-from-your-ai-terminal)) |
| `LLM_API_KEY` | | Only for the built-in `chat` command |
| `LLM_MODEL` | | Only for the built-in `chat` command |

---

## 6. Register yourself as an approver (PIN)

Strongly recommended, especially if an AI agent uses the harness. Run this **in your own terminal**:

```bash
python harness.py approver add "Your Name"
```

You are asked to choose a PIN (at least 6 characters, typed hidden). From then on:

- **Every approval needs your PIN**, in the dashboard and in the terminal.
- The PIN is stored only as a salted hash in `data/approvers.json`. An AI agent never sees it.
- Five wrong attempts lock approvals for 15 minutes.
- To add a colleague, run `python harness.py approver add "Colleague Name"`. An existing approver must
  confirm with their PIN.
- Other commands: `python harness.py approver list`, `python harness.py approver change-pin "Your Name"`
  and `python harness.py approver remove "Name"`.

**Never type your PIN into an AI chat.** The agent is instructed never to ask for it.

---

## 7. Use the agent from your AI terminal

If you already pay for **Claude Code**, **Codex CLI**, **Gemini CLI** or **GitHub Copilot CLI**, that
tool *is* the agent. No extra API key is needed. The repository contains eight **skills** in the open
[Agent Skills](https://agentskills.io) format. A skill is a folder with a `SKILL.md` file that teaches
your AI tool how to operate the harness safely.

### 7.1 Start your tool in the project folder

| Tool | Start it with | Skills folder it reads | Call a skill directly |
|---|---|---|---|
| Claude Code | `claude` | `.claude/skills/` | `/redmine-my-tickets` |
| Codex CLI | `codex --config sandbox_workspace_write.network_access=true` | `.agents/skills/` | `$redmine-my-tickets` or `/skills` |
| Gemini CLI | `gemini` | `.agents/skills/` | `/skills list` |
| GitHub Copilot CLI | `copilot` | `.agents/skills/` | `/redmine-my-tickets` or `/skills list` |
| Cursor, OpenCode, others | as usual | `.agents/skills/` | found automatically by description |

- **Codex:** network access is off by default in Codex's sandbox, so without the flag above the harness
  cannot reach Redmine. To make it permanent, add this to `~/.codex/config.toml`:
  ```toml
  [sandbox_workspace_write]
  network_access = true
  ```
- **Claude Code:** the included `.claude/settings.json` does three things:
  - lets read-only harness commands run without prompts;
  - asks you before drafting, e-mailing or starting servers;
  - blocks approval commands and reading `.env`.
- **To use the skills from any folder**, install them for your user account:
  `python harness.py skills install --tool all`. Restart your AI tool afterwards.

### 7.2 What you can ask

| Skill | Example requests |
|---|---|
| `redmine-setup` | "Connect the harness to our Redmine", "Why does check fail with 401?" |
| `redmine-my-tickets` | "What are my most urgent tickets?", "Show me #1234", "Find tickets about VPN" |
| `redmine-dashboards` | "How is our SLA this month?", "Show team workload", "Hours logged in October" |
| `redmine-reports` | "Export last week's SLA report as CSV", "October timesheet by person and project" |
| `redmine-daily-overview` | "Give me today's overview", "Send the daily e-mail to the team" |
| `redmine-closure-templates` | "Create a template for duplicate tickets", "Preview bug-fixed for #1234" |
| `redmine-ticket-closure` | "Close my resolved tickets", "Close #1234 with the bug-fixed template" |
| `redmine-rest-api` | "List the versions of the infra project", "Who are the members of service-desk?" |

### 7.3 What a ticket closure looks like

> **You:** close my resolved tickets that have been idle
>
> **Agent:** *(finds 4 candidates and previews each closing note. It asks you for the root cause of
> the incident ticket instead of guessing, then drafts the proposals.)*
> "4 proposals are waiting for your approval. Approve them at http://localhost:8000/approvals, or run
> `python harness.py close review` in your own terminal. Nothing has changed in Redmine yet."
>
> **You:** *(approve in the dashboard with your PIN)* done
>
> **Agent:** *(checks)* "#341, #342, #343 and #344 are now Closed (verified in Redmine)."

The agent can **read, export and draft**. **Approving is always yours.** Even if the agent tried to
run an approval command, the harness would refuse it.

### 7.4 No AI subscription?

Everything also works without an AI tool, through the dashboard ([section 8](#8-use-the-web-dashboard))
and the command line ([section 13](#13-command-cheat-sheet)). There are two other agent options:

- **Built-in chat with any OpenAI-compatible API**, including a free local Ollama. Set `LLM_BASE_URL`,
  `LLM_API_KEY` and `LLM_MODEL` in `.env`, then run `python harness.py chat`. Every change asks you to
  approve it in the terminal.
- **MCP server** for Claude Desktop, VS Code, Cursor and others: `python harness.py mcp`. A ready-made
  `.mcp.json` is included. Over MCP the agent can read and draft, but never approve.

---

## 8. Use the web dashboard

```bash
python harness.py dashboard
```

Open http://localhost:8000. Every page has period and project filters and CSV export buttons. The
header shows when the data was last updated. The **↻** button fetches the latest changes from Redmine
without waiting for the next background update. Pages are built from the local copy, so they open
instantly ([section 5.3](#53-projects-and-speed-local-copy)).

| Page | What you see / do |
|---|---|
| **Overview** | Open, created and closed counts; SLA compliance; breached and at-risk tickets; overdue; unassigned; trends; ticket age |
| **SLA** | Compliance by priority, first response vs resolution, open breaches, tickets closed late, active rules |
| **Team** | Per person: open, high priority, overdue, breached, closed, average resolution, SLA %, hours, utilisation |
| **Performance** | Throughput, weekly trend, average / median / 90th-percentile resolution, reopen rate, estimate accuracy |
| **Time spent** | Hours by person, project, activity and tracker; daily trend; most time-consuming tickets |
| **My tickets** | Your tickets sorted by urgency, with a **Draft closure** button |
| **Reports & CSV** | Build a custom report, preview it, download the CSV |
| **Closure templates** | Create, edit, preview and delete templates |
| **Approvals** | Review queue: edit the note, **Approve & close**, reject. Also closure candidates and history |
| **Audit log** | Every draft, approval, closure, denied attempt, export and e-mail |

**To approve on the Approvals page:**
1. Enter your **name**. This is filled in automatically if a dashboard login is set.
2. Enter your **PIN**, if you registered one.
3. Check the change and the closing note (you can edit the note), then click **Approve & close ticket**.

---

## 9. Daily overview e-mail and scheduling

The e-mail contains:
- KPIs;
- SLA breaches and at-risk tickets;
- new and closed tickets from the last 24 hours;
- overdue tickets;
- your own tickets;
- team workload;
- pending approvals;
- a CSV attachment of open tickets.

| Task | Command |
|---|---|
| Preview (sends nothing) | `python harness.py daily-overview` |
| Preview and print the text | `python harness.py daily-overview --print` |
| Send now to the configured recipients | `python harness.py daily-overview --send` |
| Send now to someone specific | `python harness.py daily-overview --send --to lead@yourcompany.com` |

**Send it automatically every working day** (`EMAIL_ENABLED=true` must be set in `.env`):

```bash
python harness.py schedule                      # keeps running; sends at DAILY_SEND_TIME, Monday-Friday
python harness.py schedule --run-now --once     # test: run the daily job once, then stop
```

The daily job also **drafts** closure proposals for idle Resolved tickets. These are only drafts:
nothing is closed until you approve.

Instead of the built-in scheduler you can use **cron** (Linux/macOS), **Windows Task Scheduler**,
**systemd** or **Docker**. Ready-made examples are in [docs/scheduling.md](docs/scheduling.md). The
cron version is one line:

```cron
0 9 * * 1-5  cd /path/to/Portal_Hearness && python3 harness.py schedule --run-now --once >> data/cron.log 2>&1
```

---

## 10. Closing tickets safely (approval workflow)

```
find candidates ──► draft proposal ──► YOU review (edit / approve / reject) ──► apply ──► verify ──► audit log
 (Resolved, idle     (closing note from      nothing changes in Redmine          (status, note,   (re-reads the
  3+ days)            a template)            until you approve + PIN              % done, fields)   ticket)
```

| Step | Command line | Dashboard |
|---|---|---|
| Find tickets ready to close | `python harness.py close candidates` | Approvals → **Find candidates** |
| Draft one proposal | `python harness.py close draft 1234 --template bug-fixed --var release="v2.4.1"` | My tickets → **Draft closure** |
| Draft for all candidates | `python harness.py close auto-draft` | Approvals → **Draft proposals for all** |
| Review and approve (you) | `python harness.py close review` | Approvals → **Approve & close ticket** |
| See results | `python harness.py close list --status all` | Approvals → **History** |

What each proposal status means:

| Status | Meaning |
|---|---|
| `pending` | Waiting for your review. Nothing has changed in Redmine |
| `approved` | Approved but not applied yet |
| `executed` | Applied in Redmine and verified |
| `rejected` / `cancelled` | Declined or withdrawn. Nothing changed |
| `stale` | Someone updated the ticket after the draft was made. Draft it again |
| `failed` | Redmine refused the change (for example the workflow does not allow that status). The reason is shown |

Built-in safety checks:
- Only a person can approve, and with your PIN once approvers are registered.
- The approval covers *exactly* the change you saw. Editing the proposal means approving it again.
- Redmine's workflow rules are checked before review, and the result is verified afterwards.

---

## 11. Closure templates

Templates are reusable closing notes. Six starters are included:

| Template | Use |
|---|---|
| `standard-resolution` | General closure |
| `bug-fixed` | Defect fixed (sets Resolution = Fixed) |
| `support-resolved` | Request completed |
| `incident-resolved` | Incident closed; asks for the root cause |
| `no-response` | No reply from the requester |
| `duplicate` | Duplicate ticket, set to Rejected |

They are stored in `data/closure_templates.yaml`. Edit them on the dashboard's **Closure templates**
page, or ask your AI agent. From the terminal:

```bash
python harness.py templates list
python harness.py templates preview bug-fixed 1234 --var release="v2.4.1"   # see the note for a real ticket
python harness.py templates create --file my-template.yaml
```

A template sets:
- the closing note;
- the target status;
- % done;
- optionally a private note;
- custom fields such as `Resolution: Fixed`;
- the variables you fill in when drafting.

Notes can use placeholders such as `{{ issue.id }}`, `{{ issue.subject }}`, `{{ issue.author }}`,
`{{ me.name }}` and `{{ today }}`. For the full format, see
[.agents/skills/redmine-closure-templates/SKILL.md](.agents/skills/redmine-closure-templates/SKILL.md).

---

## 12. Reports and CSV export

Build reports on the dashboard (**Reports & CSV**), ask your AI agent, or use the terminal:

```bash
python harness.py export --list-columns --dataset issues                       # what columns exist
python harness.py export --dataset sla --days 7 --date-field closed_on --status closed --title "Weekly SLA"
python harness.py export --dataset issues --date-field open_now --status open --group-by assigned_to,priority
python harness.py export --dataset time_entries --from 2026-10-01 --to 2026-10-31 --group-by user,project
python harness.py export --preset weekly-sla --out weekly.csv
```

- **Datasets:** `issues` (tickets), `sla` (tickets with SLA columns), `time_entries` and `team`.
- **Group-by:** gives totals per group, such as count, open/closed, SLA % and hours.
- **Presets:** saved reports you can run by name. Three are built in: `weekly-sla`,
  `open-backlog-by-assignee` and `monthly-timesheet`. Add your own under `reports.presets` in
  `config/settings.yaml`, and list them all with `python harness.py export --list-presets`.
- **Output:** files go to `data/exports/` unless you give `--out`. They are UTF-8 CSV and open
  correctly in Excel.

---

## 13. Command cheat sheet

Add `--json` to most commands for machine-readable output.

| Area | Command | What it does |
|---|---|---|
| Setup | `python harness.py init` | Create `.env` and `config/settings.yaml` |
| | `python harness.py check` | Test the configuration and the Redmine connection |
| | `python harness.py sync` | Update the local copy of Redmine now (`--full`: everything again; `--status`: what it holds) |
| Tickets | `python harness.py my-tickets` | My open tickets, most urgent first (`--status all`, `--csv file.csv`) |
| | `python harness.py ticket 1234` | One ticket: details, SLA, history, allowed next statuses |
| | `python harness.py search --text "vpn" --status all` | Search tickets |
| Dashboards | `python harness.py overview --days 30` | Overview numbers |
| | `python harness.py sla --days 30` | SLA compliance |
| | `python harness.py team --days 30` | Team workload |
| | `python harness.py performance --days 90` | Performance |
| | `python harness.py time --from 2026-10-01 --to 2026-10-31` | Time spent |
| Reports | `python harness.py export --dataset issues --days 30` | CSV export ([section 12](#12-reports-and-csv-export)) |
| E-mail | `python harness.py daily-overview` | Preview the daily e-mail (`--send` to send) |
| Closing | `python harness.py close candidates` | Tickets ready to close |
| | `python harness.py close draft 1234` | Draft a closure proposal |
| | `python harness.py close review` | **You:** review and approve proposals |
| | `python harness.py close list --status all` | All proposals and their status |
| Templates | `python harness.py templates list` | Closure templates |
| Approvers | `python harness.py approver add "Your Name"` | **You:** register your approval PIN |
| Raw API | `python harness.py api /projects.json --all` | Read-only access to any Redmine REST endpoint |
| Audit | `python harness.py audit` | Who did what, and when |
| Skills | `python harness.py skills install --tool all` | Install the AI skills for your user account |
| Servers | `python harness.py dashboard` | Web dashboard (http://localhost:8000) |
| | `python harness.py schedule` | Daily scheduler |

Global options go before the command:

| Option | Effect |
|---|---|
| `--read-only` | Block all changes for this run |
| `--as "Your Name"` | Name recorded in approvals and the audit log |
| `-c path/to/settings.yaml` | Use another configuration file |

Exit codes:

| Code | Meaning |
|---|---|
| `0` | OK |
| `1` | Checks failed |
| `2` | Error |
| `3` | Permission denied |
| `4` | Not installed |

---

## 14. Security and permissions

- **No change without your permission.** The only change the harness ever makes in Redmine is
  applying a closure that a person approved: status, note, % done and custom fields. It never deletes
  anything.
- **The approval is tied to the exact change.** It carries a fingerprint (SHA-256) of the change. If
  anything is edited, it must be approved again.
- **Stale and failed changes are caught.** If the ticket changed after drafting, the proposal becomes
  *stale* and nothing is written. After applying, the ticket is read again to confirm the result.
- **Approver PINs stop AI agents from approving**, even agents that can type into terminals
  ([section 6](#6-register-yourself-as-an-approver-pin)).
- **Read-only switch.** `REDMINE_READ_ONLY=true` blocks every write.
- **Everything is audited.** View it with `python harness.py audit` or the dashboard's **Audit log**.
- **Secrets stay local.**
  - The API key and SMTP password live only in `.env`, which git ignores.
  - `check` shows the key masked.
  - Claude Code is blocked from reading `.env`.
  - Never paste secrets into an AI chat.
- **Dashboard on a network:** set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD`, and use HTTPS.

More detail: [docs/permissions-and-approvals.md](docs/permissions-and-approvals.md).

---

## 15. Try it without a real Redmine (demo mode)

The project includes a demo Redmine with realistic fake data: about 350 tickets, 7 people and
5 projects.

```bash
# terminal 1 - start the demo Redmine (keeps running)
python harness.py mock-server --port 3000
```

```bash
# terminal 2 - point the harness at it
export REDMINE_URL=http://127.0.0.1:3000 REDMINE_API_KEY=demo-api-key REDMINE_READ_ONLY=false
python harness.py check
python harness.py my-tickets
python harness.py dashboard
```

- **Windows PowerShell:** set the variables with `$env:REDMINE_URL="http://127.0.0.1:3000"`,
  `$env:REDMINE_API_KEY="demo-api-key"` and `$env:REDMINE_READ_ONLY="false"`.
- **Alternative:** put the same three lines in `.env`.
- **Demo user:** Karthik Raja.
- **Simulate a big, slow Redmine:** run `python harness.py mock-server --port 3000 --issues 5000 --latency 0.5`.
  This gives 5,000 tickets, with every request taking half a second. The first download takes a little
  while, and then every page still opens instantly.

---

## 16. Run it on a server

| Method | How |
|---|---|
| **Docker Compose** | Fill in `.env` (set a dashboard login), then run `docker compose up -d`. `docker-compose.yml` runs the dashboard (port 8000) and the scheduler, sharing `./data` |
| **systemd (Linux)** | One service for `harness.py dashboard` and one timer for `harness.py schedule --run-now --once`. See [docs/scheduling.md](docs/scheduling.md) |
| **Windows** | Task Scheduler for the daily job ([docs/scheduling.md](docs/scheduling.md)), plus a service wrapper or a logged-in session for the dashboard |

**Back up** `.env`, `config/settings.yaml` and the `data/` folder.

---

## 17. Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `python: command not found` | Different Python command name | Use `python3` (macOS/Linux) or `py` (Windows) |
| *"not installed yet"* (exit code 4) | No virtualenv | Run the install steps in [section 4](#4-install) |
| `No Redmine credentials configured` | `.env` incomplete | Set `REDMINE_URL` and `REDMINE_API_KEY` |
| `401` | Wrong key, or the REST API is off | Copy the key again; ask an admin to enable the REST web service |
| `403` on some requests | Your Redmine role lacks that permission | Normal for admin-only data. Use an account with the right role if needed |
| `Could not reach Redmine` | Wrong URL, VPN or proxy, or the Codex sandbox | Check `REDMINE_URL` and VPN; set `HTTPS_PROXY`; in Codex enable network ([section 7.1](#71-start-your-tool-in-the-project-folder)) |
| `TLS/SSL verification failed` | Company certificate | Set `REDMINE_CA_BUNDLE` |
| `Expected JSON ... got text/html` | URL points at a login page or proxy | Use the Redmine root URL |
| `550 5.7.30 Basic authentication is not supported` | Microsoft 365 SMTP login disabled | Ask your admin to enable Authenticated SMTP for the mailbox, or use a relay ([section 5.4](#54-e-mail--smtp-settings)) |
| `535 Authentication failed` (Gmail) | Normal password used | Create a Google App Password |
| E-mail never arrives | `EMAIL_ENABLED=false`, or no recipients | Check `.env`; test with `daily-overview --send --to you@...` |
| Approve button says *"Enter your name / PIN"* | Missing reviewer details | Fill in the name and PIN fields at the top of the Approvals page |
| Closure `failed`: status unchanged | Redmine's workflow doesn't allow that status for your role | Choose another target status, or ask a Redmine admin |
| Proposal `stale` | The ticket changed after drafting | Draft it again |
| Your AI tool doesn't see the skills | Started in another folder | Start it in the project folder, or run `skills install`; restart or reload the tool |
| Dashboard not reachable from other PCs | Bound to this computer only | Set `DASHBOARD_HOST=0.0.0.0` plus a login; open the port in the firewall |
| *"Preparing your dashboard"* stays for a long time | The first download of a big Redmine | Wait for the progress bar, or set `REDMINE_PROJECTS` to your team's projects and restart. Only the first start is slow |
| Red note *"Couldn't update from Redmine"* | Redmine unreachable (VPN, proxy, URL) | Pages keep working from the local copy. Fix the connection; it retries by itself |
| Note *"This period starts before the local copy"* | Period longer than `SYNC_HISTORY_DAYS` | Choose a shorter period, or raise `SYNC_HISTORY_DAYS` (for example `365`) and restart |
| A ticket I just changed in Redmine isn't shown yet | Next background update not due yet | Click **↻** in the dashboard header, or run `python harness.py sync` |

---

## 18. FAQ

**Where exactly do I put my Redmine API key?**
In the `.env` file in the project folder, on the line `REDMINE_API_KEY=...`. Create the file with
`python harness.py init`.

**Where do I put the SMTP / e-mail settings?**
Also in `.env`: the `SMTP_*`, `SMTP_SENDER`, `DAILY_RECIPIENTS`, `DAILY_SEND_TIME` and `EMAIL_ENABLED`
lines ([section 5.4](#54-e-mail--smtp-settings)).

**Can the AI agent close tickets by itself?**
No. It can only prepare proposals. A person approves them, and with a PIN once approvers are
registered.

**Does the AI agent see my API key or SMTP password?**
It doesn't need to. The harness reads them from `.env` itself, and `check` shows the key masked. Don't
paste secrets into the chat.

**Do I need an AI subscription?**
No. The dashboard and command line work on their own. The AI terminal skills are an extra.

**Which Redmine versions work?**
4.1 or newer. 5.0+ is recommended.

**Can my whole team use it?**
Yes. Run the dashboard on a server with a login, register each approver, and point
`DASHBOARD_PUBLIC_URL` at it so e-mails link to the Approvals page.

**What does it change in Redmine?**
Only approved closures: status, closing note, % done and the custom fields set by the template. Every
change is audited.

**Why are dashboard numbers up to a few minutes old?**
Pages are built from the local copy so they open instantly, and the copy fetches changes every
`SYNC_INTERVAL_MINUTES` (default 5). Click **↻** to update immediately. Closing a ticket through the
harness shows up at once. Before closing anything, the harness always re-reads the ticket live from
Redmine.

---

## 19. Project layout and further reading

```
harness.py              launcher: python harness.py <command> (finds .venv, works from any folder)
.env.example            template for .env - API key, SMTP, dashboard login (your copy is .env)
config/                 settings.example.yaml (team policies), closure_templates.yaml (starter templates)
.agents/skills/         AI agent skills (Codex, Gemini CLI, Copilot CLI, Cursor, OpenCode...)
.claude/skills/         the same skills for Claude Code; .claude/settings.json = permission rules
AGENTS.md               rules for AI agents working in this repo (CLAUDE.md, GEMINI.md point to it)
agent/                  agent manifest + system prompt (built-in chat and MCP)
redmine_agent/          the Python package: REST client, permission guard, SLA, reports, workflow, web, CLI
docs/                   in-depth documentation
tests/                  automated tests (run against the demo Redmine)
data/                   your local state - created at runtime, never committed
```

| Document | Read it for |
|---|---|
| [docs/agent-skills.md](docs/agent-skills.md) | Using the skills with each AI terminal tool |
| [docs/permissions-and-approvals.md](docs/permissions-and-approvals.md) | How "no change without permission" is enforced |
| [docs/scheduling.md](docs/scheduling.md) | cron, systemd, Windows Task Scheduler, Docker |
| [docs/redmine-rest-api.md](docs/redmine-rest-api.md) | Which Redmine API endpoints are used, and how |
| [docs/architecture.md](docs/architecture.md) | How the code is organised |

**For developers:** run `.venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/python -m pytest`.
All tests run against the built-in demo Redmine, with no network needed.

**License:** MIT. Chart.js is included under its own MIT licence (`redmine_agent/web/static/vendor/CHARTJS_LICENSE.md`).
