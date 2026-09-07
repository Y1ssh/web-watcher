"""The three cases the course insists on, plus the retarget case.

1. first run with no stored state saves a baseline and reports no change
2. a run whose value matches stored state reports no change
3. a run whose value differs reports a change

Nothing here touches the network or the disk, so every branch is covered.
"""

import unittest

from watcher.detect import Outcome, evaluate
from watcher.state import Snapshot, hash_value

FINGERPRINT = "fingerprint-a"
OTHER_FINGERPRINT = "fingerprint-b"
URL = "https://example.com/product"
T1 = "2000-01-01T00:00:00Z"
T2 = "2000-01-01T00:15:00Z"
T3 = "2000-01-01T00:30:00Z"


def snapshot(value: str, **overrides) -> Snapshot:
    base = {
        "url": URL,
        "fingerprint": FINGERPRINT,
        "value": value,
        "value_hash": hash_value(value),
        "first_seen_at": T1,
        "last_checked_at": T1,
        "last_changed_at": None,
        "check_count": 1,
        "change_count": 0,
    }
    base.update(overrides)
    return Snapshot(**base)


class FirstRunTests(unittest.TestCase):
    def test_no_stored_state_saves_a_baseline(self):
        result = evaluate(
            None, url=URL, fingerprint=FINGERPRINT, value="In Stock", now=T1
        )
        self.assertIs(result.outcome, Outcome.BASELINE)
        self.assertFalse(result.changed)
        self.assertIsNone(result.previous_value)
        self.assertEqual(result.snapshot.value, "In Stock")
        self.assertEqual(result.snapshot.check_count, 1)
        self.assertEqual(result.snapshot.change_count, 0)
        self.assertIsNone(result.snapshot.last_changed_at)


class UnchangedTests(unittest.TestCase):
    def test_matching_value_reports_no_change(self):
        result = evaluate(
            snapshot("In Stock"),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertIs(result.outcome, Outcome.UNCHANGED)
        self.assertFalse(result.changed)

    def test_check_count_rises_but_change_count_does_not(self):
        result = evaluate(
            snapshot("In Stock", check_count=4, change_count=2),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertEqual(result.snapshot.check_count, 5)
        self.assertEqual(result.snapshot.change_count, 2)

    def test_an_unchanged_run_still_records_the_check_time(self):
        result = evaluate(
            snapshot("In Stock"),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertEqual(result.snapshot.last_checked_at, T2)
        self.assertEqual(result.snapshot.first_seen_at, T1)

    def test_an_unchanged_run_preserves_the_previous_change_time(self):
        previous = snapshot("In Stock", last_changed_at=T1)
        result = evaluate(
            previous, url=URL, fingerprint=FINGERPRINT, value="In Stock", now=T2
        )
        self.assertEqual(result.snapshot.last_changed_at, T1)


class ChangedTests(unittest.TestCase):
    def test_differing_value_reports_a_change(self):
        result = evaluate(
            snapshot("Sold Out"),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertIs(result.outcome, Outcome.CHANGED)
        self.assertTrue(result.changed)
        self.assertEqual(result.previous_value, "Sold Out")
        self.assertEqual(result.value, "In Stock")

    def test_the_new_value_replaces_the_stored_one(self):
        result = evaluate(
            snapshot("Sold Out"),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertEqual(result.snapshot.value, "In Stock")
        self.assertEqual(result.snapshot.value_hash, hash_value("In Stock"))

    def test_both_counters_rise_and_the_change_time_is_recorded(self):
        result = evaluate(
            snapshot("Sold Out", check_count=3, change_count=1),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertEqual(result.snapshot.check_count, 4)
        self.assertEqual(result.snapshot.change_count, 2)
        self.assertEqual(result.snapshot.last_changed_at, T2)

    def test_first_seen_survives_a_change(self):
        result = evaluate(
            snapshot("Sold Out"),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertEqual(result.snapshot.first_seen_at, T1)

    def test_change_is_reported_once_not_on_every_later_run(self):
        # The value stays "In Stock" after the change, so the run after it must
        # be quiet. Without this, a schedule would alert on every single check.
        first = evaluate(
            snapshot("Sold Out"),
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        second = evaluate(
            first.snapshot,
            url=URL,
            fingerprint=FINGERPRINT,
            value="In Stock",
            now=T3,
        )
        self.assertIs(first.outcome, Outcome.CHANGED)
        self.assertIs(second.outcome, Outcome.UNCHANGED)


class RetargetTests(unittest.TestCase):
    def test_a_different_target_re_baselines_instead_of_reporting_a_change(self):
        # Pointing the config at another page must not fire an alert; the two
        # values were never measuring the same thing.
        result = evaluate(
            snapshot("Sold Out"),
            url="https://example.com/other",
            fingerprint=OTHER_FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertIs(result.outcome, Outcome.RETARGETED)
        self.assertFalse(result.changed)

    def test_retargeting_resets_the_history(self):
        result = evaluate(
            snapshot("Sold Out", check_count=9, change_count=4, last_changed_at=T1),
            url="https://example.com/other",
            fingerprint=OTHER_FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertEqual(result.snapshot.check_count, 1)
        self.assertEqual(result.snapshot.change_count, 0)
        self.assertEqual(result.snapshot.first_seen_at, T2)
        self.assertIsNone(result.snapshot.last_changed_at)

    def test_the_new_url_is_stored(self):
        result = evaluate(
            snapshot("Sold Out"),
            url="https://example.com/other",
            fingerprint=OTHER_FINGERPRINT,
            value="In Stock",
            now=T2,
        )
        self.assertEqual(result.snapshot.url, "https://example.com/other")


class EmptyValueTests(unittest.TestCase):
    def test_an_empty_value_is_compared_like_any_other(self):
        result = evaluate(
            snapshot(""), url=URL, fingerprint=FINGERPRINT, value="", now=T2
        )
        self.assertIs(result.outcome, Outcome.UNCHANGED)

    def test_becoming_empty_is_a_change(self):
        result = evaluate(
            snapshot("In Stock"),
            url=URL,
            fingerprint=FINGERPRINT,
            value="",
            now=T2,
        )
        self.assertIs(result.outcome, Outcome.CHANGED)


if __name__ == "__main__":
    unittest.main()
