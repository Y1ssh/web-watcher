# Deploying Web Watcher

A watcher that only runs while your laptop is open is not much of a watcher.
This is how to move it somewhere that stays on, and — more importantly — how to
tell whether it is actually working once it gets there.

Read the whole of **The one thing that silently breaks a deployed watcher**
before you pick a host. It is the mistake that costs you the alert you deployed
for in the first place.

---

## 1. The one thing that silently breaks a deployed watcher

The watcher works by remembering what it saw last time. That memory lives in
`state/snapshot.json`.

Most hosting platforms give a deployment a **fresh, empty filesystem** — on
every deploy, every restart, and, for scheduled jobs, often on every single run.
If the snapshot lands on that filesystem, here is what happens:

1. The job wakes up. There is no snapshot, so it records a baseline.
2. The job exits. The filesystem is thrown away.
3. The job wakes up again. There is no snapshot, so it records a baseline.

It reports `BASELINE` forever and **never detects a change**. Nothing errors.
The logs look healthy. You simply never get told.

There are two ways out:

- **A long-running worker with a mounted disk.** The process stays up, runs its
  own schedule, and writes the snapshot to a disk that survives restarts. This
  is what `render.yaml` in this repo sets up, and it is the shape I would
  default to.
- **A scheduled job plus durable storage.** Cheaper, since it is idle most of
  the time, but only correct if your host lets a scheduled job attach storage
  that outlives the run. Check your host's current documentation for this — it
  varies by platform and by plan, and it is exactly the kind of detail that
  goes stale.

`preflight` warns you when the snapshot path looks like it lives inside the
project, which is the usual sign of this mistake:

```bash
python -m watcher preflight
```

```
[WARN] state        the snapshot lives inside the project at /app/state/snapshot.json
                    -> Most hosts give a deployment a fresh filesystem, which
                       would wipe this on every deploy -- the watcher would
                       re-baseline and miss the next real change.
```

Point `state_path` at your mounted disk (`/data/snapshot.json`) and the warning
goes away.

---

## 2. Choosing a host

First, know what shape your app is. A **scheduled job** wakes on a timer, does
its work, and sleeps — cheap, because it is idle most of the time. A
**long-running process** stays awake continuously — more expensive, but it
keeps its own memory and runs its own schedule.

Web Watcher can be either. `python -m watcher check` is one run, for a
scheduler to call. `python -m watcher watch` is the long-running form.

The **runtime** is Python 3.10 or newer. There are no dependencies to install,
which removes an entire category of deployment problem.

Broadly: platforms that grew up serving websites and front-end apps are built
around short serverless bursts, and are an awkward fit for something that must
run on a strict schedule and remember things between runs. Platforms built for
background workers and scheduled jobs — with attachable persistent storage —
fit a monitor naturally. Compare current pricing and storage options yourself
before committing; this is the part of the landscape that changes fastest.

You can also ask the agent to look at the code and make a grounded
recommendation rather than a generic one:

> Look at this codebase and tell me whether it runs as a scheduled job or a
> continuous process, what runtime it needs, and where the state has to live
> for it to work. Explain the trade-offs.

Treat the answer as an informed opinion to verify, not a verdict.

---

## 3. Pre-launch checklist

Run it. Do not read it and nod.

```bash
python -m watcher preflight
```

It exits non-zero if anything would actually break, so a deploy script can
refuse. What it covers:

| Check | Why it blocks |
| --- | --- |
| `secrets` | A credential in a tracked file is a leaked credential. |
| `gitignore` | `.env`, `config.json`, `state/`, `logs/` must never be pushed. |
| `environment` | Every variable the config names must be set, or the alert fails at the moment it is needed. |
| `state` | The snapshot directory must be writable, and should outlive a deploy. |
| `notify` | A watcher with no channels only fills a log file. |
| `action` | Warns loudly when an action is LIVE, and lists the guards still in force. |
| `interval` | Warns when a schedule is fast enough to look abusive. |
| `target` | Actually fetches the page and reads the value out of it. |

Then, separately:

**Confirm CI is green.** `.github/workflows/tests.yml` runs the suite on every
push across Python 3.10–3.13, scans for secrets, and builds the container. A
red check on launch day is a smoke alarm going off as you leave the house —
deal with it first.

**Prove the alert path actually delivers.**

```bash
python -m watcher test-notify
```

Did it arrive? Did it land in spam? Does it contain the values you asked for?

**Keep test and production credentials apart.** Use a webhook pointing at a
private channel and a practice target while you are building; swap in the real
ones only once a full cycle has succeeded. If a test key leaks, the damage is
limited. If one is ever exposed, **rotate it**: revoke it at the service that
issued it, generate a replacement, and paste the new value into your host's
environment settings. The old one stops working immediately.

---

## 4. Getting the settings onto the host

`config.json` is git-ignored, so it is not in the repository — which means a
fresh deployment has no settings. Two ways to fix that, and the config holds no
credentials by design, so both are safe:

**As an environment variable.** Set `WATCHER_CONFIG` to the config's JSON. The
app reads it when no `--config` is given. This needs nothing on disk and works
on every platform.

**As a committed production config.** Copy `config.production.example.json` to
`config.production.json`, edit it, commit it, and start with
`--config config.production.json`. It names environment variables rather than
holding secrets, so it is safe to commit — but double-check with
`python -m watcher scan-secrets` first.

Either way, credentials themselves go in the host's environment settings, never
in the config.

---

## 5. Deploying

### With the Render blueprint

