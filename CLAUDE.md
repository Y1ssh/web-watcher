# CLAUDE.md — project memory for Web Watcher

Read this before touching anything. It is the onboarding document for this
project: what it is, how it is built, and the rules you must follow here.

## What this project is

A website watcher. It fetches one page, reduces it to a single string, compares
that string against the one stored from the previous run, and reports whether it
changed. Built in three slices:

- **Slice 1 (done): monitor and report.** Fetch, extract, compare, store, log,
  schedule.
- **Slice 2 (done): notify and act.** Three-valued conditions, notification
  channels, and guarded actions. Credentials moved to environment variables
  here rather than waiting for slice 3, because a channel cannot send without
  one and the wrong habit is hard to undo later.
- **Slice 3 (done): control and deploy.** A pre-launch checklist that runs, a
  secret scanner, CI across Python 3.10-3.13, a container image, a Render
  blueprint, and settings that can arrive as an environment variable.
  [DEPLOY.md](DEPLOY.md) and [MAINTENANCE.md](MAINTENANCE.md) carry the
  operational guidance.

All three slices are complete. New work is a change to a finished project, not
a continuation of a slice: propose it, and keep the decisions below intact
unless we agree otherwise.

## Architecture decisions

These are settled. Changing one is a conversation, not a refactor.

- **Standard library only.** No third-party packages. Fetching is `urllib`,
  parsing is `html.parser`, tests are `unittest`. This keeps the project
  installable anywhere with nothing to patch. Adding a dependency needs a
  reason and my approval.
- **The comparison target is one normalised string, never raw HTML.** Raw
  markup churns on every request and would report a change constantly.
- **`detect.py` is pure.** No network, no disk, no clock — everything arrives as
  an argument. Keep it that way; it is why every outcome can be tested.
- **The snapshot is human-readable JSON.** You must be able to open
  `state/snapshot.json` and see what the app recorded. That manual check is how
  a watcher reading the wrong part of a page gets caught.
- **A corrupt snapshot raises; it never silently re-baselines.** A watcher that
  quietly resets would report "first run" forever and never detect a change.
- **The snapshot carries a fingerprint of `url` + `extractor`.** When the config
  is retargeted, the watcher re-baselines instead of announcing a change that
  never happened on the site.
- **Only `http` and `https` are ever opened**, on the first request and on every
  redirect. Left unguarded, urllib also opens `file://` and `ftp://`.
- **Config paths resolve against the config file, not the shell's directory.** A
  scheduler launches the app from wherever it likes.
- **Every checkable mistake fails at startup.** Unknown config keys, bad regexes,
  and impossible element targets are rejected in `config.py` / `extract.py`
  before the first fetch, not halfway through an unattended run.
- **Failure to extract is an error, never an empty string.** An empty string
  would look exactly like a change on the next run.

Added in slice 2:

- **Conditions are three-valued: true, false, unknown.** Unknown means the rule
  genuinely could not be evaluated. It never alerts as a match and never acts.
  Collapsing it into false would hide a broken watcher; into true would fire on
  a value nobody could read.
- **Every condition returns a plain-English reason.** It goes on screen, into
  the run log, and into the alert. Reasoning you cannot read is reasoning you
  cannot check.
- **Number parsing refuses ambiguous input.** `12,5` is unknown, not 12 and not
  12.5. A wrong number here fires a real alert or a real action.
- **Alerts de-duplicate by value, not by run.** A standing condition stays true
  on every check; without this a 15-minute schedule sends the same alert 96
  times a day.
- **An alert nobody received is not recorded as sent.** If every channel fails,
  the next run tries again.
- **A channel that fails never ends the watch.** Failures are collected, not
  raised.
- **`dry_run` defaults to on, and nothing can turn it off but the config.**
  There is deliberately no `--live` flag. `--dry-run` may only make a run safer.
- **Blocking guards are evaluated before the dry-run check**, so a rehearsal
  reports honestly whether the real thing would have been stopped.
- **Only a successful real action is recorded.** Dry runs, blocked runs, and
  failed attempts leave the guards untouched.
- **Action guards live in the snapshot**, so restarting cannot unlock them.
- **An unanswered confirmation prompt means no.** EOF, no terminal, or a closed
  stdin all decline; none of them proceed.
