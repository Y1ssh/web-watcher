# CLAUDE.md — project memory for Web Watcher

Read this before touching anything. It is the onboarding document for this
project: what it is, how it is built, and the rules you must follow here.

## What this project is

A website watcher. It fetches one page, reduces it to a single string, compares
that string against the one stored from the previous run, and reports whether it
changed. Built in three slices:

- **Slice 1 (done): monitor and report.** Fetch, extract, compare, store, log,
  schedule, and a full test suite.
- **Slice 2 (not started): notify and act.** Notifications when a change
  happens, plain-English conditions that decide when a change matters, and
  optional actions with a dry-run mode and a run-once guard.
- **Slice 3 (not started): control and deploy.** Secrets in environment
  variables, CI, and always-on hosting.

Do not build slice 2 or slice 3 work into slice 1 files without being asked.

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

## Behavioural rules

- **Propose a plan before editing code.** Say which files you will touch and
  why, and wait for me to agree.
- **Never delete files without asking me first.**
- **Never install a package, or add a dependency, without asking me first.**
- **Never commit `config.json`, `state/`, `logs/`, or `.env`.** They are in
  `.gitignore` and belong there.
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
python -m watcher check             # one live check
python -m watcher show --log        # what was stored, and recent runs
python -m watcher watch --interval 1 --max-runs 3   # prove the schedule fires
```

A green test run is the objective evidence. "It looks right" is not.