`render.yaml` describes a worker with a 1 GB disk mounted at `/data`, tests as
the build command, and `WATCHER_CONFIG` plus `WATCHER_WEBHOOK_URL` marked
`sync: false` so the dashboard asks you for them instead of storing them in the
file. In Render: **New → Blueprint → pick this repository**, then fill in the
two values when prompted.

### With the container

```bash
docker build -t web-watcher .
docker run -d --name web-watcher \
  -v web-watcher-data:/data \
  -e WATCHER_CONFIG="$(cat config.production.json)" \
  -e WATCHER_WEBHOOK_URL="..." \
  web-watcher
```

The named volume is what makes the snapshot survive a restart. The image runs
as a non-root user and sets `PYTHONUNBUFFERED=1`, so each run appears in the log
dashboard as it happens rather than when a buffer happens to flush.

### On your own machine (Windows scheduled task)

The cheapest option, and a reasonable one if the machine is usually on. Windows
Task Scheduler owns the schedule, so you run `check` rather than `watch` — no
console window has to stay open, and the task survives a reboot.

The local disk is durable, so the ephemeral-storage problem in section 1 does
not apply here. `preflight` will still warn about it; that warning is aimed at
hosted deployments and can be read and dismissed.

**1. A launcher, so failures leave a trace.** Task Scheduler runs a program, not
a shell, so redirection needs a wrapper. Put this in `local/run-check.cmd`
(`local/` is git-ignored because the path is machine-specific):

```bat
@echo off
setlocal
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
echo [%date% %time%] run start >> "logs\task.out"
"C:\path\to\python.exe" -m watcher check >> "logs\task.out" 2>&1
set "RC=%ERRORLEVEL%"
echo [%date% %time%] run finished, exit %RC% >> "logs\task.out"
exit /b %RC%
```

Handing the exit code back matters: without it a crashed run looks to Task
Scheduler like a success that happened to do nothing.

**2. Register the task.**

```powershell
$root = "C:\path\to\web-watcher"
$action = New-ScheduledTaskAction -Execute "$root\local\run-check.cmd" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -Hidden
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "WebWatcher" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
```

Three of those settings are the difference between a watcher that works and one
that quietly does not: `-AllowStartIfOnBatteries` and
`-DontStopIfGoingOnBatteries` (Windows skips scheduled tasks on battery by
default, so a laptop watcher would only run while plugged in), and
`-StartWhenAvailable` (a run missed while the machine slept fires on wake
instead of being dropped).

Omit `-RepetitionDuration` to repeat indefinitely. Passing
`[TimeSpan]::MaxValue` looks right but serialises to a duration Task Scheduler
rejects.

**3. Prove it runs**, rather than trusting that it will:

```powershell
Start-ScheduledTask -TaskName "WebWatcher"
Get-ScheduledTaskInfo -TaskName "WebWatcher" | Select-Object LastRunTime, LastTaskResult, NextRunTime
```

`LastTaskResult` of `0` is success. Then confirm the watcher itself did
something — `check_count` in `state/snapshot.json` should have gone up, and
`logs/task.out` should have a new entry. A task that reports success while the
snapshot stands still is a task running the wrong thing.

**To remove it:**

```powershell
Unregister-ScheduledTask -TaskName "WebWatcher" -Confirm:$false
```

**The honest limit:** with `-LogonType Interactive` the task runs only while you
are logged in, and only while the machine is awake. Close the lid overnight and
it pauses until morning. That is the gap a real host fills, and the only reason
to move it off your machine.

Whichever route you take, ask the agent to narrate each step before it takes it.
Deployment touches accounts, billing, and live credentials. When a secret needs
pasting, paste it yourself — it should never pass through a conversation.

---

## 6. Confirming it actually started

A successful deploy message is not a working app. Open the host's logs and
confirm three things, in order.

**It started and found its settings:**

```
web-watcher starting | config: $WATCHER_CONFIG | using the platform environment | state: /data/snapshot.json
```

**It knows what it is doing:**

```
watching <div> id='price' at https://example.com/product every 900s, alerting when the number is below 50.0 (Ctrl+C to stop)
```

**It actually reached the site.** This is the one people skip, and the one that
matters most — an app can start perfectly and still never connect to the page
it is meant to watch:

```
BASELINE   first run - nothing to compare against yet
  url:   https://example.com/product (HTTP 200)
  value: $75.00
```

`HTTP 200` and a sensible value is the proof. A first run that says `BASELINE`
and then a second run fifteen minutes later that says `UNCHANGED` — rather than
`BASELINE` again — is the proof that your storage is genuinely persistent.

---

## 7. Testing the whole chain end to end

Testing the parts separately is not enough. Trigger the real thing.

The safest way is to **control the trigger yourself**: point the watcher at a
practice page you can edit, with test credentials and a private alert channel,
then flip that page to the value your condition is waiting for. Watch all three
stages complete: the check notices, the alert arrives, the action runs.

**Verify the action, not just the alert.** The alert is easy — it lands in your
inbox where you will see it. The action is where people get complacent. Do not
trust a log line saying the request succeeded: go and look at where the
submission should have landed, and confirm the fields hold the right values
rather than blanks or garbage.

Only point it at the real target once you have watched a full
trigger → alert → act cycle succeed against a practice one. Keep
`"dry_run": true` until that moment; the rehearsal reports honestly whether the
real thing would have been blocked.

---

## 8. After it is live

See [MAINTENANCE.md](MAINTENANCE.md). A live app is a commitment, not a finish
line — pages get redesigned, services change their rules, and a watcher that
quietly stopped matching its target looks exactly like a watcher with nothing
to report.
