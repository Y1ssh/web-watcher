# Web Watcher

Watches one value on one web page, tells you when it changes in a way you care
about, and — if you ask it to — does something about it.

Point it at a URL, say which part of the page matters, write the rule that makes
a change worth hearing about, and it fetches, compares, decides, alerts, and
optionally acts. Everything it records is plain JSON you can open and read.

This is **slice 2 of 3**. Slice 1 monitored and reported; slice 2 adds
conditions, notifications, and guarded actions. Slice 3 brings production
hardening and always-on hosting.

---

## Requirements

- **Python 3.10 or newer.** Developed and tested on 3.13.15.
- **No third-party packages.** Everything comes from the standard library, so
  there is nothing to install and nothing to keep patched.

## Quickstart

```bash
cp config.example.json config.json
```

Edit `config.json` to point at the page you care about, then:

```bash
python -m watcher check
```

The first run records a baseline. Later runs compare against it:

```
CHANGED
  url:   http://example.com/widget
  from:  $75.00
  to:    $49.99
  saved: <timestamp>
  rule:  MET - 49.99 satisfies below 50.0
  alert: console: sent
  alert: file(logs/notifications.log): sent
```

To keep checking on a schedule:

```bash
python -m watcher watch
```

Before trusting an alert path, prove it works without waiting for a real change:

```bash
python -m watcher test-notify
```

---

## Configuration

`config.json` lives next to this README. Unknown keys are rejected rather than
ignored, so a typo fails loudly instead of silently falling back to a default.

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `url` | yes | — | The page to watch. Must be `http://` or `https://`. |
| `extractor` | yes | — | Which part of the page to watch. |
| `condition` | no | `{"kind":"changed"}` | Which changes are worth acting on. |
| `notify` | no | console only | Where alerts go. |
| `action` | no | none | What to do when the condition is met. |
| `interval_seconds` | no | `900` | How often `watch` checks. |
| `timeout_seconds` | no | `20` | How long to wait for the page. |
| `user_agent` | no | a web-watcher string | Sent with every request. |
| `state_path` | no | `state/snapshot.json` | Where the snapshot is stored. |
| `log_path` | no | `logs/runs.jsonl` | Where each run is recorded. |

Relative paths resolve against the config file, not your shell's working
directory, so a scheduler launching the app from anywhere still reads and
writes the same files.

### Extractors

Every extractor returns one string. Raw HTML is never compared directly — it
churns constantly (session ids, analytics tags, reordered attributes) and would
report a change on nearly every run.

```json
{ "kind": "element", "tag": "div", "id": "price" }
{ "kind": "element", "tag": "span", "class": "status", "index": 2 }
{ "kind": "contains", "needle": "In Stock" }
{ "kind": "regex", "pattern": "\\$([\\d,]+\\.\\d{2})", "group": 1 }
{ "kind": "full_text" }
```

`contains` returns `"present"` or `"absent"`. `regex` supports numbered and
named groups, and **fails** rather than returning an empty string when the
pattern matches nothing — an empty string would look exactly like a change on
the next run.

Text handling is the same for all four: `<script>`, `<style>`, `<noscript>` and
`<template>` contents are dropped, entities are decoded, `&nbsp;` collapses to a
normal space, and inline tags do not split a value — `$1<span>,299</span>.00`
reads as `$1,299.00`, not `$1 ,299 .00`.

### Conditions

A condition decides whether a change is one you actually want to hear about.
With none set, the watcher behaves as it did in slice 1: it reacts to any change.

```json
{ "kind": "changed" }
{ "kind": "contains", "text": "In Stock" }
{ "kind": "equals", "text": "Available" }
{ "kind": "matches", "pattern": "^Available" }
{ "kind": "number", "below": 50 }
{ "kind": "number", "above": 10, "below": 100 }
```

Combine them with `all`, `any`, and `not`:

```json
{
  "kind": "all",
  "conditions": [
    { "kind": "changed" },
    { "kind": "number", "below": 50 }
  ]
}
```

**Every decision explains itself.** The reasoning appears on screen, in the run
log, and in the alert body, so you can check the logic instead of trusting it:

```
rule:  MET - 49.99 satisfies below 50.0
rule:  not met - 75.0 is not below 50.0
```

**Conditions are three-valued.** Beyond true and false there is *unknown*, for
when the condition genuinely cannot be evaluated — usually a number that cannot
be read out of a messy page. Unknown never triggers an alert-as-match and never
triggers an action. Instead you get a warning saying what went wrong:

```
rule:  UNKNOWN - could not read a clear number from 'Currently unavailable',
       so the condition was not triggered
```

