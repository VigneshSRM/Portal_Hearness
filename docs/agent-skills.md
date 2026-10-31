# Agent skills: run the harness from your AI terminal

If you already pay for an AI coding assistant that runs in the terminal, you can use it as the
harness's agent. You do not need a separate LLM API key. Supported assistants include **Claude Code**
(Claude subscription), **Codex CLI** (ChatGPT subscription), **Gemini CLI** (Google AI) and
**GitHub Copilot CLI**.

This works through **skills**. A skill is a folder with a `SKILL.md` file in the open
[Agent Skills](https://agentskills.io) format. It teaches the assistant how to drive the harness
safely. The assistant loads a skill automatically when your request matches its description.

## The skills

| Skill | What you can ask |
|---|---|
| `redmine-setup` | "Connect the harness to our Redmine", "why does check fail with 401?" |
| `redmine-my-tickets` | "What's on my plate?", "show #1234", "find tickets about VPN" |
| `redmine-dashboards` | "How is our SLA this month?", "team workload", "hours logged in October" |
| `redmine-reports` | "Export last week's SLA report", "October timesheet by person as CSV" |
| `redmine-daily-overview` | "Give me today's overview", "send the daily mail to the team", "schedule it for 9am" |
| `redmine-closure-templates` | "Create a template for duplicate tickets", "preview bug-fixed for #1234" |
| `redmine-ticket-closure` | "Close my resolved tickets", "close #1234 with the bug-fixed template" |
| `redmine-rest-api` | "List the versions of the infra project", "who is a member of service-desk?" |

Every skill calls the harness through one portable command, `python harness.py <command> --json`.
The launcher finds the project's `.venv` itself, so it works from any folder and needs no
activation.

## Setup per tool

First do the normal setup once:
1. Create `.venv`.
2. Run `python harness.py init`.
3. Fill in `.env`.
4. Run `python harness.py check`.

The `redmine-setup` skill can walk you through these steps. Then open your assistant **in the
repository folder**:

| Tool | Start | Skills folder it reads | Invoke a skill explicitly |
|---|---|---|---|
| Claude Code | `claude` | `.claude/skills/` | `/redmine-ticket-closure` |
| Codex CLI | `codex --config sandbox_workspace_write.network_access=true` | `.agents/skills/` | `$redmine-ticket-closure` or `/skills` |
| Gemini CLI | `gemini` | `.agents/skills/` | `/skills list` (Gemini asks before activating) |
| GitHub Copilot CLI | `copilot` | `.agents/skills/` (also `.claude/skills/`) | `/redmine-ticket-closure`, `/skills list` |
| Cursor, OpenCode, others | as usual | `.agents/skills/` | by description |

Tool-specific notes:

- **Codex CLI** runs commands in a sandbox *without network* by default, so the harness could not reach
  Redmine. Use the flag shown above, or add this to `~/.codex/config.toml`:
  ```toml
  [sandbox_workspace_write]
  network_access = true
  ```
- **Claude Code** reads `.claude/settings.json` in this repo:
  - read-only harness commands run without prompts;
  - drafting, e-mailing and starting servers ask first;
  - approval commands and reading `.env` are blocked.
- **Use the skills from any folder.** Install them for your user:
  `python harness.py skills install --tool all`. This copies them to `~/.claude/skills` and
  `~/.agents/skills`, pointing at this harness's absolute path. Remove them with
  `python harness.py skills uninstall`.

## How permission works with a terminal agent

| Level | Examples | Who can do it |
|---|---|---|
| Read | my tickets, dashboards, SLA, previews, raw GET API | the agent, freely |
| Local outputs | CSV exports, e-mail preview | the agent |
| Draft | closure proposals, template edits, sending an e-mail | the agent, only after you ask or agree in the chat |
| **Approve and apply ticket changes** | closing #1234 | **you only**: dashboard **Approvals** page, or `python harness.py close review` in your own terminal window |

The harness records which AI tool prepared each draft, for example `karthik via claude-code`. This
appears in proposals, the dashboard and the audit log:
- Claude Code, Gemini CLI and Codex are detected automatically.
- For any other tool, set `HARNESS_AGENT=<tool name>` in its environment.

A typical exchange:

> **You:** close my resolved tickets that are idle
>
> **Agent:** *(runs `close candidates`, previews the notes, asks you for an incident's root cause, then drafts)*
> "3 proposals are waiting for your approval. Approve them at http://localhost:8000/approvals or with
> `python harness.py close review` in your terminal. Nothing has changed in Redmine yet."
>
> **You:** *(approve in the dashboard)* done
>
> **Agent:** *(runs `close show`)* "#341, #342 and #344 are now Closed (verified)."

### Register an approver PIN (recommended)

Modern terminal agents can drive interactive programs, so "a person must be at the keyboard" is not
enough on its own. Register yourself as an approver once, in your own terminal:

```bash
python harness.py approver add "Your Name"
```

After this, **every** approval needs your PIN, whether in the CLI, the dashboard or the built-in chat:

- The PIN is stored only as a salted hash in `data/approvers.json`.
- Five wrong attempts lock approvals for 15 minutes.
- Never type the PIN into an AI chat. The skills tell agents never to ask for it.
- Manage approvers with `python harness.py approver list|change-pin|remove`.

## Adding or changing skills

1. Edit or add a folder under `.agents/skills/<name>/`. `name` must match the folder name, using
   lowercase letters and hyphens.
2. Run `python harness.py skills sync` to mirror it into `.claude/skills/`.
3. Run `python harness.py skills validate` to check it against the spec and the mirror.
4. Run `pytest tests/test_skills.py`, which checks three things:
   - every `python harness.py ...` command in a skill parses;
   - the referenced files exist;
   - the permission rules are present.

Keep each `SKILL.md` short (under 500 lines). Put long reference material in `references/`.

## Troubleshooting

| Problem | Fix |
|---|---|
| The assistant doesn't see the skills | Start it in the repo folder; restart it or reload skills (`/skills reload` in Gemini or Copilot) |
| `python` not found | The skills say to try `python3` (macOS/Linux) or `py` (Windows) |
| Exit code 4 "not installed yet" | Create `.venv` and install `requirements.txt` (see `redmine-setup`) |
| "Could not reach Redmine" only inside Codex | Enable sandbox network access (see above) |
| The agent says approval is impossible | Expected. Approve in the dashboard or with `close review` in your own terminal |
