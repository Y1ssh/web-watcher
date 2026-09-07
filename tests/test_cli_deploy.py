"""The commands and plumbing that only matter once the app leaves your machine.

Covers the pre-launch checklist, the secret scan, settings arriving as an
environment variable instead of a file, and the startup lines a host's log
dashboard has to show for a deploy to be verifiable.
"""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import LocalSite, page
from watcher import preflight
from watcher.cli import EXIT_CONFIG, EXIT_OK, main
from watcher.config import CONFIG_ENV_VAR

REQUIRED_IGNORES_TEXT = "\n".join(preflight.REQUIRED_IGNORES)


class DeployTestCase(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        # A leftover WATCHER_CONFIG would silently steer every test.
        os.environ.pop(CONFIG_ENV_VAR, None)

        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)
        self.config_path = self.dir / "config.json"
        (self.dir / ".gitignore").write_text(REQUIRED_IGNORES_TEXT, encoding="utf-8")

        self.site = LocalSite(html=page('<div id="status">In Stock</div>'))
        self.site.__enter__()
        self.addCleanup(self.site.__exit__, None, None, None)

        self.write_config()

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def settings(self, **overrides):
        base = {
            "url": self.site.url + "/",
            "extractor": {"kind": "element", "tag": "div", "id": "status"},
            "interval_seconds": 900,
            "timeout_seconds": 5,
            "state_path": str(self.dir / "state" / "snapshot.json"),
            "log_path": str(self.dir / "logs" / "runs.jsonl"),
        }
        base.update(overrides)
        return base

    def write_config(self, **overrides):
        self.config_path.write_text(
            json.dumps(self.settings(**overrides)), encoding="utf-8"
        )

    def run_cli(self, *args):
        out = io.StringIO()
        code = main(list(args), stream=out)
        return code, out.getvalue()

    def run_with_config(self, *args):
        return self.run_cli(*args, "--config", str(self.config_path))


class PreflightCommandTests(DeployTestCase):
    def test_a_healthy_project_passes(self):
        code, output = self.run_with_config("preflight")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("No blocking problems", output)

    def test_it_reports_every_check(self):
        _, output = self.run_with_config("preflight")
        for name in ("secrets", "gitignore", "environment", "state",
                     "notify", "action", "interval", "target"):
            self.assertIn(name, output)

    def test_a_missing_gitignore_blocks_with_a_non_zero_exit(self):
        (self.dir / ".gitignore").unlink()
        code, output = self.run_with_config("preflight")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("Fix the failures", output)

    def test_a_hardcoded_credential_blocks(self):
        (self.dir / "leaky.py").write_text(
            'api_key = "s7Kd93jfmQ2xPl4z"\n', encoding="utf-8"  # secretscan: allow
        )
        code, output = self.run_with_config("preflight")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("FAIL", output)

    def test_offline_skips_the_network_check(self):
        before = self.site.request_count
        code, output = self.run_with_config("preflight", "--offline")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.site.request_count, before)
        self.assertIn("--offline", output)

    def test_the_live_page_is_checked_by_default(self):
        before = self.site.request_count
        self.run_with_config("preflight")
        self.assertEqual(self.site.request_count - before, 1)

    def test_it_shows_the_value_it_read_so_you_can_check_it_by_eye(self):
        _, output = self.run_with_config("preflight")
        self.assertIn("In Stock", output)

    def test_an_unreachable_target_blocks(self):
        self.write_config(url="http://127.0.0.1:1/", timeout_seconds=2)
        code, _ = self.run_with_config("preflight")
        self.assertEqual(code, EXIT_CONFIG)

    def test_warnings_alone_do_not_block(self):
        self.write_config(interval_seconds=5)
        code, output = self.run_with_config("preflight")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("WARN", output)

    def test_the_project_root_can_be_pointed_elsewhere(self):
        other = self.dir / "elsewhere"
        other.mkdir()
        code, output = self.run_with_config("preflight", "--project", str(other))
        # No .gitignore over there, so that check must fail.
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn(str(other), output)

    def test_it_does_not_write_a_snapshot(self):
        # A checklist must not change the thing it is checking.
        self.run_with_config("preflight")
        self.assertFalse((self.dir / "state" / "snapshot.json").exists())


