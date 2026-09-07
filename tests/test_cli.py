"""End-to-end: the whole cycle against a real page that really changes.

This is the automated version of the manual check the course asks for -- run it
once, look at the stored file, change the page, run it again, confirm it says
CHANGED and the file was updated.
"""

import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.support import LocalSite, page
from watcher.cli import EXIT_CONFIG, EXIT_FETCH, EXIT_OK, main
from watcher.runlog import read as read_log
from watcher.state import load as load_state


class CliTestCase(unittest.TestCase):
    """Sets up a temporary project directory pointed at a local test site."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)
        self.config_path = self.dir / "config.json"
        self.state_path = self.dir / "state" / "snapshot.json"
        self.log_path = self.dir / "logs" / "runs.jsonl"

        self.site = LocalSite(html=page('<div id="status">In Stock</div>'))
        self.site.__enter__()
        self.addCleanup(self.site.__exit__, None, None, None)

        self.write_config()

    def write_config(self, **overrides):
        settings = {
            "url": self.site.url + "/",
            "extractor": {"kind": "element", "tag": "div", "id": "status"},
            "interval_seconds": 1,
            "timeout_seconds": 5,
        }
        settings.update(overrides)
        self.config_path.write_text(json.dumps(settings), encoding="utf-8")

    def run_cli(self, *args):
        """Invoke the CLI. Common flags follow the subcommand, as argparse expects."""
        out = io.StringIO()
        code = main([*args, "--config", str(self.config_path)], stream=out)
        return code, out.getvalue()


class CheckTests(CliTestCase):
    def test_first_run_saves_a_baseline(self):
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("BASELINE", output)
        self.assertIn("In Stock", output)

    def test_first_run_writes_the_state_file(self):
        self.run_cli("check")
        snapshot = load_state(self.state_path)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.value, "In Stock")
        self.assertEqual(snapshot.check_count, 1)

    def test_second_run_with_no_change_is_quiet(self):
        self.run_cli("check")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        # "CHANGED" is a substring of "UNCHANGED", so check the headline line.
        self.assertEqual(output.splitlines()[0], "UNCHANGED")

    def test_a_real_change_on_the_page_is_reported(self):
        self.run_cli("check")
        self.site.html = page('<div id="status">Sold Out</div>')
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("CHANGED", output)
        self.assertIn("In Stock", output)
        self.assertIn("Sold Out", output)

    def test_a_change_updates_the_stored_file(self):
        self.run_cli("check")
        self.site.html = page('<div id="status">Sold Out</div>')
        self.run_cli("check")
        snapshot = load_state(self.state_path)
        self.assertEqual(snapshot.value, "Sold Out")
        self.assertEqual(snapshot.change_count, 1)
        self.assertEqual(snapshot.check_count, 2)
        self.assertIsNotNone(snapshot.last_changed_at)

    def test_a_change_is_announced_once_not_on_every_later_run(self):
        self.run_cli("check")
        self.site.html = page('<div id="status">Sold Out</div>')
        self.run_cli("check")
        _, output = self.run_cli("check")
        self.assertIn("UNCHANGED", output)

    def test_editing_the_state_file_by_hand_makes_the_next_run_report_a_change(self):
        # The manual state comparison from the course, automated.
        self.run_cli("check")
        stored = json.loads(self.state_path.read_text(encoding="utf-8"))
        stored["snapshot"]["value"] = "something else"
        stored["snapshot"]["value_hash"] = "0" * 64
        self.state_path.write_text(json.dumps(stored), encoding="utf-8")
        _, output = self.run_cli("check")
        self.assertIn("CHANGED", output)

    def test_pointing_at_a_new_target_re_baselines_instead_of_alerting(self):
        self.run_cli("check")
        self.write_config(extractor={"kind": "element", "tag": "title"})
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("RETARGETED", output)


class RunLogTests(CliTestCase):
    def test_every_check_appends_one_entry(self):
        self.run_cli("check")
        self.run_cli("check")
        entries = read_log(self.log_path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["outcome"], "baseline")
        self.assertEqual(entries[1]["outcome"], "unchanged")

    def test_each_entry_carries_a_timestamp(self):
        self.run_cli("check")
        self.assertTrue(read_log(self.log_path)[0]["at"].endswith("Z"))

    def test_a_failed_fetch_is_recorded(self):
        self.write_config(url="http://127.0.0.1:1/", timeout_seconds=2)
        self.run_cli("check")
        entries = read_log(self.log_path)
        self.assertEqual(entries[-1]["event"], "error")
        self.assertEqual(entries[-1]["error_type"], "FetchError")


class FailureTests(CliTestCase):
    def test_an_unreachable_site_exits_with_the_fetch_code(self):
        self.write_config(url="http://127.0.0.1:1/", timeout_seconds=2)
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_FETCH)
        self.assertIn("check failed", output)

    def test_a_missing_value_on_the_page_exits_with_the_fetch_code(self):
        self.write_config(extractor={"kind": "element", "tag": "div", "id": "gone"})
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_FETCH)
        self.assertIn("check failed", output)

    def test_a_missing_value_leaves_the_previous_snapshot_intact(self):
        # A page that stops showing the value must not wipe the baseline.
        self.run_cli("check")
        self.write_config(extractor={"kind": "element", "tag": "div", "id": "gone"})
        self.run_cli("check")
        self.assertEqual(load_state(self.state_path).value, "In Stock")

    def test_a_broken_config_exits_with_the_config_code(self):
        self.config_path.write_text("{oops", encoding="utf-8")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("config error", output)

    def test_a_missing_config_exits_with_the_config_code(self):
        self.config_path.unlink()
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("config.example.json", output)

    def test_a_corrupt_state_file_is_reported_rather_than_ignored(self):
        self.run_cli("check")
        self.state_path.write_text("{broken", encoding="utf-8")
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("state error", output)


class ShowTests(CliTestCase):
    def test_show_before_any_run_explains_what_to_do(self):
        code, output = self.run_cli("show")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no snapshot stored", output)

    def test_show_prints_the_stored_value(self):
        self.run_cli("check")
        code, output = self.run_cli("show")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("In Stock", output)
        self.assertIn("1 check(s)", output)

    def test_show_json_is_machine_readable(self):
        self.run_cli("check")
        _, output = self.run_cli("show", "--json")
        self.assertEqual(json.loads(output)["value"], "In Stock")

    def test_show_can_print_recent_log_entries(self):
        self.run_cli("check")
        _, output = self.run_cli("show", "--log")
        self.assertIn("run-log entries", output)
        self.assertIn("baseline", output)

    def test_show_warns_when_the_config_no_longer_matches_the_snapshot(self):
        self.run_cli("check")
        self.write_config(extractor={"kind": "element", "tag": "title"})
        _, output = self.run_cli("show")
        self.assertIn("re-baseline", output)


class ResetTests(CliTestCase):
    def test_reset_requires_confirmation(self):
        self.run_cli("check")
        code, output = self.run_cli("reset")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("--yes", output)
        self.assertTrue(self.state_path.exists())

    def test_reset_with_yes_deletes_the_snapshot(self):
        self.run_cli("check")
        code, _ = self.run_cli("reset", "--yes")
        self.assertEqual(code, EXIT_OK)
        self.assertFalse(self.state_path.exists())

    def test_after_a_reset_the_next_check_re_baselines(self):
        self.run_cli("check")
        self.run_cli("reset", "--yes")
        _, output = self.run_cli("check")
        self.assertIn("BASELINE", output)

    def test_reset_with_nothing_stored_says_so(self):
        code, output = self.run_cli("reset", "--yes")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("nothing to delete", output)


class WatchTests(CliTestCase):
    def test_watch_fires_repeatedly_and_logs_every_run(self):
        # Proving the schedule with a short interval, exactly as the course
        # describes: shorten it, watch the log fill, then restore the real one.
        code, output = self.run_cli("watch", "--interval", "1", "--max-runs", "2")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("stopped after 2 run(s)", output)
        entries = read_log(self.log_path)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["outcome"], "baseline")
        self.assertEqual(entries[1]["outcome"], "unchanged")

    def test_watch_hit_the_site_once_per_run(self):
        before = self.site.request_count
        self.run_cli("watch", "--interval", "1", "--max-runs", "2")
        self.assertEqual(self.site.request_count - before, 2)

    def test_watch_survives_a_failing_run(self):
        self.write_config(url="http://127.0.0.1:1/", timeout_seconds=1)
        code, output = self.run_cli("watch", "--interval", "1", "--max-runs", "2")
        self.assertEqual(code, EXIT_FETCH)
        self.assertIn("stopped after 2 run(s), 2 failed", output)

    def test_watch_rejects_a_max_runs_below_one(self):
        code, _ = self.run_cli("watch", "--max-runs", "0")
        self.assertEqual(code, EXIT_CONFIG)

    def test_watch_rejects_an_interval_below_the_minimum(self):
        code, output = self.run_cli("watch", "--interval", "0")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("--interval", output)


if __name__ == "__main__":
    unittest.main()
