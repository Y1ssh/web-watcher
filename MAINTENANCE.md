# Keeping Web Watcher alive

Launching is not the finish line. A live app keeps asking for attention, and
the failure mode that costs you most is silent: a watcher whose target page was
redesigned looks exactly like a watcher with nothing to report.

---

## The failure that looks like success

Three things break quietly:

- **The page changes shape.** A redesign renames the element you extract. The
  watcher now errors on every run — or worse, matches a different element and
  reports "unchanged" forever about something you do not care about.
- **The storage stops persisting.** A host migration, a plan change, or a
  dropped volume, and suddenly every run is a baseline again. See
  [DEPLOY.md](DEPLOY.md) section 1.
- **A credential expires or gets rotated elsewhere.** The check keeps working;
  only the alert fails, and it fails into a log nobody reads.

None of these announce themselves. That is why the review below is scheduled
rather than triggered.

---

## The monthly review

Thirty minutes, once a month, is far cheaper than one emergency rescue after
six months of neglect.

**1. Confirm it is still reaching the target and completing its cycle.**

```bash
python -m watcher show --log 20
```

Look for recent `"event": "check"` entries with `"status": 200`. A run of
`"event": "error"` entries is the answer to a question you did not know to ask.
Check that `check_count` is climbing and that `first_seen_at` is old — if it
keeps resetting, your storage is not persistent.

**2. Re-run the checklist.**

```bash
python -m watcher preflight
```

The `target` check re-fetches the live page and shows the value it read. Look
at it. Is it still the thing you meant to watch?

**3. Ask the agent to look for drift.**

> Review this project for anything that no longer matches how the target
> website behaves, and for any part of the config that has drifted from what
> the site now returns.

**4. Update `CLAUDE.md` and commit it.** If an architectural decision changed,
the file has to change with it. An out-of-date project memory is worse than
none, because the agent will trust it completely.

```bash
git commit -am "Document the new extractor after the site redesign"
```

Every edit to the project's memory is then dated, described, and reversible.

---

## The maintenance cliff

As a codebase grows it eventually exceeds what the model can hold in its
context window at once. Past that point the agent changes things without
seeing the whole picture, and each change carries a little more risk than the
last. That accumulation is technical debt, and for an agent-built project it
arrives faster than you would expect.

What keeps this project on the right side of that line:

- **`CLAUDE.md` is the map.** It summarises the architecture and the rules, so
  the agent can act sensibly without reading every file. Keep it current and
  keep it in version control.
- **Modules stay small and single-purpose.** `detect.py` and `conditions.py`
  are pure functions with no I/O; `checker.py` is the only place the pieces are
  wired together. You can reason about one without loading the rest.
- **The tests are the safety net.** 498 of them, no network required. If a
  change is safe, they say so in about two minutes.

If the project keeps growing, split it rather than letting one part sprawl. And
if maintaining it starts costing more time than the app is worth, retiring it is
a legitimate choice, not a failure. The goal was a useful tool, not an immortal
one.

---

## Working with the agent on this project

### Spend

You are billed by tokens — everything the agent reads and writes, including
your instructions, the files it opens, and its own replies. A large project
with many files burns through them quickly.

- **Route work to the right model.** Send routine edits, renames, and
  formatting to a smaller, cheaper model. Save the capable one for designing
  behaviour, debugging confusing failures, and planning multi-step changes. Use
  `/model` inside Claude Code to switch.
- **Turn effort up for hard planning, down for busywork.** "Design a reliable
  way to detect a price change" deserves it; "fix this typo" does not.
- **Point at files explicitly.** `In @watcher/detect.py, the comparison...`
  keeps the context window tight and the edits accurate, instead of making the
  agent re-read the project to guess where you meant.
- **Watch the meter.** Run `/usage` now and then, not once at the end.

### When the agent goes in circles

A **revert loop** is the agent fixing something, deciding the fix was wrong,
undoing it, and redoing it — burning tokens without getting closer. The signs:
the same file flipping between two versions, repeated "let me revert that", no
progress.

Do not wait for it to sort itself out. Stop it, run `/rewind` to return to a
known-good state, look at what was correct there, and give one specific
instruction instead of the vague one that caused the loop.

`CLAUDE.md` already carries anti-looping rules for this project: *if a change
already works, do not undo it*, and *do not change the same file back and forth
— if unsure, stop and ask*.

### When the conversation gets muddy

Long sessions accumulate dead ends and contradictions until the agent starts
contradicting itself and ignoring rules you set an hour ago. When re-instructing
stops helping:

1. Ask for a summary: *what currently works, what is left, what we have settled*.
2. Save it — a note, or straight into `CLAUDE.md`.
3. Run `/clear` to reset the conversation. Your files are untouched.
4. Paste the summary back as the starting point.

`/compact` is the lighter version: use it mid-task when you want continuity
plus a cleanup. `/clear` is for moving to genuinely unrelated work.

Because `CLAUDE.md` reloads automatically every session, even a full `/clear`
leaves the agent knowing this project's architecture and rules. Persistent
memory in the file, disposable memory in the window — that pairing is what lets
you clean context aggressively without losing anything that matters.

---

## Whose fault it is when it goes wrong

Yours. If the app takes a harmful action, the answer to "who is responsible" is
the person who deployed it, not the model that wrote the code. That is the whole
reason for the verification habits in this project: the dry-run default, the
`test-notify` command, the checklist that actually runs, the insistence on
looking at the stored value with your own eyes.

Licensing and ownership of AI-generated code are unsettled. So is the long-term
maintainability question you meet at the cliff above. None of that should stop
you building. It should keep you engaged, skeptical, and willing to check.
