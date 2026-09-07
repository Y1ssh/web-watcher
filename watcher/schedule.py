"""Run a check repeatedly on a fixed interval.

The clock and the sleep function are arguments so the whole loop can be tested
in microseconds instead of by waiting for real minutes to pass.
"""

import math
import time
from collections.abc import Callable


def next_delay(
    started: float, now: float, interval_seconds: float
) -> float:
    """Seconds to wait so the next run lands on the next scheduled slot.

    Slots are measured from the moment the loop started rather than from the
    end of the last run, so a check that takes eight seconds does not push
    every later run eight seconds further out.

    Slots already missed are skipped. If a run overruns its interval, the loop
    resumes at the next future slot instead of firing repeatedly to catch up.
    """
    elapsed = now - started
    slot = math.floor(elapsed / interval_seconds) + 1
    return (started + slot * interval_seconds) - now


def run_schedule(
    check: Callable[[], None],
    *,
    interval_seconds: float,
    max_runs: int | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Call `check` every `interval_seconds`. Returns the number of runs done.

    `max_runs` stops the loop after that many runs, which is how the schedule
    gets verified without waiting out a production interval.

    A run that raises is passed to `on_error` and the loop continues -- one
    unreachable fetch should not end an unattended watch. With no `on_error`
    the exception propagates instead. KeyboardInterrupt is never swallowed.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be greater than zero")
    if max_runs is not None and max_runs < 0:
        raise ValueError("max_runs cannot be negative")
    if max_runs == 0:
        return 0

    started = clock()
    completed = 0
    while True:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - a watcher must survive a bad run
            if on_error is None:
                raise
            on_error(exc)
        completed += 1

        if max_runs is not None and completed >= max_runs:
            return completed

        delay = next_delay(started, clock(), interval_seconds)
        if delay > 0:
            sleeper(delay)