- **Credentials are never written into config.json.** The config names an
  environment variable. Inline `url`/`password`/`token`/`secret`/`api_key` keys
  are rejected with an error, not merely discouraged.
- **A real environment variable always beats `.env`.** A stray local file must
  not override a deployment's platform settings.
- **The snapshot is written once per check**, carrying the change, the alert
  bookkeeping, and the action bookkeeping together. A half-written state would
  re-alert on the next run.
- **State v1 files are upgraded on read, not rejected.** Losing the baseline on
  an upgrade would mean missing the next real change.

Added in slice 3:

- **The pre-launch checklist is code, not prose.** A checklist you have to
  remember to follow is one you will eventually skip. `preflight` exits non-zero
  so a deploy script can refuse.
- **Preflight distinguishes FAIL from WARN.** FAIL means it will not work or
  will do something unintended. WARN means it works but you owe it a decision.
  Warnings never block; padding the FAIL list would teach people to ignore it.
- **The snapshot's durability is a first-class check.** Most hosts give a
  deployment a fresh filesystem. A watcher that loses its snapshot re-baselines
  forever and never reports a change, with no error anywhere. This is the
  single most likely way a deployment of this app fails.
- **The secret scanner tolerates placeholders on purpose.** A scanner with a
  high false-positive rate gets ignored within a day, which is worse than none.
  It is a net, not a proof -- never describe a clean scan as a guarantee.
- **`scan-secrets` runs without a config**, because that is exactly the
  situation in CI: `config.json` is git-ignored and absent from a checkout.
- **CI tests the Python floor the README claims.** 3.10 is in the matrix so the
  claim is verified rather than hopeful. If you raise the floor, change both.
- **An explicit `--config` always beats `WATCHER_CONFIG`.** A deployment
  variable must never silently shadow a file someone deliberately pointed at.
- **The startup line and the HTTP status exist for log dashboards.** After a
  deploy the only evidence available is the host's log; it has to show that the
  app started, found its settings, and actually reached the site.

## Behavioural rules

- **Propose a plan before editing code.** Say which files you will touch and
  why, and wait for me to agree.
- **Never delete files without asking me first.**
- **Never install a package, or add a dependency, without asking me first.**
- **Never commit `config.json`, `state/`, `logs/`, or `.env`.** They are in
  `.gitignore` and belong there.
- **Never send a real notification or perform a real action while testing.**
  Tests use local servers and injected fakes. Do not point a test at a real
  webhook, mail server, or form.
- **Never turn off a safeguard to make something work.** If `dry_run` or
  `run_once` is in the way, that is the guard doing its job; ask me.
- **Never print a secret.** Use `secrets.redact` if a value must be shown at all.
- **Run the tests and show me the output before claiming anything works.** Do
  not report a task complete on the basis that the code looks right.
- **When you add behaviour, add a test for it in the same change.**
- **Do not write dates into code, comments, or docs.** Timestamps generated at
  runtime are fine; authored dates go stale and start lying.
- **If a change already works, do not undo it.** Confirm with me before
  reverting anything, and do not flip the same file back and forth.
- **Ask rather than guess.** If a requirement is ambiguous, stop and ask. A
  plausible guess that is wrong costs more than a question.
- **Keep `CLAUDE.md` current.** When an architectural decision changes, update
  this file in the same commit. An out-of-date memory is worse than none,
  because it will be trusted completely.

## Rule priority

When two rules conflict, follow this order and then tell me what you hit:

1. **Safety** — do not delete, do not send anything outside this machine, do not
   commit secrets. These always win.
2. **Correctness** — tests pass, and the behaviour is verified by observation
   rather than assumed.
3. **Scope** — stay inside the slice being worked on.
4. **Convenience** — tidiness, formatting, refactors.

If following a safety rule blocks a task, stop and ask. Do not resolve the
conflict on your own.

## Verifying work in this project

```bash
python -m unittest discover -v      # all tests
python -m watcher preflight         # the pre-launch checklist
python -m watcher scan-secrets      # credentials in tracked files
python -m watcher check             # one live check
python -m watcher test-notify       # prove the alert path without a real change
python -m watcher show --log        # what was stored, and recent runs
python -m watcher watch --interval 1 --max-runs 3   # prove the schedule fires
```

A green test run is the objective evidence. "It looks right" is not.
