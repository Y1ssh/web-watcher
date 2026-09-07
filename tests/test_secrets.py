"""Credentials come from the environment; the .env loader must never surprise you."""

import os
import tempfile
import unittest
from pathlib import Path

from watcher import secrets
from watcher.errors import ConfigError


class EnvTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(self._restore)
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)
        self.path = self.dir / ".env"

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")


class GetAndRequireTests(EnvTestCase):
    def test_get_returns_a_set_value(self):
        os.environ["TEST_VAR"] = "value"
        self.assertEqual(secrets.get("TEST_VAR"), "value")

    def test_get_returns_none_when_unset(self):
        os.environ.pop("TEST_VAR", None)
        self.assertIsNone(secrets.get("TEST_VAR"))

    def test_a_blank_value_counts_as_missing(self):
        # An empty variable is nearly always a half-finished .env, not a real
        # empty credential.
        os.environ["TEST_VAR"] = "   "
        self.assertIsNone(secrets.get("TEST_VAR"))

    def test_surrounding_whitespace_is_trimmed(self):
        os.environ["TEST_VAR"] = "  value  "
        self.assertEqual(secrets.get("TEST_VAR"), "value")

    def test_require_names_the_variable_and_its_purpose(self):
        os.environ.pop("TEST_VAR", None)
        with self.assertRaises(ConfigError) as caught:
            secrets.require("TEST_VAR", used_for="webhook URL")
        message = str(caught.exception)
        self.assertIn("TEST_VAR", message)
        self.assertIn("webhook URL", message)
        self.assertIn(".env", message)


class LoadEnvFileTests(EnvTestCase):
    def test_missing_file_is_fine(self):
        self.assertEqual(secrets.load_env_file(self.path), [])

    def test_sets_variables(self):
        self.write("TEST_A=one\nTEST_B=two\n")
        applied = secrets.load_env_file(self.path)
        self.assertEqual(sorted(applied), ["TEST_A", "TEST_B"])
        self.assertEqual(os.environ["TEST_A"], "one")

    def test_the_real_environment_always_wins(self):
        # A deployment sets real values on the platform; a stray local .env
        # must not quietly override them.
        os.environ["TEST_A"] = "from-the-shell"
        self.write("TEST_A=from-the-file\n")
        applied = secrets.load_env_file(self.path)
        self.assertEqual(os.environ["TEST_A"], "from-the-shell")
        self.assertNotIn("TEST_A", applied)

    def test_comments_and_blank_lines_are_skipped(self):
        self.write("# a comment\n\nTEST_A=one\n   \n# another\n")
        self.assertEqual(secrets.load_env_file(self.path), ["TEST_A"])

    def test_export_prefix_is_tolerated(self):
        self.write("export TEST_A=one\n")
        secrets.load_env_file(self.path)
        self.assertEqual(os.environ["TEST_A"], "one")

    def test_quotes_are_stripped(self):
        self.write("TEST_A=\"one two\"\nTEST_B='three'\n")
        secrets.load_env_file(self.path)
        self.assertEqual(os.environ["TEST_A"], "one two")
        self.assertEqual(os.environ["TEST_B"], "three")

    def test_a_value_containing_equals_is_kept_whole(self):
        # Base64 and URLs routinely contain "=".
        self.write("TEST_A=abc=def=\n")
        secrets.load_env_file(self.path)
        self.assertEqual(os.environ["TEST_A"], "abc=def=")

    def test_an_empty_value_is_allowed(self):
        self.write("TEST_A=\n")
        secrets.load_env_file(self.path)
        self.assertEqual(os.environ["TEST_A"], "")

    def test_a_line_without_equals_is_reported_with_its_line_number(self):
        self.write("TEST_A=one\nthis is not a setting\n")
        with self.assertRaises(ConfigError) as caught:
            secrets.load_env_file(self.path)
        self.assertIn("line 2", str(caught.exception))

    def test_a_line_with_no_name_is_rejected(self):
        self.write("=orphan\n")
        with self.assertRaises(ConfigError):
            secrets.load_env_file(self.path)


class RedactTests(unittest.TestCase):
    def test_a_short_secret_is_fully_masked(self):
        self.assertEqual(secrets.redact("abc"), "***")

    def test_a_long_secret_shows_only_its_edges(self):
        result = secrets.redact("supersecrettoken1234")
        self.assertNotIn("secrettoken", result)
        self.assertTrue(result.startswith("sup"))
        self.assertIn("20 chars", result)

    def test_an_empty_secret_is_labelled(self):
        self.assertEqual(secrets.redact(""), "(empty)")


if __name__ == "__main__":
    unittest.main()
