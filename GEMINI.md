# Gemini CLI instructions

The project rules live in AGENTS.md (shared by every AI coding tool):

@AGENTS.md

Gemini CLI specifics:
- Skills for operating the harness are loaded from `.agents/skills/`. List them with `/skills list`; Gemini asks before activating one.
- The harness is called through `python harness.py <command>`. Confirm shell commands when prompted.
- Never confirm approval commands for the user (`close approve`, `close review`, `close execute`, `close reject`).
