# Permissions and approvals

> **Rule:** the agent never changes anything without your permission.

This page explains how that rule is enforced in code, and how the review flow works day to day.

## What counts as a "change"

| Action | Level | Needs approval? |
|---|---|---|
| Reading tickets, dashboards, SLA, time entries | read | No |
| Previewing a closure note or the daily e-mail | read | No |
| Drafting a closure proposal (goes into the queue as *pending*) | draft | No. Nothing changes in Redmine. |
| Exporting a CSV report to your disk | draft | No |
| Cancelling a *pending* proposal | draft | No |
| **Updating or closing a ticket in Redmine** | write | **Yes.** A person approves each proposal. |
| **Sending an ad-hoc e-mail** from the agent chat | write | **Yes** |
| **Creating, editing or deleting closure templates** from the agent chat | write | **Yes** |
| Scheduled daily overview e-mail | standing permission | Only sent when you set `email.enabled: true` and list recipients. That configuration is your permission. |

The harness has **no code path** for deleting tickets, time entries, projects or users.

## How the code enforces it

```
 agent / CLI / dashboard / scheduler
            │
            ▼
   ClosureWorkflow (approvals.py) ─── draft ──► SQLite queue (pending)  ◄── human approves (dashboard / CLI TTY)
            │ execute (approved only)
            ▼
   RedmineClient.update_issue(..., approval=Approval)
            │
            ▼
   WriteGuard.authorize()  (guard.py)
     1. read-only mode?                → deny
     2. approval present?              → deny if missing
     3. action + target match?         → deny if not ("update_issue" on "issue:123")
     4. approver is a human?           → deny agent/automation/scheduler/mcp/system
     5. SHA-256(payload) == approved?  → deny if the change differs from what was approved
     6. store verifier                 → proposal still 'approved', reviewer is human, digest matches
            │
            ▼
   RedmineClient._request("PUT")  — refuses any non-GET that did not pass the guard
```

Other safeguards:

- **Bound to the exact change.** The approval stores a SHA-256 digest of the exact JSON that
  will be sent. Editing the note, status or % done after approval sends the proposal back to
  *pending*.
- **Staleness check.** Before executing, the ticket is read again. If anyone changed it since the
  proposal was drafted (a new comment, a status change), the proposal becomes **stale** and nothing
  is written.
- **Workflow check.** On Redmine 5.0+ the draft step asks Redmine which statuses your role may set
  (`include=allowed_statuses`). A transition the workflow does not allow is rejected before review.
- **Result verification.** Redmine *silently ignores* a status change its workflow does not allow,
  while still adding the note. After every update the harness reads the ticket again. If the status
  did not change, the proposal is marked **failed** and tells you why.
- **No automatic retries for writes.** A retried PUT could post the note twice, so writes are sent
  exactly once.
- **Read-only switch.** `REDMINE_READ_ONLY=true` (or `--read-only` on the CLI) blocks every write,
  including approved ones.
- **Audit log.** Every draft, edit, approval, rejection, execution, denied attempt, export and
  e-mail is recorded in `data/harness.db`. View it with `python -m redmine_agent audit` or on the
  dashboard's **Audit log** page.

## Who can approve

| Surface | How approval works |
|---|---|
| Dashboard → **Approvals** | Click **Approve & close ticket**. The reviewer is your dashboard login when `DASHBOARD_USERNAME` is set. Otherwise it is the name you type at the top of the page. |
| CLI `close review` / `close approve` | Needs an **interactive terminal** and a typed `yes`. With no TTY it refuses, which stops scripts and coding agents from approving silently. |
| Agent chat (`chat`) | The agent's `close_ticket` call shows the exact change and waits for you to type `yes`. |
| MCP clients | **Cannot approve.** They can draft proposals and run `execute_approved_proposal` only on proposals a person already approved. |

Reviewer names `agent`, `llm`, `assistant`, `automation`, `scheduler`, `system` and `mcp` are
always rejected.

## Proposal states

| State | Meaning |
|---|---|
| `pending` | Drafted and waiting for review. Nothing has changed in Redmine. |
| `approved` | A person approved it. It is executed immediately ("Approve & close") or later (`close execute`). |
| `executed` | Applied to Redmine and verified. |
| `rejected` | A person said no. |
| `cancelled` | Withdrawn before review. |
| `stale` | The ticket changed after drafting, or the content changed after approval. Draft it again. |
| `failed` | Redmine refused the update (422), or verification showed it did not apply. |

## Hardening checklist for production

- Use a dedicated Redmine account for the harness, with only the roles it needs. Redmine's own
  permissions still apply on top of the harness rules.
- Set `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` before binding the dashboard to `0.0.0.0`, and
  put it behind HTTPS (reverse proxy).
- Keep `.env` out of version control (it is in `.gitignore`).
- Start with `REDMINE_READ_ONLY=true` while you evaluate the dashboards. Turn it off only when you
  want approved closures to apply.
