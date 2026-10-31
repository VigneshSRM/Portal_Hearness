# Scheduling options

Pick one. All of them run the same daily job:
1. Draft closure proposals for idle Resolved tickets (drafts only).
2. Send the overview if `EMAIL_ENABLED=true`. Otherwise save a preview in `data/outbox/`.

Replace `/opt/portal-harness` with the harness folder.

## Built-in scheduler (simplest)

Run `python harness.py schedule` and keep it running (terminal, `tmux`, service).

## cron (Linux/macOS)

```cron
CRON_TZ=Asia/Kolkata
0 9 * * 1-5  cd /opt/portal-harness && python3 harness.py schedule --run-now --once >> data/cron.log 2>&1
```

## systemd timer

`/etc/systemd/system/redmine-agent-daily.service`:

```ini
[Service]
Type=oneshot
WorkingDirectory=/opt/portal-harness
ExecStart=/usr/bin/python3 /opt/portal-harness/harness.py schedule --run-now --once
```

`/etc/systemd/system/redmine-agent-daily.timer`:

```ini
[Timer]
OnCalendar=Mon..Fri 09:00 Asia/Kolkata
Persistent=true
[Install]
WantedBy=timers.target
```

Enable it with `sudo systemctl enable --now redmine-agent-daily.timer`.

## Windows Task Scheduler (PowerShell)

```powershell
$action  = New-ScheduledTaskAction -Execute "py" -Argument "harness.py schedule --run-now --once" -WorkingDirectory "C:\portal-harness"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 9:00am
Register-ScheduledTask -TaskName "Redmine daily overview" -Action $action -Trigger $trigger
```

## Docker Compose

`docker compose up -d` starts the dashboard and a `scheduler` service that shares `./data`.

## SMTP quick reference

| Provider | Host | Port | Security | Note |
|---|---|---|---|---|
| Office 365 | `smtp.office365.com` | 587 | `starttls` | |
| Gmail | `smtp.gmail.com` | 587 | `starttls` | Use an app password |
| Internal relay | your relay | 25 | `none` | Often needs no login |
