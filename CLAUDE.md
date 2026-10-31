# Claude Code instructions

The project rules live in AGENTS.md (shared by every AI coding tool):

@AGENTS.md

Claude Code specifics:
- Skills for operating the harness are in `.claude/skills/` (a mirror of `.agents/skills/`). Invoke them with `/redmine-my-tickets`, `/redmine-ticket-closure` and so on, or just ask.
- `.claude/settings.json` does three things:
  - pre-approves read-only `python harness.py ...` commands;
  - asks before drafting, e-mailing or starting servers;
  - blocks approval commands and reading `.env`.
