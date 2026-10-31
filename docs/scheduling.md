# Scheduling the daily overview

The daily job does two things:

1. **Drafts** closure proposals for idle *Resolved* tickets, if
   `closure.auto_propose_on_schedule: true`. These are drafts only. Nothing closes until a person
   approves it.
2. Builds the **daily overview e-mail** and sends it when `email.enabled: true` and recipients are
   set. Otherwise it saves a preview to `data/outbox/`.

Pick **one** of the options below.

## Option A: built-in scheduler

```bash
python harness.py schedule                    # runs every day at email.send_time on email.send_days
python harness.py schedule --run-now          # run once immediately, then keep scheduling
python harness.py schedule --run-now --once   # run once and exit (handy for testing)
```

The timezone is `timezone` in `settings.yaml` (default `Asia/Kolkata`).

## Option B: cron (Linux/macOS)

```cron
# m h  dom mon dow  command
0 9 * * 1-5  cd /opt/portal-harness && python3 harness.py close auto-draft >> data/cron.log 2>&1
5 9 * * 1-5  cd /opt/portal-harness && python3 harness.py daily-overview --send >> data/cron.log 2>&1
```

Make sure cron runs in your timezone, or adjust the hour (`CRON_TZ=Asia/Kolkata` works with cronie).

## Option C: systemd timer

`/etc/systemd/system/redmine-agent-daily.service`

```ini
[Unit]
Description=Redmine agent daily overview

[Service]
Type=oneshot
WorkingDirectory=/opt/portal-harness
ExecStart=/usr/bin/python3 /opt/portal-harness/harness.py schedule --run-now --once
User=redmine-agent
```

`/etc/systemd/system/redmine-agent-daily.timer`

```ini
[Unit]
Description=Run the Redmine agent daily overview on weekdays

[Timer]
OnCalendar=Mon..Fri 09:00 Asia/Kolkata
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now redmine-agent-daily.timer
```

The dashboard can run as a normal service:
`ExecStart=/usr/bin/python3 /opt/portal-harness/harness.py dashboard --host 127.0.0.1 --port 8000`,
behind nginx with HTTPS.

## Option D: Windows Task Scheduler

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\portal-harness\.venv\Scripts\python.exe" `
           -Argument "-m redmine_agent schedule --run-now --once" -WorkingDirectory "C:\portal-harness"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00am
Register-ScheduledTask -TaskName "Redmine daily overview" -Action $action -Trigger $trigger
```

`tzdata` is installed automatically on Windows, so IANA timezones like `Asia/Kolkata` work.

## Option E: Docker Compose

`docker-compose.yml` runs the dashboard and the scheduler as two services that share `./data`:

```bash
docker compose up -d
```

## Testing the e-mail safely

```bash
python harness.py daily-overview                  # preview only -> data/outbox/*.html
python harness.py daily-overview --print          # also print the plain-text version
python harness.py daily-overview --send --to you@example.com
```

SMTP notes:

| Provider | Host | Port | Security |
|---|---|---|---|
| Office 365 | `smtp.office365.com` | 587 | `starttls` |
| Gmail | `smtp.gmail.com` | 587 | `starttls` |

For Gmail, use an app password. For an internal relay without auth, use port 25 with
`SMTP_SECURITY=none`.
