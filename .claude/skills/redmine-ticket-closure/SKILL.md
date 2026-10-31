---
name: redmine-ticket-closure
description: Close Redmine tickets safely with human review - find tickets ready to close (Resolved and idle), preview the closing note from a closure template, draft closure proposals into the approval queue, hand them to a person to approve in the dashboard or their own terminal, and verify the outcome. Use when the user wants to close, resolve, finish or clean up tickets, close a specific ticket such as #1234, or asks about pending, approved, failed or stale closure proposals. Never closes anything without a person's approval.
license: MIT
compatibility: Needs the Portal Harness repository (python harness.py) with Redmine configured, and network access to Redmine.
metadata:
  author: portal-harness
  version: "1.1"
---

# Ticket closure with review and approval

You prepare the closure, a person approves it, and the harness applies it. You can never approve.

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py close candidates --json
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher picks the project's
  virtualenv by itself.
- Exit codes: `0` ok, `2` error (read the message), `3` permission denied, `4` not installed.

## Workflow

### 1. Find what can be closed

- `python harness.py close candidates --json` lists the user's own tickets that are Resolved and idle
  for 3 days or more. Add `--scope all` for the whole team.
- Each item has `issue_id`, `subject`, `status`, `idle_days`, `suggested_template` and `url`.
- For a specific ticket, run `python harness.py ticket <issue-id> --json` and check `status`,
  `allowed_next_statuses` and `open_proposals`.

### 2. Preview the closing note

`python harness.py templates preview <template-id> <issue-id> --var name=value --json`

- Use `suggested_template` unless the user picks another (`python harness.py templates list --json`).
- When a template has required variables (e.g. `root_cause` for incidents), ask the user for the
  values. Never invent root causes, releases, ticket numbers or other facts.

### 3. Draft the proposal (after the user agrees)

`python harness.py close draft <issue-id> --template <template-id> --var name=value --reason "<why>" --json`

- **Variants:** `--note "<text>"` for a custom note without a template, or `--status Rejected` for a
  different target status.
- **Several tickets:** run one `close draft` per ticket.
- **Every candidate at once** (only if the user asks): `python harness.py close auto-draft --json`.
  Report the `skipped` items and their reasons.
- **What drafting does:** nothing in Redmine. It returns `proposal_id`, the before/after `changes`, the
  `note` and any `warnings`. Show these to the user.

### 4. Hand over for approval

Always tell the user plainly:

> Proposal `<proposal-id>` for #<issue-id> is waiting for your approval. Approve it on the dashboard's
> **Approvals** page (http://localhost:8000/approvals - start it with `python harness.py dashboard` if it
> is not running), or run `python harness.py close review` in your own terminal window. Nothing changes
> in Redmine until you approve.

If approver PINs are set up (`python harness.py approver list --json` shows `pin_required: true`), the
person enters their PIN while approving. You never see it and must never ask for it.

### 5. Verify (when the user says they approved)

Check with `python harness.py close show <proposal-id> --json` or `python harness.py close list --status all --json`.

| `status` | Meaning | Tell the user |
|---|---|---|
| `pending` | Not reviewed yet | Still waiting for their approval |
| `approved` | Approved, not applied yet | They can apply it on the dashboard or with `close execute` in their terminal |
| `executed` | Applied and verified in Redmine | Done. Give `result.new_status` |
| `rejected` / `cancelled` | Declined / withdrawn | Nothing changed |
| `stale` | The ticket changed after drafting | Re-check the ticket and draft again |
| `failed` | Redmine refused, or the status did not change | Explain `result.message` / `result.errors` and suggest a fix |

To cancel one of your own pending drafts, and only when the user asks:
`python harness.py close cancel <proposal-id> --json`

## Common problems

- **"workflow does not allow ... Allowed next statuses: ..."** Pick an allowed status, or tell the user
  the ticket must first move to e.g. Resolved in Redmine.
- **"already has an open proposal"** Show it with `python harness.py close list --json`. Do not draft
  twice.
- **"Please provide: Root cause"** Ask the user for the value.
- **422 "... is not included in the list"** A custom-field value in the template does not match
  Redmine's options. Use `redmine-closure-templates` to fix the template.
- **Read-only mode** (`REDMINE_READ_ONLY=true`) Proposals can be drafted and approved but not applied.

## Permission rules (strict)

- You may read, preview, draft, cancel your own pending drafts, and check status. Nothing more.
- Never run `close approve`, `close review`, `close reject` or `close execute`.
- Never try to pass the interactive-terminal or PIN checks: no `script`, `expect`, `unbuffer`,
  pseudo-terminals, piping answers, or editing files in `data/`.
- Never ask for, accept or store an approver PIN. If the user offers one, tell them to keep it private
  and approve themselves.
- Never claim a ticket is closed unless `close show` reports `executed`.
- Draft only the tickets the user asked for, and never resubmit a rejected proposal unprompted.