Number parsing ignores currency symbols and thousands separators, so `$1,299.00`
reads as `1299.0`. It refuses to guess at genuinely ambiguous input: `12,5` is
twelve-and-a-half in much of Europe and a typo elsewhere, so it returns unknown.
Set `"decimal_comma": true` for pages that really do use European notation.

### Notifications

```json
"notify": {
  "channels": [
    { "kind": "console" },
    { "kind": "file", "path": "logs/notifications.log" },
    { "kind": "webhook", "url_env": "WATCHER_WEBHOOK_URL" },
    {
      "kind": "email",
      "to": "me@example.com",
      "from": "watcher@example.com",
      "host": "smtp.example.com",
      "port": 587,
      "username_env": "WATCHER_SMTP_USER",
      "password_env": "WATCHER_SMTP_PASSWORD"
    }
  ],
  "subject": "Web Watcher: {condition_reason}",
  "body": "{url}\n\nnow: {value}\nbefore: {previous_value}",
  "once_per_value": true,
  "warn_on_unknown": true
}
```

The `webhook` channel POSTs JSON and works with Slack, Discord, Teams, or your
own endpoint. Set `text_key` to match the service (`text` for Slack and Teams,
`content` for Discord).

Templates accept `{url}`, `{value}`, `{previous_value}`, `{outcome}`,
`{condition}`, `{condition_reason}`, and `{at}`. An unknown placeholder is left
visible rather than raising — a typo in a template should not cost you the alert.

**`once_per_value` is the alert-overload guard.** A condition like "price below
50" stays true on every later check; without de-duplication a fifteen-minute
schedule would re-send the same alert ninety-six times a day. With it on (the
default) you are told once per distinct value, and again when the value changes.

If every channel fails, the alert is **not** marked as delivered, so the next
run tries again rather than treating it as sent.

### Actions

An action does something in the world when the condition is met. This is the
part that can cause real harm, so the defaults are deliberately timid.

```json
"action": {
  "kind": "http_request",
  "method": "POST",
  "url": "https://example.com/book",
  "fields": { "name": "Jane Doe", "slot": "morning" },
  "encoding": "form",
  "headers": { "Authorization": "Bearer ${WATCHER_ACTION_TOKEN}" },
  "safeguards": {
    "dry_run": true,
    "run_once": true,
    "max_per_24h": 1,
    "hours": [9, 17],
    "confirm": false
  }
}
```

| Safeguard | Default | What it does |
| --- | --- | --- |
| `dry_run` | `true` | Rehearse everything, send nothing. |
| `run_once` | `true` | After one real run, never again until you reset it. |
| `max_per_24h` | `1` | Rolling 24-hour limit, not a calendar day. |
| `hours` | none | Only inside this local-time window. Wraps past midnight. |
| `confirm` | `false` | Ask on the terminal first, and treat silence as no. |

Every run says plainly, before anything happens, which mode it is in:

```
SANDBOX ON - the action will be rehearsed, nothing will really be sent
LIVE - the action WILL really be performed: POST https://example.com/book (name, slot)
```

Some deliberate properties:

- **`--dry-run` can only make a run safer.** There is no `--live` flag. The only
  way to perform a real action is to set `"dry_run": false` in the config.
- **Blocking guards run *before* the dry-run check**, so a rehearsal tells you
  truthfully whether the real thing would have been stopped.
- **Only a successful real run is recorded.** A dry run, a blocked run, and a
  failed attempt all leave the guards untouched.
- **The guards live in the snapshot, not in memory**, so restarting the process
  cannot unlock them.
- **A failed action does not fail the check.** The page was still read and the
  snapshot still saved.

Note that an action is guarded by `run_once` and `max_per_24h`, not by the
per-value de-duplication that notifications use. With a standing condition like
"price below 50" and both guards disabled, the action would fire on every check.
That combination requires explicitly turning off two defaults.

### Secrets

**No credential is ever written into `config.json`.** The config names an
environment variable; the value is read at run time. Configs get copied, pasted,
and committed, and a Slack webhook URL in one is a leaked credential.

This is enforced, not merely advised: a `url`, `password`, `token`, `secret`, or
`api_key` key in a channel or action config is rejected with an error telling you
to use the `_env` form instead.

```bash
cp .env.example .env
```

`.env` sits next to your config, is git-ignored, and is read automatically. A
value already set in your real environment always wins, so a stray local file
cannot override a deployment's platform settings.

---

## Commands

