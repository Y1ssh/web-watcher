"""Actions touch the real world, so the guards get the most testing here.

The property that matters most: nothing performs for real unless the config
explicitly turned dry-run off. Everything else -- rate limits, the run-once
guard, the time window, confirmation -- is a second line of defence.
"""

import json
import os
import unittest
import urllib.parse

from tests.support import LocalSite
from watcher.actions import (
    ActionStatus,
    Safeguards,
    build_action,
    check_guards,
    record_run,
    run_action,
    runs_in_last_24h,
    within_hours,
)
from watcher.errors import ConfigError

NOW = "2000-01-02T12:00:00Z"


def spec(**overrides):
    base = {"kind": "http_request", "url": "https://example.com/book"}
    base.update(overrides)
    return base


def live(**overrides):
    """An action with dry-run explicitly disabled and the guards wide open."""
    safeguards = {"dry_run": False, "run_once": False, "max_per_24h": 99}
    safeguards.update(overrides.pop("safeguards", {}))
    return build_action(spec(safeguards=safeguards, **overrides))


class BuildActionTests(unittest.TestCase):
    def test_minimal_action(self):
        action = build_action(spec())
        self.assertEqual(action.method, "POST")
        self.assertEqual(action.url, "https://example.com/book")

    def test_dry_run_is_on_unless_the_config_turns_it_off(self):
        # The single most important default in the project.
        self.assertTrue(build_action(spec()).safeguards.dry_run)

    def test_run_once_is_on_by_default(self):
        self.assertTrue(build_action(spec()).safeguards.run_once)

    def test_the_default_rate_limit_is_one_per_day(self):
        self.assertEqual(build_action(spec()).safeguards.max_per_24h, 1)

    def test_force_dry_run_overrides_a_live_config(self):
        action = build_action(
            spec(safeguards={"dry_run": False}), force_dry_run=True
        )
        self.assertTrue(action.safeguards.dry_run)

    def test_force_dry_run_cannot_be_used_to_enable_a_real_run(self):
        # There is no force_live. The only route to a real action is the config.
        action = build_action(spec(safeguards={"dry_run": True}), force_dry_run=False)
        self.assertTrue(action.safeguards.dry_run)

    def test_method_is_normalised(self):
        self.assertEqual(build_action(spec(method="post")).method, "POST")

    def test_an_unsupported_method_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_action(spec(method="DELETE"))

    def test_a_non_http_url_is_refused(self):
        with self.assertRaises(ConfigError):
            build_action(spec(url="file:///etc/passwd"))

    def test_a_url_without_a_host_is_refused(self):
        with self.assertRaises(ConfigError):
            build_action(spec(url="https:///path"))

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_action({"kind": "click_button", "url": "https://example.com"})

    def test_misspelled_key_is_rejected(self):
        with self.assertRaises(ConfigError) as caught:
            build_action(spec(feilds={"a": "b"}))
        self.assertIn("feilds", str(caught.exception))

    def test_an_inline_token_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            build_action(spec(token="secret-value"))
        self.assertIn("config file", str(caught.exception))

    def test_fields_must_be_simple_values(self):
        with self.assertRaises(ConfigError):
            build_action(spec(fields={"a": {"nested": 1}}))

    def test_numeric_field_values_are_stringified(self):
        self.assertEqual(build_action(spec(fields={"qty": 2})).fields["qty"], "2")

    def test_describe_lists_the_field_names(self):
        text = build_action(spec(fields={"name": "Jane", "email": "j@e.c"})).describe()
        self.assertIn("name", text)
        self.assertIn("email", text)
        self.assertIn("https://example.com/book", text)

    def test_describe_does_not_print_field_values(self):
        # The description goes into logs and confirmation prompts.
        self.assertNotIn("Jane", build_action(spec(fields={"name": "Jane"})).describe())


class SafeguardValidationTests(unittest.TestCase):
    def test_max_per_24h_must_be_at_least_one(self):
        with self.assertRaises(ConfigError):
            build_action(spec(safeguards={"max_per_24h": 0}))

    def test_hours_must_be_a_pair_of_hour_numbers(self):
        for bad in ([9], [9, 17, 20], ["9", "17"], [9, 24], [-1, 5]):
            with self.subTest(hours=bad):
                with self.assertRaises(ConfigError):
                    build_action(spec(safeguards={"hours": bad}))

    def test_valid_hours_are_accepted(self):
        self.assertEqual(
            build_action(spec(safeguards={"hours": [9, 17]})).safeguards.hours, (9, 17)
        )

    def test_unknown_safeguard_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_action(spec(safeguards={"dry_runs": True}))

    def test_booleans_must_be_booleans(self):
        with self.assertRaises(ConfigError):
            build_action(spec(safeguards={"dry_run": "no"}))


