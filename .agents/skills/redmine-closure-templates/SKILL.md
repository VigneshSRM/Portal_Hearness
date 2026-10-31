---
name: redmine-closure-templates
description: Create, edit, preview and validate ticket-closure templates for Redmine - reusable closing notes with placeholders (ticket, requester, today), reviewer-supplied variables such as root cause or release, target status, % done, private note and custom fields like Resolution. Use when the user wants a new closure template, to change or review existing templates, to see how a closing note would look for a ticket, or asks which templates exist.
license: MIT
compatibility: Needs the Portal Harness repository (python harness.py) with Redmine configured, and network access to Redmine for previews.
metadata:
  author: portal-harness
  version: "1.1"
---

# Closure templates

## Running the harness

Run from the repository root (the folder that contains `harness.py`):

```bash
python harness.py templates list --json
```

- Use `python3` (macOS/Linux) or `py` (Windows) if `python` is not found. The launcher picks the project's
  virtualenv by itself.
- Exit codes: `0` ok, `1` validation failed, `2` error, `3` permission denied, `4` not installed.

## Look up templates

| Need | Command |
|---|---|
| All templates | `python harness.py templates list --json` |
| One template | `python harness.py templates show <template-id> --json` |
| Rendered note for a real ticket | `python harness.py templates preview <template-id> <issue-id> --var release=v2.4 --json` |

A preview changes nothing; it only renders the note. `--var name=value` can be repeated.

## Create a template

1. Ask what the template is for:
   - which status it sets (e.g. `Closed`, `Rejected`);
   - the `% done`;
   - whether the note should be private;
   - custom fields to set (e.g. `Resolution: Fixed`);
   - which values the reviewer must fill in (variables);
   - which trackers it suits.
2. Write one template as YAML to `data/drafts/<template-id>.yaml` (format below; `data/` is never committed).
3. Validate it: `python harness.py templates validate data/drafts/<template-id>.yaml`
4. Preview it on a real ticket: `python harness.py templates preview data/drafts/<template-id>.yaml <issue-id> --json`
5. Show the user the YAML and the rendered preview, then ask "Save this template?". Only after a clear yes:
   `python harness.py templates create --file data/drafts/<template-id>.yaml --json`

## Edit a template

1. Write only the fields to change to a YAML file, for example `data/drafts/<template-id>-changes.yaml`.
2. Show the change and get a yes.
3. Run `python harness.py templates update <template-id> --file data/drafts/<template-id>-changes.yaml --json`

Single fields can also be changed with flags: `--name`, `--status`, `--done-ratio`, `--description` or `--note-file`.

## Delete a template

Deleting needs the person. Ask them either:
- to delete it on the dashboard's **Closure templates** page, or
- to run `python harness.py templates delete <template-id>` in their own terminal.

## Template format

```yaml
id: bug-fixed                    # lowercase letters, digits, dashes
name: Bug fixed and deployed
description: Defect fixed, deployed and verified.
target_status: Closed            # Redmine status name (or id)
done_ratio: 100                  # 0-100, or "keep" to leave it unchanged
private_note: false
custom_fields:                   # optional - field name (or id): value
  Resolution: Fixed
applies_to:                      # optional - suggest it for these trackers / projects
  trackers: [Bug]
variables:                       # values the reviewer supplies when drafting a closure
  - name: fix_summary
    label: What was fixed
    default: The defect has been fixed and verified.
  - name: release
    label: Release / deployment reference
    required: true
note: |
  Hi {{ issue.author }},

  The bug reported in #{{ issue.id }} ("{{ issue.subject }}") has been fixed.
  * Fix: {{ fix_summary }}
  * Deployed in: {{ release }}

  Thanks,
  {{ me.name }}
```

Placeholders and rules: [references/placeholders.md](references/placeholders.md).

## Gotchas

- The target status must be allowed by Redmine's workflow from the ticket's current status. "New → Closed"
  is often forbidden; drafting a closure reports the allowed next statuses.
- Custom-field values must match Redmine's list options exactly (e.g. `Fixed`), or Redmine rejects the
  closure with a 422 error.
- Every variable used in `note` must be declared under `variables`, and validation catches mistakes. Write
  a literal `{{` as `{{ '{{' }}`.
- Templates live in `data/closure_templates.yaml`, which is local to this harness, not in Redmine. The
  starter set comes from `config/closure_templates.yaml`.
- `closure.template_rules` in `config/settings.yaml` picks a template per tracker automatically.

## Permission rules

- Saving or editing a template changes the shared library. Always show the full template and get an
  explicit yes first. Deleting is for the person only.
- Templates never change tickets by themselves. Closing happens through `redmine-ticket-closure` with
  human approval.
- Never run `close approve`, `close review`, `close reject`, `close execute` or `templates delete`. Never
  work around the interactive-terminal or PIN checks, and never ask for an approver PIN.
