# Web Watcher

Watches one value on one web page and tells you when it changes.

Point it at a URL, say which part of the page you care about, and it fetches
that page, reduces it to a single string, and compares that string against what
it saw last time. It stores the result so you can open the file and check what
it actually recorded.

This is **slice 1 of 3**. It monitors and reports. Notifications and conditional
actions arrive in slice 2; production hardening and deployment in slice 3.

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

The first run has nothing to compare against, so it records a baseline:

```
BASELINE   first run - nothing to compare against yet
  url:   https://example.com/
  value: Example Domain
  saved: <timestamp>
```

Run it again and it reports `UNCHANGED`. When the page actually changes, it
reports `CHANGED` with both values:

```
CHANGED
  url:   https://example.com/product
  from:  Sold Out
  to:    In Stock
  saved: <timestamp>
```

To keep checking on a schedule:

```bash
python -m watcher watch
```

---

## Configuration

`config.json` lives next to this README. Unknown keys are rejected rather than
ignored, so a typo fails loudly instead of silently falling back to a default.

| Key | Required | Default | Meaning |
| --- | --- | --- | --- |
| `url` | yes | — | The page to watch. Must be `http://` or `https://`. |
| `extractor` | yes | — | Which part of the page to watch. See below. |
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

**`element`** — the text of the nth matching element.

```json
{ "kind": "element", "tag": "div", "id": "price" }
{ "kind": "element", "tag": "span", "class": "status", "index": 2 }
```

**`contains`** — whether some text appears on the page. Returns `"present"` or
`"absent"`, so you get an alert when a label flips.

```json
{ "kind": "contains", "needle": "In Stock" }
{ "kind": "contains", "needle": "In Stock", "case_sensitive": true }
```

**`regex`** — a pattern matched against the page's visible text.

```json
{ "kind": "regex", "pattern": "\\$([\\d,]+\\.\\d{2})", "group": 1 }
{ "kind": "regex", "pattern": "\\$(?P<price>[\\d.]+)", "group": "price" }
```

If the pattern matches nothing, the run **fails** rather than returning an empty
string. An empty string would look exactly like a change on the next run.

**`full_text`** — the whole page's visible text. Broad, and noisy on most real
sites, but useful when you genuinely want to know about any edit at all.

```json
{ "kind": "full_text" }
```

Text handling is the same for all four: `<script>`, `<style>`, `<noscript>` and
`<template>` contents are dropped, entities are decoded, `&nbsp;` collapses to a
normal space, and inline tags do not split a value — `$1<span>,299</span>.00`
reads as `$1,299.00`, not `$1 ,299 .00`.

---

## Commands

| Command | What it does |
| --- | --- |
| `python -m watcher check` | One check, right now. |
| `python -m watcher watch` | Check repeatedly on `interval_seconds`. Ctrl+C stops it. |
| `python -m watcher show` | Print the stored snapshot without fetching anything. |
| `python -m watcher reset --yes` | Delete the snapshot so the next check re-baselines. |

Useful flags:

- `--config PATH` — use a different config file.
- `--url URL` — override the configured URL for one run.
- `--interval SECONDS` (`watch`) — override the interval.
- `--max-runs N` (`watch`) — stop after N runs instead of running until stopped.
- `--json` (`show`) — print the raw stored snapshot.
- `--log [N]` (`show`) — also print the last N run-log entries.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The command completed. For `check`, this covers every outcome. |
| `1` | An unexpected internal error. |
| `2` | The configuration or the stored snapshot could not be used. |
| `3` | The page could not be fetched, or the watched value was not on it. |

`check` exits `0` whether the page changed or not — a change is a normal result,
not an error. Use the run log or `show` to find out what happened.

---

## Outcomes

Each check reports exactly one of four things:

| Outcome | Meaning |
| --- | --- |
| `BASELINE` | Nothing was stored, so this run only recorded a starting point. |
| `UNCHANGED` | The value matches what was stored. |
| `CHANGED` | The value differs from what was stored. |
| `RETARGETED` | The config now watches something else, so the old value is not comparable. The baseline resets instead of firing a false alarm. |

`RETARGETED` exists because editing `url` or `extractor` makes the stored value
meaningless. Comparing across that edit would announce a change that never
happened on the site.

A change is reported **once**. After a value moves from `Sold Out` to
`In Stock`, later runs report `UNCHANGED` until it moves again.

---

## Verifying it actually works

Code that looks finished and code that works are different things. Two checks
prove this one behaves.

### 1. The automated tests

```bash
python -m unittest discover -v
```

180 tests, no network access required — the end-to-end tests run against a real
HTTP server started on localhost. They cover the three cases that matter most:
a first run saves a baseline and reports no change; a matching value reports no
change; a differing value reports a change.

### 2. A state comparison you do by hand

The snapshot is plain JSON. Read it yourself:

```bash
python -m watcher check
python -m watcher show
```

Confirm the stored value is genuinely the thing you meant to watch — not the
site's menu, not a cookie banner. Then force a change:

```bash
python -m watcher show --json
```

Edit `state/snapshot.json`, change `value` to something wrong, and run
`check` again. It should report `CHANGED` and write the correct value back.
That is observable proof the comparison and the save both work.

### 3. Proving the schedule fires

Do not wait fifteen minutes to find out whether a fifteen-minute schedule works.
Shorten it, watch the log fill, then restore it:

```bash
python -m watcher watch --interval 1 --max-runs 3
python -m watcher show --log
```

Three log entries roughly a second apart is the evidence. Each entry carries a
timestamp, the outcome, and the value.

---

## How it fits together

```
config.py     reads and validates config.json; every checkable mistake
              fails here, at startup, not mid-run
fetching.py   downloads the page over http/https only, on the first
              request and on every redirect
extract.py    reduces the HTML to one string
state.py      reads and atomically writes the stored snapshot
detect.py     pure comparison: baseline / unchanged / changed / retargeted
checker.py    one full cycle: fetch, extract, compare, store, log
schedule.py   repeats a check on a fixed interval without drifting
runlog.py     appends one JSON object per run
cli.py        the command line
```

`detect.py` touches no network, no disk, and no clock — everything arrives as an
argument — so every outcome is tested exhaustively without a live website.

## Where the files go

- `state/snapshot.json` — what the watcher saw last, plus check and change
  counts. Deleting it re-baselines.
- `logs/runs.jsonl` — one JSON object per run, appended forever. This is the
  evidence that a schedule actually fired.

Both are machine-local and ignored by git, along with `config.json` itself.

## Deliberate limits in this slice

- `watch` only runs while the process runs. Close the terminal and it stops.
  Always-on hosting is slice 3.
- Pages that build their content with JavaScript after loading will not work.
  The watcher reads the HTML the server sends, which is what a browser receives
  before scripts run.
- Nothing is sent anywhere. Notifications are slice 2.