class HourWindowTests(unittest.TestCase):
    def test_a_normal_window(self):
        self.assertTrue(within_hours(12, (9, 17)))
        self.assertFalse(within_hours(8, (9, 17)))
        self.assertFalse(within_hours(18, (9, 17)))

    def test_the_window_is_inclusive_at_both_ends(self):
        self.assertTrue(within_hours(9, (9, 17)))
        self.assertTrue(within_hours(17, (9, 17)))

    def test_a_window_that_wraps_past_midnight(self):
        self.assertTrue(within_hours(23, (22, 4)))
        self.assertTrue(within_hours(2, (22, 4)))
        self.assertFalse(within_hours(12, (22, 4)))


class RateWindowTests(unittest.TestCase):
    def test_counts_only_runs_inside_the_window(self):
        recorded = (
            "2000-01-01T00:00:00Z",  # 36 hours before NOW
            "2000-01-02T06:00:00Z",  # 6 hours before
            "2000-01-02T11:00:00Z",  # 1 hour before
        )
        self.assertEqual(runs_in_last_24h(recorded, NOW), 2)

    def test_no_history_means_no_runs(self):
        self.assertEqual(runs_in_last_24h((), NOW), 0)

    def test_an_unreadable_timestamp_counts_against_you(self):
        # Erring toward "already used" keeps a corrupt file from unlocking the
        # action rather than locking it.
        self.assertEqual(runs_in_last_24h(("not-a-date",), NOW), 1)

    def test_record_run_appends(self):
        self.assertEqual(record_run(("a",), "b"), ("a", "b"))

    def test_record_run_caps_the_history(self):
        history = tuple(str(n) for n in range(60))
        self.assertEqual(len(record_run(history, "new")), 50)
        self.assertEqual(record_run(history, "new")[-1], "new")


class GuardTests(unittest.TestCase):
    def _check(self, action, **overrides):
        settings = {"now": NOW, "previous_runs": (), "total_runs": 0}
        settings.update(overrides)
        return check_guards(action, **settings)

    def test_nothing_blocks_a_clean_run(self):
        self.assertIsNone(self._check(live()))

    def test_run_once_blocks_a_second_run(self):
        action = build_action(spec(safeguards={"dry_run": False, "run_once": True}))
        outcome = self._check(action, total_runs=1)
        self.assertIsNotNone(outcome)
        self.assertIs(outcome.status, ActionStatus.BLOCKED)
        self.assertIn("run_once", outcome.reason)

    def test_the_run_once_message_says_how_to_clear_it(self):
        action = build_action(spec(safeguards={"run_once": True}))
        self.assertIn("--action-only", self._check(action, total_runs=1).reason)

    def test_the_rate_limit_blocks_a_run(self):
        action = build_action(
            spec(safeguards={"dry_run": False, "run_once": False, "max_per_24h": 2})
        )
        recent = ("2000-01-02T10:00:00Z", "2000-01-02T11:00:00Z")
        outcome = self._check(action, previous_runs=recent)
        self.assertIs(outcome.status, ActionStatus.BLOCKED)
        self.assertIn("24 hours", outcome.reason)

    def test_old_runs_do_not_count_against_the_rate_limit(self):
        action = build_action(
            spec(safeguards={"dry_run": False, "run_once": False, "max_per_24h": 1})
        )
        self.assertIsNone(
            self._check(action, previous_runs=("1999-12-01T00:00:00Z",))
        )

    def test_confirmation_blocks_when_nothing_can_ask(self):
        # An unattended scheduled run has no terminal, so a confirm-required
        # action must refuse rather than assume yes.
        action = build_action(
            spec(safeguards={"dry_run": False, "run_once": False, "confirm": True})
        )
        outcome = self._check(action, confirm_fn=None)
        self.assertIs(outcome.status, ActionStatus.BLOCKED)
        self.assertIn("not interactive", outcome.reason)

    def test_confirmation_blocks_on_a_no(self):
        action = build_action(
            spec(safeguards={"dry_run": False, "run_once": False, "confirm": True})
        )
        outcome = self._check(action, confirm_fn=lambda description: False)
        self.assertIs(outcome.status, ActionStatus.BLOCKED)

    def test_confirmation_allows_a_yes(self):
        action = build_action(
            spec(safeguards={"dry_run": False, "run_once": False, "confirm": True})
        )
        self.assertIsNone(self._check(action, confirm_fn=lambda description: True))