| Command | What it does |
| --- | --- |
| `python -m watcher check` | One check, right now. |
| `python -m watcher watch` | Check repeatedly. Ctrl+C stops it. |
| `python -m watcher test-notify` | Send a sample alert now, labelled `[TEST]`. |
| `python -m watcher show` | Print the stored snapshot without fetching. |
| `python -m watcher reset --yes` | Delete the snapshot so the next check re-baselines. |
| `python -m watcher reset --action-only --yes` | Clear the action guards, keeping the value baseline. |

Useful flags:

- `--config PATH` — use a different config file.
- `--url URL` — override the configured URL for one run.
- `--dry-run` (`check`, `watch`) — force rehearsal mode for this run.
- `--interval SECONDS` (`watch`) — override the interval.
- `--max-runs N` (`watch`) — stop after N runs.
- `--json` (`show`) — print the raw stored snapshot.
- `--log [N]` (`show`) — also print the last N run-log entries.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command completed. For `check`, this covers every outcome. |
| `1` | An unexpected internal error. |
| `2` | The configuration or the stored snapshot could not be used. |
| `3` | The page could not be fetched, the value was not on it, or every notify channel failed during `test-notify`. |

`check` exits `0` whether the page changed or not, and whether an action was
blocked or not — those are normal results, not errors.

---

## Outcomes

| Outcome | Meaning |
| --- | --- |
| `BASELINE` | Nothing was stored, so this run only recorded a starting point. |
| `UNCHANGED` | The value matches what was stored. |
| `CHANGED` | The value differs from what was stored. |
| `RETARGETED` | The config now watches something else, so the old value is not comparable. The baseline resets instead of firing a false alarm. |

`RETARGETED` covers editing `url` or `extractor`. Editing a *condition* or a
*channel* does not reset anything — it changes how the value is judged, not what
is being measured.

---

## Verifying it actually works

### 1. The automated tests

```bash
python -m unittest discover -v
```

398 tests, no internet access required. The end-to-end tests run against real
HTTP servers started on localhost, so a test can watch a page change, see the
alert fire, and confirm the action really did or did not post.

### 2. Prove the alert path before you rely on it

```bash
python -m watcher test-notify
```

Sends a sample through every configured channel, labelled `[TEST]`, without
touching the site or the snapshot. Then check: did it arrive, did it land in
spam, does it contain the values you asked for?

### 3. A state comparison you do by hand

```bash
python -m watcher check
python -m watcher show
```

Confirm the stored value is genuinely what you meant to watch. Then edit
`state/snapshot.json`, change `value` to something wrong, and run `check` again.
It should report `CHANGED` and write the correct value back.

### 4. Prove the schedule fires

```bash
python -m watcher watch --interval 1 --max-runs 3
python -m watcher show --log
```

Three log entries roughly a second apart is the evidence.

### 5. Rehearse the action before letting it loose

Leave `dry_run` on and watch a full cycle complete. The rehearsal reports any
guard that would have blocked the real thing, so a clean dry run means the real
one would have gone through.

---

## How it fits together

```
config.py       reads and validates config.json; every checkable mistake
                fails here, at startup, not mid-run
secrets.py      environment variables and the optional .env file
fetching.py     downloads the page over http/https only
extract.py      reduces the HTML to one string
state.py        reads and atomically writes the stored snapshot
detect.py       pure comparison: baseline / unchanged / changed / retargeted
conditions.py   pure three-valued rules, each with a plain-English reason
notify.py       console, file, webhook, and email channels
actions.py      the guarded HTTP request
checker.py      one full cycle: fetch, extract, compare, decide, alert, act
schedule.py     repeats a check on a fixed interval without drifting
runlog.py       appends one JSON object per run
cli.py          the command line
```

`detect.py` and `conditions.py` touch no network, no disk, and no clock —
everything arrives as an argument — so every outcome is tested exhaustively
without a live website.

The snapshot is written **once**, at the end of a check, carrying the change,
the notification bookkeeping, and the action bookkeeping together. A half-written
state — value updated but "already alerted" not recorded — would re-alert on the
next run.

## Where the files go

- `state/snapshot.json` — the last value, plus alert and action bookkeeping.
- `logs/runs.jsonl` — one JSON object per run, appended forever.
- `logs/notifications.log` — alerts, if the `file` channel is configured.
- `.env` — your credentials.

All are machine-local and git-ignored, along with `config.json` itself.

## Deliberate limits in this slice

- `watch` only runs while the process runs. Always-on hosting is slice 3.
- Pages that build their content with JavaScript after loading will not work.
  The watcher reads the HTML the server sends, which is what a browser receives
  before scripts run.
- An action is an HTTP request. That covers plain HTML forms and APIs, but not
  clicking a button on a JavaScript-driven page — that needs a real browser, and
  therefore a third-party dependency this project has so far avoided. Worth a
  deliberate decision rather than a quiet one.
