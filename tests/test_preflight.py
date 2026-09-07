"""The pre-launch checklist, checked.

A checklist that passes when something is wrong is worse than none, so each
check gets both its passing and its failing case.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import LocalSite, page
from watcher import config as config_module
from watcher import preflight
from watcher.preflight import Level

REQUIRED_IGNORES_TEXT = "\n".join(preflight.REQUIRED_IGNORES)


class PreflightTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)

        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)
        (self.dir / ".gitignore").write_text(REQUIRED_IGNORES_TEXT, encoding="utf-8")

        self.site = LocalSite(html=page('<div id="status">In Stock</div>'))
        self.site.__enter__()
        self.addCleanup(self.site.__exit__, None, None, None)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def config(self, **overrides) -> config_module.Config:
        settings = {
            "url": self.site.url + "/",
            "extractor": {"kind": "element", "tag": "div", "id": "status"},
            "state_path": str(self.dir / "state" / "snapshot.json"),
            "notify": {"channels": [{"kind": "console"}]},
        }
        settings.update(overrides)
        path = self.dir / "config.json"
        path.write_text(json.dumps(settings), encoding="utf-8")
        return config_module.load(path)

    def run_checks(self, settings=None, *, offline=True) -> dict[str, preflight.Result]:
        report = preflight.run(
            settings or self.config(), project_root=self.dir, offline=offline
        )
        return {result.name: result for result in report.results}


class SecretCheckTests(PreflightTestCase):
    def test_a_clean_project_passes(self):
        self.assertIs(self.run_checks()["secrets"].level, Level.PASS)

    def test_a_hardcoded_credential_fails(self):
        (self.dir / "app.py").write_text(
            'api_key = "s7Kd93jfmQ2xPl4z"\n', encoding="utf-8"  # secretscan: allow
        )
        result = self.run_checks()["secrets"]
        self.assertIs(result.level, Level.FAIL)
        self.assertIn("rotate", result.advice)


class GitignoreCheckTests(PreflightTestCase):
    def test_a_complete_gitignore_passes(self):
        self.assertIs(self.run_checks()["gitignore"].level, Level.PASS)

    def test_a_missing_gitignore_fails(self):
        (self.dir / ".gitignore").unlink()
        self.assertIs(self.run_checks()["gitignore"].level, Level.FAIL)

    def test_a_gitignore_that_forgets_env_fails(self):
        (self.dir / ".gitignore").write_text(
            "config.json\nstate/\nlogs/\n", encoding="utf-8"
        )
        result = self.run_checks()["gitignore"]
        self.assertIs(result.level, Level.FAIL)
        self.assertIn(".env", result.message)

    def test_comments_do_not_count_as_entries(self):
        (self.dir / ".gitignore").write_text("# .env\n", encoding="utf-8")
        self.assertIs(self.run_checks()["gitignore"].level, Level.FAIL)

    def test_a_trailing_slash_still_matches(self):
        (self.dir / ".gitignore").write_text(
            ".env\nconfig.json\nstate/\nlogs/\n", encoding="utf-8"
        )
        self.assertIs(self.run_checks()["gitignore"].level, Level.PASS)


class EnvironmentCheckTests(PreflightTestCase):
    def test_a_config_needing_nothing_passes(self):
        self.assertIs(self.run_checks()["environment"].level, Level.PASS)

    def test_a_missing_webhook_variable_fails(self):
        os.environ.pop("TEST_PREFLIGHT_HOOK", None)
        settings = self.config(notify={
            "channels": [{"kind": "webhook", "url_env": "TEST_PREFLIGHT_HOOK"}]
        })
        result = self.run_checks(settings)["environment"]
        self.assertIs(result.level, Level.FAIL)
        self.assertIn("TEST_PREFLIGHT_HOOK", result.message)

    def test_a_present_webhook_variable_passes(self):
        os.environ["TEST_PREFLIGHT_HOOK"] = "https://example.com/hook"
        settings = self.config(notify={
            "channels": [{"kind": "webhook", "url_env": "TEST_PREFLIGHT_HOOK"}]
        })
        self.assertIs(self.run_checks(settings)["environment"].level, Level.PASS)

    def test_a_blank_variable_counts_as_missing(self):
        os.environ["TEST_PREFLIGHT_HOOK"] = "   "
        settings = self.config(notify={
            "channels": [{"kind": "webhook", "url_env": "TEST_PREFLIGHT_HOOK"}]
        })
        self.assertIs(self.run_checks(settings)["environment"].level, Level.FAIL)

    def test_an_action_header_variable_is_required_too(self):
        os.environ.pop("TEST_PREFLIGHT_TOKEN", None)
        settings = self.config(action={
            "kind": "http_request",
            "url": "https://example.com/book",
            "headers": {"Authorization": "Bearer ${TEST_PREFLIGHT_TOKEN}"},
        })
        result = self.run_checks(settings)["environment"]
        self.assertIs(result.level, Level.FAIL)
        self.assertIn("TEST_PREFLIGHT_TOKEN", result.message)

    def test_email_credentials_are_required_too(self):
        os.environ.pop("TEST_PREFLIGHT_SMTP_PASSWORD", None)
        os.environ["TEST_PREFLIGHT_SMTP_USER"] = "user@example.com"
        settings = self.config(notify={"channels": [{
            "kind": "email",
            "to": "me@example.com",
            "from": "watcher@example.com",
            "host": "smtp.example.com",
            "username_env": "TEST_PREFLIGHT_SMTP_USER",
            "password_env": "TEST_PREFLIGHT_SMTP_PASSWORD",
        }]})
        result = self.run_checks(settings)["environment"]
        self.assertIs(result.level, Level.FAIL)
        self.assertIn("TEST_PREFLIGHT_SMTP_PASSWORD", result.message)


class StateCheckTests(PreflightTestCase):
    def test_a_state_path_inside_the_project_warns_about_ephemeral_disks(self):
        # This is the deployment trap: a fresh filesystem per deploy means the
        # watcher re-baselines and misses the next real change.
        result = self.run_checks()["state"]
        self.assertIs(result.level, Level.WARN)
        self.assertIn("re-baseline", result.advice)

    def test_a_state_path_outside_the_project_passes(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        settings = self.config(state_path=str(outside / "snapshot.json"))
        self.assertIs(self.run_checks(settings)["state"].level, Level.PASS)

    def test_an_unwritable_state_directory_fails(self):
        blocker = self.dir / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        settings = self.config(state_path=str(blocker / "inner" / "snapshot.json"))
        self.assertIs(self.run_checks(settings)["state"].level, Level.FAIL)

    def test_the_write_probe_cleans_up_after_itself(self):
        self.run_checks()
        leftovers = list((self.dir / "state").glob(".preflight*"))
        self.assertEqual(leftovers, [])


class NotifyCheckTests(PreflightTestCase):
    def test_a_configured_channel_passes(self):
        self.assertIs(self.run_checks()["notify"].level, Level.PASS)

    def test_no_channels_warns(self):
        settings = self.config(notify={"channels": []})
        result = self.run_checks(settings)["notify"]
        self.assertIs(result.level, Level.WARN)
        self.assertIn("nobody", result.advice)


class ActionCheckTests(PreflightTestCase):
    def test_no_action_passes(self):
        self.assertIs(self.run_checks()["action"].level, Level.PASS)

    def test_a_sandboxed_action_passes(self):
        settings = self.config(action={
            "kind": "http_request",
            "url": "https://example.com/book",
            "safeguards": {"dry_run": True},
        })
        self.assertIs(self.run_checks(settings)["action"].level, Level.PASS)

    def test_a_live_action_warns_loudly(self):
        settings = self.config(action={
            "kind": "http_request",
            "url": "https://example.com/book",
            "safeguards": {"dry_run": False},
        })
        result = self.run_checks(settings)["action"]
        self.assertIs(result.level, Level.WARN)
        self.assertIn("LIVE", result.message)

    def test_the_live_warning_lists_the_guards_still_in_force(self):
        settings = self.config(action={
            "kind": "http_request",
            "url": "https://example.com/book",
            "safeguards": {"dry_run": False, "run_once": True, "max_per_24h": 3},
        })
        advice = self.run_checks(settings)["action"].advice
        self.assertIn("run_once=True", advice)
        self.assertIn("max_per_24h=3", advice)


class IntervalCheckTests(PreflightTestCase):
    def test_a_gentle_interval_passes(self):
        settings = self.config(interval_seconds=900)
        self.assertIs(self.run_checks(settings)["interval"].level, Level.PASS)

    def test_a_hammering_interval_warns(self):
        settings = self.config(interval_seconds=5)
        result = self.run_checks(settings)["interval"]
        self.assertIs(result.level, Level.WARN)
        self.assertIn("abusive", result.advice)


class TargetCheckTests(PreflightTestCase):
    def test_a_reachable_page_passes_and_shows_the_value(self):
        result = self.run_checks(offline=False)["target"]
        self.assertIs(result.level, Level.PASS)
        self.assertIn("In Stock", result.message)

    def test_an_unreachable_page_fails(self):
        settings = self.config(url="http://127.0.0.1:1/", timeout_seconds=2)
        self.assertIs(self.run_checks(settings, offline=False)["target"].level, Level.FAIL)

    def test_a_missing_value_on_a_reachable_page_fails(self):
        settings = self.config(
            extractor={"kind": "element", "tag": "div", "id": "gone"}
        )
        result = self.run_checks(settings, offline=False)["target"]
        self.assertIs(result.level, Level.FAIL)
        self.assertIn("could not be read", result.message)

    def test_offline_mode_warns_rather_than_silently_skipping(self):
        result = self.run_checks(offline=True)["target"]
        self.assertIs(result.level, Level.WARN)
        self.assertIn("--offline", result.message)

    def test_offline_mode_makes_no_request(self):
        before = self.site.request_count
        self.run_checks(offline=True)
        self.assertEqual(self.site.request_count, before)


class ReportTests(PreflightTestCase):
    def test_a_clean_project_reports_ok(self):
        report = preflight.run(self.config(), project_root=self.dir, offline=False)
        self.assertTrue(report.ok)

    def test_a_failure_makes_the_report_not_ok(self):
        (self.dir / ".gitignore").unlink()
        report = preflight.run(self.config(), project_root=self.dir, offline=False)
        self.assertFalse(report.ok)

    def test_warnings_alone_do_not_block(self):
        # A warning is a decision to make, not a reason to refuse to deploy.
        report = preflight.run(self.config(), project_root=self.dir, offline=True)
        self.assertTrue(report.warnings)
        self.assertTrue(report.ok)

    def test_the_summary_counts_every_result(self):
        report = preflight.run(self.config(), project_root=self.dir, offline=True)
        self.assertIn("passed", report.summary())

    def test_the_formatted_report_shows_advice_for_problems_only(self):
        report = preflight.run(self.config(), project_root=self.dir, offline=True)
        text = preflight.format_report(report, self.dir)
        self.assertIn("[WARN]", text)
        self.assertIn("->", text)

    def test_every_check_runs_exactly_once(self):
        report = preflight.run(self.config(), project_root=self.dir, offline=True)
        names = [result.name for result in report.results]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            set(names),
            {"secrets", "gitignore", "environment", "state", "notify",
             "action", "interval", "target"},
        )


if __name__ == "__main__":
    unittest.main()