class RunActionTests(unittest.TestCase):
    def _run(self, action, **overrides):
        settings = {"now": NOW, "previous_runs": (), "total_runs": 0}
        settings.update(overrides)
        return run_action(action, **settings)

    def test_a_dry_run_sends_nothing(self):
        with LocalSite() as site:
            action = build_action(spec(url=site.url + "/book"))
            outcome = self._run(action)
            self.assertIs(outcome.status, ActionStatus.DRY_RUN)
            self.assertEqual(site.received, [])

    def test_a_dry_run_still_reports_a_blocking_guard(self):
        # A rehearsal that says "fine" when the real thing would be blocked
        # would be worse than useless.
        action = build_action(spec(safeguards={"dry_run": True, "run_once": True}))
        outcome = self._run(action, total_runs=1)
        self.assertIs(outcome.status, ActionStatus.BLOCKED)

    def test_a_live_run_really_posts_the_fields(self):
        with LocalSite() as site:
            action = live(url=site.url + "/book", fields={"name": "Jane Doe"})
            outcome = self._run(action)
            self.assertIs(outcome.status, ActionStatus.PERFORMED)
            self.assertEqual(len(site.received), 1)
            sent = site.received[0]
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(urllib.parse.parse_qs(sent["body"])["name"], ["Jane Doe"])

    def test_json_encoding(self):
        with LocalSite() as site:
            action = live(
                url=site.url + "/book", fields={"name": "Jane"}, encoding="json"
            )
            self._run(action)
            sent = site.received[0]
        self.assertEqual(json.loads(sent["body"]), {"name": "Jane"})
        self.assertIn("application/json", sent["headers"]["Content-Type"])

    def test_a_get_action_puts_fields_in_the_query_string(self):
        with LocalSite() as site:
            action = live(url=site.url + "/", method="GET", fields={"q": "hello"})
            outcome = self._run(action)
        self.assertIs(outcome.status, ActionStatus.PERFORMED)

    def test_a_failing_target_is_reported_not_raised(self):
        with LocalSite() as site:
            site.write_status = 500
            action = live(url=site.url + "/book")
            outcome = self._run(action)
        self.assertIs(outcome.status, ActionStatus.FAILED)
        self.assertIn("500", outcome.reason)

    def test_an_unreachable_target_is_reported_not_raised(self):
        action = live(url="http://127.0.0.1:1/book", timeout_seconds=2)
        self.assertIs(self._run(action).status, ActionStatus.FAILED)

    def test_a_failed_attempt_does_not_count_as_taking_effect(self):
        # The caller uses took_effect to decide whether to burn a run-once slot.
        action = live(url="http://127.0.0.1:1/book", timeout_seconds=2)
        self.assertFalse(self._run(action).took_effect)

    def test_only_a_performed_run_took_effect(self):
        with LocalSite() as site:
            self.assertTrue(self._run(live(url=site.url + "/book")).took_effect)
            self.assertFalse(
                self._run(build_action(spec(url=site.url + "/book"))).took_effect
            )


class HeaderSecretTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._saved)))

    def test_a_header_placeholder_is_filled_from_the_environment(self):
        os.environ["TEST_ACTION_TOKEN"] = "abc123"
        with LocalSite() as site:
            action = live(
                url=site.url + "/book",
                headers={"Authorization": "Bearer ${TEST_ACTION_TOKEN}"},
            )
            self._perform(action)
            sent = site.received[0]
        self.assertEqual(sent["headers"]["Authorization"], "Bearer abc123")

    def test_a_missing_header_variable_is_reported(self):
        os.environ.pop("TEST_ACTION_TOKEN", None)
        with LocalSite() as site:
            action = live(
                url=site.url + "/book",
                headers={"Authorization": "Bearer ${TEST_ACTION_TOKEN}"},
            )
            with self.assertRaises(ConfigError) as caught:
                self._perform(action)
        self.assertIn("TEST_ACTION_TOKEN", str(caught.exception))

    def _perform(self, action):
        return run_action(action, now=NOW, previous_runs=(), total_runs=0)


class SafeguardsDefaultsTests(unittest.TestCase):
    def test_the_defaults_are_the_cautious_ones(self):
        guards = Safeguards()
        self.assertTrue(guards.dry_run)
        self.assertTrue(guards.run_once)
        self.assertEqual(guards.max_per_24h, 1)
        self.assertIsNone(guards.hours)
        self.assertFalse(guards.confirm)


if __name__ == "__main__":
    unittest.main()