class ScanSecretsCommandTests(DeployTestCase):
    def test_a_clean_directory_passes(self):
        code, output = self.run_cli("scan-secrets", "--path", str(self.dir))
        self.assertEqual(code, EXIT_OK)
        self.assertIn("no credential-shaped values", output)

    def test_a_credential_is_reported_with_a_non_zero_exit(self):
        (self.dir / "leaky.py").write_text(
            'api_key = "s7Kd93jfmQ2xPl4z"\n', encoding="utf-8"  # secretscan: allow
        )
        code, output = self.run_cli("scan-secrets", "--path", str(self.dir))
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("leaky.py", output)
        self.assertIn("rotate", output)

    def test_the_secret_itself_is_not_printed(self):
        (self.dir / "leaky.py").write_text(
            'api_key = "s7Kd93jfmQ2xPl4z"\n', encoding="utf-8"  # secretscan: allow
        )
        _, output = self.run_cli("scan-secrets", "--path", str(self.dir))
        self.assertNotIn("s7Kd93jfmQ2xPl4z", output)

    def test_it_runs_without_any_config_at_all(self):
        # This is exactly the situation in CI: config.json is git-ignored.
        self.config_path.unlink()
        code, _ = self.run_cli("scan-secrets", "--path", str(self.dir))
        self.assertEqual(code, EXIT_OK)

    def test_a_missing_directory_is_reported(self):
        code, output = self.run_cli(
            "scan-secrets", "--path", str(self.dir / "nowhere")
        )
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("no such directory", output)


class ConfigFromEnvironmentTests(DeployTestCase):
    def test_settings_can_arrive_as_an_environment_variable(self):
        os.environ[CONFIG_ENV_VAR] = json.dumps(self.settings())
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("BASELINE", output)

    def test_an_explicit_config_flag_wins_over_the_environment(self):
        # A deployment variable must not silently shadow a file you chose.
        os.environ[CONFIG_ENV_VAR] = json.dumps(
            self.settings(url="http://127.0.0.1:1/")
        )
        code, output = self.run_with_config("check")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("In Stock", output)

    def test_malformed_json_in_the_variable_is_reported_clearly(self):
        os.environ[CONFIG_ENV_VAR] = "{not json"
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn(CONFIG_ENV_VAR, output)

    def test_an_invalid_config_in_the_variable_is_rejected(self):
        os.environ[CONFIG_ENV_VAR] = json.dumps({"url": "not-a-url"})
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)

    def test_a_blank_variable_falls_through_to_the_default_file(self):
        os.environ[CONFIG_ENV_VAR] = "   "
        # The fallback reads config.json from the working directory, so the
        # test needs a directory it controls rather than wherever it was run.
        empty = self.dir / "empty"
        empty.mkdir()
        previous = Path.cwd()
        os.chdir(empty)
        self.addCleanup(os.chdir, previous)
        code, output = self.run_cli("check")
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("config.example.json", output)


class StartupLoggingTests(DeployTestCase):
    """After a deploy you read the host's logs. They have to say enough."""

    def test_watch_reports_that_it_started_and_where_its_settings_came_from(self):
        _, output = self.run_with_config(
            "watch", "--interval", "1", "--max-runs", "1"
        )
        self.assertIn("web-watcher starting", output)
        self.assertIn(str(self.config_path), output)

    def test_watch_reports_where_the_snapshot_lives(self):
        _, output = self.run_with_config(
            "watch", "--interval", "1", "--max-runs", "1"
        )
        self.assertIn("state:", output)
        self.assertIn("snapshot.json", output)

    def test_watch_names_the_config_variable_when_settings_come_from_it(self):
        os.environ[CONFIG_ENV_VAR] = json.dumps(self.settings())
        _, output = self.run_cli("watch", "--interval", "1", "--max-runs", "1")
        self.assertIn(f"${CONFIG_ENV_VAR}", output)

    def test_watch_says_when_a_dotenv_file_was_read(self):
        (self.dir / ".env").write_text("TEST_DEPLOY_VAR=value\n", encoding="utf-8")
        os.environ.pop("TEST_DEPLOY_VAR", None)
        _, output = self.run_with_config(
            "watch", "--interval", "1", "--max-runs", "1"
        )
        self.assertIn("from .env", output)

    def test_watch_says_when_it_is_using_the_platform_environment(self):
        _, output = self.run_with_config(
            "watch", "--interval", "1", "--max-runs", "1"
        )
        self.assertIn("platform environment", output)

    def test_a_check_records_that_the_site_actually_responded(self):
        # The easiest thing to miss in a log: the app started fine but never
        # reached the site it is supposed to be watching.
        _, output = self.run_with_config("check")
        self.assertIn("HTTP 200", output)


if __name__ == "__main__":
    unittest.main()
