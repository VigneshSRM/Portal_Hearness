# Template placeholders

Closing notes are Jinja2 templates rendered in a **sandbox**: no code execution and no file access.
Unknown names are errors.

## Built-in values

| Placeholder | Example |
|---|---|
| `{{ issue.id }}` | `1234` |
| `{{ issue.subject }}` | `Checkout button unresponsive on Firefox` |
| `{{ issue.project }}`, `{{ issue.tracker }}`, `{{ issue.status }}`, `{{ issue.priority }}` | `Customer Portal`, `Bug`, `Resolved`, `High` |
| `{{ issue.author }}` | the requester's name |
| `{{ issue.assigned_to }}` | the assignee, or `Unassigned` |
| `{{ issue.created_on }}` | `22 Oct 2026` |
| `{{ issue.due_date }}` | `2026-11-13`, or empty |
| `{{ issue.done_ratio }}`, `{{ issue.spent_hours }}`, `{{ issue.estimated_hours }}` | `100`, `3.5`, `4` |
| `{{ issue.category }}`, `{{ issue.version }}` | category and target version names |
| `{{ issue.url }}` | link to the ticket |
| `{{ me.name }}`, `{{ me.login }}`, `{{ me.mail }}` | the Redmine account running the harness |
| `{{ today }}` | `31 Oct 2026` |
| `{{ now }}` | `31 Oct 2026 14:05`, in the configured timezone |

## Your variables

Declare every custom value under `variables`:

```yaml
variables:
  - name: root_cause            # snake_case, used as {{ root_cause }}
    label: Root cause           # shown to the reviewer
    required: true              # drafting stops until it is provided
  - name: prevention
    label: Preventive action
    default: Monitoring has been added to detect this earlier.
```

When drafting, values are passed as `--var root_cause="Disk full" --var prevention="Alert at 80%"`.
Missing optional values fall back to `default`.

## Logic

```jinja
{% if issue.tracker == "Incident" %}Post-incident review scheduled.{% endif %}
{% if issue.spent_hours > 8 %}Thanks for your patience on this one.{% endif %}
{{ issue.subject | upper }}                  {# standard Jinja filters work #}
```

## Validation rules

- `id`: 2-63 characters; lowercase letters, digits and dashes.
- `name` and `note`: required.
- `done_ratio`: 0-100, or omit / `keep` to leave the ticket's value unchanged.
- Every placeholder in `note` must be built in or declared under `variables`.
- Variable names: snake_case. The names `issue`, `me`, `today`, `now` and `vars` are reserved.
