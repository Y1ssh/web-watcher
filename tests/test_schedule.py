"""The schedule is verified with a fake clock, so the tests take no real time.

A separate end-to-end test runs the real loop briefly against a real server;
this file covers the timing arithmetic and the failure behaviour exhaustively.
"""

import unittest

from watcher.schedule import next_delay, run_schedule


class FakeClock:
    """A clock that only advances when something sleeps or a run takes time."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NextDelayTests(unittest.TestCase):
    def test_waits_the_remainder_of_the_interval(self):
        # Started at 0, three seconds in, ten second interval: seven to go.
        self.assertAlmostEqual(next_delay(0.0, 3.0, 10.0), 7.0)

    def test_a_slow_run_does_not_push_the_next_slot_out(self):
        # Slots are measured from the start, so run 2 still lands at t=10 even
        # though run 1 took nine seconds.
        self.assertAlmostEqual(next_delay(0.0, 9.0, 10.0), 1.0)

    def test_missed_slots_are_skipped_rather_than_fired_back_to_back(self):
        # A run that overran three whole intervals must resume at the next
        # future slot, not fire three times immediately to catch up.
        self.assertAlmostEqual(next_delay(0.0, 35.0, 10.0), 5.0)

    def test_landing_exactly_on_a_slot_waits_a_full_interval(self):
        self.assertAlmostEqual(next_delay(0.0, 20.0, 10.0), 10.0)

    def test_the_delay_is_always_positive(self):
        for elapsed in (0.0, 0.1, 9.99, 10.0, 10.01, 999.0):
            self.assertGreater(next_delay(0.0, elapsed, 10.0), 0.0)


class RunScheduleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.runs = 0

    def _count(self):
        self.runs += 1

    def test_runs_exactly_max_runs_times(self):
        completed = run_schedule(
            self._count,
            interval_seconds=10,
            max_runs=3,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )
        self.assertEqual(completed, 3)
        self.assertEqual(self.runs, 3)

    def test_sleeps_one_interval_between_runs(self):
        run_schedule(
            self._count,
            interval_seconds=10,
            max_runs=3,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )
        # Two gaps between three runs.
        self.assertEqual(self.clock.sleeps, [10.0, 10.0])

    def test_does_not_sleep_after_the_final_run(self):
        run_schedule(
            self._count,
            interval_seconds=10,
            max_runs=1,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )
        self.assertEqual(self.clock.sleeps, [])

    def test_a_slow_run_shortens_the_following_sleep(self):
        def slow():
            self.runs += 1
            self.clock.advance(4.0)

        run_schedule(
            slow,
            interval_seconds=10,
            max_runs=2,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )
        self.assertEqual(self.clock.sleeps, [6.0])

    def test_max_runs_zero_does_nothing(self):
        completed = run_schedule(
            self._count,
            interval_seconds=10,
            max_runs=0,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )
        self.assertEqual(completed, 0)
        self.assertEqual(self.runs, 0)

    def test_interval_must_be_positive(self):
        with self.assertRaises(ValueError):
            run_schedule(self._count, interval_seconds=0, max_runs=1)

    def test_negative_max_runs_is_rejected(self):
        with self.assertRaises(ValueError):
            run_schedule(self._count, interval_seconds=10, max_runs=-1)


class FailureHandlingTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_a_failing_run_does_not_stop_the_watch(self):
        # One unreachable fetch must not end an unattended watch.
        attempts = []

        def flaky():
            attempts.append(len(attempts))
            if len(attempts) == 1:
                raise RuntimeError("site down")

        errors = []
        completed = run_schedule(
            flaky,
            interval_seconds=10,
            max_runs=3,
            on_error=errors.append,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )
        self.assertEqual(completed, 3)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(errors), 1)

    def test_a_failed_run_still_counts_toward_max_runs(self):
        def always_fails():
            raise RuntimeError("nope")

        completed = run_schedule(
            always_fails,
            interval_seconds=10,
            max_runs=2,
            on_error=lambda exc: None,
            sleeper=self.clock.sleep,
            clock=self.clock,
        )
        self.assertEqual(completed, 2)

    def test_without_a_handler_the_error_propagates(self):
        def always_fails():
            raise RuntimeError("nope")

        with self.assertRaises(RuntimeError):
            run_schedule(
                always_fails,
                interval_seconds=10,
                max_runs=2,
                sleeper=self.clock.sleep,
                clock=self.clock,
            )

    def test_keyboard_interrupt_is_never_swallowed(self):
        # Ctrl+C must stop the watch even though other exceptions are handled.
        def interrupted():
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            run_schedule(
                interrupted,
                interval_seconds=10,
                max_runs=2,
                on_error=lambda exc: None,
                sleeper=self.clock.sleep,
                clock=self.clock,
            )


if __name__ == "__main__":
    unittest.main()
