"""End-to-end for slice 2: condition, alert, and action, through the real CLI.

Everything runs against local HTTP servers, so a test can watch a page change,
see the alert fire, and confirm the action really posted -- or really did not.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from tests.support import LocalSite, page
from watcher.cli import EXIT_CONFIG, EXIT_FETCH, EXIT_OK, main
from watcher.runlog import read as read_log
from watcher.state import load as load_state


class NotifyTestCase(unittest.TestCase):
    """A temporary project watching a local page, with a local action target."""

    def setUp(self):
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)

        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)
        self.config_path = self.dir / "config.json"
        self.state_path = self.dir / "state" / "snapshot.json"
        self.log_path = self.dir / "logs" / "runs.jsonl"
        self.alerts_path = self.dir / "logs" / "notifications.log"

        self.site = LocalSite(html=page('<div id="status">Sold Out</div>'))
        self.site.__enter__()
        self.addCleanup(self.site.__exit__, None, None, None)

        self.target = LocalSite()
        self.target.__enter__()
        self.addCleanup(self.target.__exit__, None, None, None)

        self.write_config()

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def write_config(self, **overrides):
        settings = {
            "url": self.site.url + "/",
            "extractor": {"kind": "element", "tag": "div", "id": "status"},
            "interval_seconds": 1,
            "timeout_seconds": 5,
            "notify": {
                "channels": [
                    {"kind": "console"},
                    {"kind": "file", "path": "logs/notifications.log"},
                ]
            },
        }
        settings.update(overrides)
        self.config_path.write_text(json.dumps(settings), encoding="utf-8")

    def set_page(self, text: str) -> None:
        self.site.html = page(f'<div id="status">{text}</div>')

    def run_cli(self, *args):
        out = io.StringIO()
        code = main([*args, "--config", str(self.config_path)], stream=out)
        return code, out.getvalue()

    def alerts(self) -> str:
        if not self.alerts_path.exists():
            return ""
        return self.alerts_path.read_text(encoding="utf-8")


class ChangeAlertTests(NotifyTestCase):
    def test_a_baseline_run_does_not_alert(self):
        # Nothing has changed yet, so the first run must be quiet.
        self.run_cli("check")
        self.assertEqual(self.alerts(), "")

    def test_a_real_change_sends_an_alert(self):
        self.run_cli("check")
        self.set_page("In Stock")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("In Stock", self.alerts())
        self.assertIn("alert:", output)

    def test_the_alert_carries_the_old_and_new_values(self):
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        text = self.alerts()
        self.assertIn("Sold Out", text)
        self.assertIn("In Stock", text)
        self.assertIn(self.site.url, text)

    def test_an_unchanged_run_does_not_alert(self):
        self.run_cli("check")
        self.run_cli("check")
        self.assertEqual(self.alerts(), "")

    def test_the_same_change_alerts_only_once(self):
        # A fifteen-minute schedule would otherwise re-send the same alert
        # ninety-six times a day.
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        first = self.alerts()
        self.run_cli("check")
        self.run_cli("check")
        self.assertEqual(self.alerts(), first)

    def test_a_second_distinct_change_alerts_again(self):
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.set_page("Sold Out")
        self.run_cli("check")
        self.assertEqual(self.alerts().count("==="), 2)

    def test_the_notification_state_is_recorded(self):
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        snapshot = load_state(self.state_path)
        self.assertEqual(snapshot.last_notified_hash, snapshot.value_hash)
        self.assertIsNotNone(snapshot.last_notified_at)

    def test_a_standing_condition_alerts_once_while_it_stays_true(self):
        # "contains In Stock" keeps being true on every later check. Without
        # once-per-value de-duplication this would alert on every run forever.
        self.write_config(
            condition={"kind": "contains", "text": "In Stock"},
            notify={"channels": [{"kind": "file", "path": "logs/notifications.log"}]},
        )
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.run_cli("check")
        self.run_cli("check")
        self.assertEqual(self.alerts().count("==="), 1)

    def test_once_per_value_can_be_turned_off(self):
        self.write_config(
            condition={"kind": "contains", "text": "In Stock"},
            notify={
                "channels": [{"kind": "file", "path": "logs/notifications.log"}],
                "once_per_value": False,
            },
        )
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.run_cli("check")
        self.assertEqual(self.alerts().count("==="), 2)


class ConditionTests(NotifyTestCase):
    def test_a_condition_that_is_not_met_stays_quiet(self):
        self.write_config(
            condition={"kind": "contains", "text": "In Stock"},
            notify={"channels": [{"kind": "file", "path": "logs/notifications.log"}]},
        )
        self.run_cli("check")
        self.set_page("Still Sold Out")
        self.run_cli("check")
        self.assertEqual(self.alerts(), "")

    def test_a_condition_that_is_met_alerts(self):
        self.write_config(
            condition={"kind": "contains", "text": "In Stock"},
            notify={"channels": [{"kind": "file", "path": "logs/notifications.log"}]},
        )
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.assertIn("In Stock", self.alerts())

    def test_a_price_condition_fires_only_below_the_threshold(self):
        self.write_config(
            extractor={"kind": "element", "tag": "div", "id": "status"},
            condition={"kind": "number", "below": 50},
            notify={"channels": [{"kind": "file", "path": "logs/notifications.log"}]},
        )
        self.set_page("$99.00")
        self.run_cli("check")
        self.set_page("$75.00")
        self.run_cli("check")
        self.assertEqual(self.alerts(), "")
        self.set_page("$49.99")
        self.run_cli("check")
        self.assertIn("49.99", self.alerts())

    def test_the_reasoning_is_shown_so_the_logic_can_be_checked(self):
        self.write_config(condition={"kind": "number", "below": 50})
        self.set_page("$49.99")
        _, output = self.run_cli("check")
        self.assertIn("rule:", output)
        self.assertIn("49.99", output)
        self.assertIn("below 50", output)

    def test_an_unreadable_value_warns_instead_of_triggering(self):
        # The course's rule: when the number cannot be read, do not act -- warn.
        self.write_config(
            condition={"kind": "number", "below": 50},
            notify={"channels": [{"kind": "file", "path": "logs/notifications.log"}]},
        )
        self.set_page("Currently unavailable")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("UNKNOWN", output)
        self.assertIn("could not evaluate", self.alerts())

    def test_the_unknown_warning_can_be_switched_off(self):
        self.write_config(
            condition={"kind": "number", "below": 50},
            notify={
                "channels": [{"kind": "file", "path": "logs/notifications.log"}],
                "warn_on_unknown": False,
            },
        )
        self.set_page("Currently unavailable")
        self.run_cli("check")
        self.assertEqual(self.alerts(), "")

    def test_a_warning_does_not_use_up_the_once_per_value_slot(self):
        # Once the page becomes readable again the real alert must still fire.
        self.write_config(
            condition={"kind": "number", "below": 50},
            notify={"channels": [{"kind": "file", "path": "logs/notifications.log"}]},
        )
        self.set_page("Currently unavailable")
        self.run_cli("check")
        self.set_page("$10.00")
        self.run_cli("check")
        self.assertIn("10.00", self.alerts())

    def test_a_combined_condition_works_end_to_end(self):
        self.write_config(
            condition={
                "kind": "all",
                "conditions": [
                    {"kind": "changed"},
                    {"kind": "number", "below": 50},
                ],
            },
            notify={"channels": [{"kind": "file", "path": "logs/notifications.log"}]},
        )
        self.set_page("$49.00")
        self.run_cli("check")          # baseline: changed is false
        self.assertEqual(self.alerts(), "")
        self.set_page("$45.00")
        self.run_cli("check")          # changed and below 50
        self.assertIn("45.00", self.alerts())


class TestNotifyCommandTests(NotifyTestCase):
    def test_sends_a_sample_without_touching_the_site(self):
        before = self.site.request_count
        code, output = self.run_cli("test-notify")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.site.request_count, before)
        self.assertIn("sent", output)

    def test_the_sample_is_clearly_labelled_as_a_test(self):
        self.run_cli("test-notify")
        self.assertIn("[TEST]", self.alerts())

    def test_it_does_not_disturb_the_stored_snapshot(self):
        self.run_cli("check")
        before = load_state(self.state_path)
        self.run_cli("test-notify")
        self.assertEqual(load_state(self.state_path), before)

    def test_a_failing_channel_is_reported_with_a_non_zero_exit(self):
        os.environ.pop("MISSING_HOOK", None)
        self.write_config(notify={
            "channels": [{"kind": "webhook", "url_env": "MISSING_HOOK"}]
        })
        code, output = self.run_cli("test-notify")
        self.assertEqual(code, EXIT_FETCH)
        self.assertIn("FAILED", output)

    def test_no_channels_configured_says_so(self):
        self.write_config(notify={"channels": []})
        code, output = self.run_cli("test-notify")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("no notify channels", output)


class WebhookEndToEndTests(NotifyTestCase):
    def test_a_change_posts_to_the_webhook(self):
        os.environ["TEST_HOOK_URL"] = self.target.url + "/hook"
        self.write_config(notify={
            "channels": [{"kind": "webhook", "url_env": "TEST_HOOK_URL"}]
        })
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.assertEqual(len(self.target.received), 1)
        payload = json.loads(self.target.received[0]["body"])
        self.assertIn("In Stock", payload["text"])

    def test_the_webhook_url_can_come_from_a_dotenv_file(self):
        (self.dir / ".env").write_text(
            f"TEST_HOOK_URL={self.target.url}/hook\n", encoding="utf-8"
        )
        os.environ.pop("TEST_HOOK_URL", None)
        self.write_config(notify={
            "channels": [{"kind": "webhook", "url_env": "TEST_HOOK_URL"}]
        })
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.assertEqual(len(self.target.received), 1)

    def test_a_failed_alert_is_retried_on_the_next_run(self):
        # Nothing got through, so the change must not be marked as announced.
        os.environ["TEST_HOOK_URL"] = "http://127.0.0.1:1/hook"
        self.write_config(notify={
            "channels": [
                {"kind": "webhook", "url_env": "TEST_HOOK_URL", "timeout_seconds": 2}
            ]
        })
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.assertIsNone(load_state(self.state_path).last_notified_hash)


class ActionTests(NotifyTestCase):
    def _action_config(self, **safeguards):
        return {
            "kind": "http_request",
            "method": "POST",
            "url": self.target.url + "/book",
            "fields": {"name": "Jane Doe"},
            "safeguards": safeguards,
        }

    def test_dry_run_is_the_default_and_sends_nothing(self):
        self.write_config(action=self._action_config())
        self.run_cli("check")
        self.set_page("In Stock")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.target.received, [])
        self.assertIn("dry_run", output)

    def test_the_sandbox_banner_is_printed_before_anything_happens(self):
        self.write_config(action=self._action_config())
        _, output = self.run_cli("check")
        self.assertTrue(output.startswith("SANDBOX ON"))

    def test_the_live_banner_warns_clearly(self):
        self.write_config(action=self._action_config(dry_run=False))
        _, output = self.run_cli("check")
        self.assertTrue(output.startswith("LIVE"))

    def test_a_live_action_really_posts_when_the_condition_is_met(self):
        self.write_config(action=self._action_config(dry_run=False))
        self.run_cli("check")
        self.set_page("In Stock")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.target.received), 1)
        body = urllib.parse.parse_qs(self.target.received[0]["body"])
        self.assertEqual(body["name"], ["Jane Doe"])
        self.assertIn("DONE", output)

    def test_no_action_when_the_condition_is_not_met(self):
        self.write_config(action=self._action_config(dry_run=False))
        self.run_cli("check")
        self.run_cli("check")
        self.assertEqual(self.target.received, [])

    def test_run_once_stops_a_second_real_action(self):
        self.write_config(action=self._action_config(dry_run=False, run_once=True))
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.set_page("Back In Stock")
        code, output = self.run_cli("check")
        self.assertEqual(len(self.target.received), 1)
        self.assertIn("blocked", output)

    def test_the_run_once_guard_survives_a_restart(self):
        # It lives in the snapshot, not in memory, so a scheduler restarting
        # the process cannot accidentally unlock it.
        self.write_config(action=self._action_config(dry_run=False, run_once=True))
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.assertEqual(load_state(self.state_path).action_total_runs, 1)

    def test_the_rate_limit_stops_a_second_action(self):
        self.write_config(
            action=self._action_config(dry_run=False, run_once=False, max_per_24h=1)
        )
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.set_page("Back In Stock")
        self.run_cli("check")
        self.assertEqual(len(self.target.received), 1)

    def test_dry_run_flag_overrides_a_live_config(self):
        self.write_config(action=self._action_config(dry_run=False))
        self.run_cli("check")
        self.set_page("In Stock")
        code, output = self.run_cli("check", "--dry-run")
        self.assertEqual(self.target.received, [])
        self.assertIn("SANDBOX ON", output)

    def test_a_dry_run_does_not_consume_the_run_once_slot(self):
        self.write_config(action=self._action_config(dry_run=True, run_once=True))
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.assertEqual(load_state(self.state_path).action_total_runs, 0)

    def test_a_failed_action_does_not_consume_the_run_once_slot(self):
        self.target.write_status = 500
        self.write_config(action=self._action_config(dry_run=False, run_once=True))
        self.run_cli("check")
        self.set_page("In Stock")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("failed", output)
        self.assertEqual(load_state(self.state_path).action_total_runs, 0)

    def test_a_failing_action_does_not_stop_the_check_from_completing(self):
        self.target.write_status = 500
        self.write_config(action=self._action_config(dry_run=False))
        self.run_cli("check")
        self.set_page("In Stock")
        code, _ = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(load_state(self.state_path).value, "In Stock")

    def test_reset_action_only_allows_the_action_again(self):
        self.write_config(action=self._action_config(dry_run=False, run_once=True))
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.run_cli("reset", "--action-only", "--yes")
        self.assertEqual(load_state(self.state_path).action_total_runs, 0)
        self.set_page("Back In Stock")
        self.run_cli("check")
        self.assertEqual(len(self.target.received), 2)

    def test_reset_action_only_needs_confirmation(self):
        self.write_config(action=self._action_config(dry_run=False, run_once=True))
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        code, output = self.run_cli("reset", "--action-only")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("--yes", output)
        self.assertEqual(load_state(self.state_path).action_total_runs, 1)

    def test_reset_action_only_keeps_the_value_baseline(self):
        self.write_config(action=self._action_config(dry_run=False))
        self.run_cli("check")
        self.run_cli("reset", "--action-only", "--yes")
        self.assertEqual(load_state(self.state_path).value, "Sold Out")

    def test_a_confirm_required_action_refuses_without_an_answer(self):
        # Nothing is there to answer the prompt, whether because the run is
        # unattended or because stdin is closed. Either way silence must read
        # as "no", never as "go ahead".
        self.write_config(
            action=self._action_config(dry_run=False, confirm=True)
        )
        self.run_cli("check")
        self.set_page("In Stock")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.target.received, [])
        self.assertIn("blocked", output)
        self.assertEqual(load_state(self.state_path).action_total_runs, 0)


class RunLogTests(NotifyTestCase):
    def test_the_log_records_the_condition_verdict_and_reason(self):
        self.write_config(condition={"kind": "number", "below": 50})
        self.set_page("$49.99")
        self.run_cli("check")
        entry = read_log(self.log_path)[-1]
        self.assertEqual(entry["condition"], "true")
        self.assertIn("below 50", entry["condition_reason"])

    def test_the_log_records_the_alert_result(self):
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        entry = read_log(self.log_path)[-1]
        self.assertTrue(any("sent" in line for line in entry["notified"]))

    def test_the_log_records_the_action_status(self):
        self.write_config(action={
            "kind": "http_request",
            "url": self.target.url + "/book",
            "safeguards": {"dry_run": True},
        })
        self.run_cli("check")
        self.set_page("In Stock")
        self.run_cli("check")
        self.assertEqual(read_log(self.log_path)[-1]["action"], "dry_run")


class ShowTests(NotifyTestCase):
    def test_show_reports_the_condition_and_channels(self):
        self.run_cli("check")
        _, output = self.run_cli("show")
        self.assertIn("alert when:", output)
        self.assertIn("channels:", output)

    def test_show_reports_the_action_mode(self):
        self.write_config(action={
            "kind": "http_request",
            "url": self.target.url + "/book",
            "safeguards": {"dry_run": True},
        })
        self.run_cli("check")
        _, output = self.run_cli("show")
        self.assertIn("sandbox", output)

    def test_show_says_when_no_action_is_configured(self):
        self.run_cli("check")
        _, output = self.run_cli("show")
        self.assertIn("none configured", output)


class ConfigErrorTests(NotifyTestCase):
    def test_a_bad_condition_fails_at_startup(self):
        self.write_config(condition={"kind": "nonsense"})
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("condition", output)

    def test_a_bad_channel_fails_at_startup(self):
        self.write_config(notify={"channels": [{"kind": "smoke-signal"}]})
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)

    def test_a_bad_action_fails_at_startup_before_any_fetch(self):
        before = self.site.request_count
        self.write_config(action={"kind": "http_request", "url": "not-a-url"})
        code, _ = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(self.site.request_count, before)

    def test_an_inline_webhook_url_is_refused(self):
        self.write_config(notify={
            "channels": [{"kind": "webhook", "url": "https://hooks.example.com/x"}]
        })
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("environment variable", output)


if __name__ == "__main__":
    unittest.main()
