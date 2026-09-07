"""The secret scanner has to catch real credentials without crying wolf.

A scanner with a high false-positive rate gets ignored within a day, which is
worse than not having one. These tests pin both sides: the shapes it must
catch, and the placeholder-shaped values it must let through.

Every credential-shaped string below is a fixture, so this file exempts itself.
That is the marker doing its job -- without it, the scanner's own tests would
fail the scanner, and the only ways out would be a red CI or a weaker rule.

secretscan: allow-file
"""

import tempfile
import unittest
from pathlib import Path

from watcher.secretscan import (
    has_file_marker,
    Finding,
    looks_like_placeholder,
    redact,
    scan_paths,
    scan_repository,
    scan_text,
    tracked_files,
)


def rules(text: str) -> set[str]:
    return {finding.rule for finding in scan_text(text, Path("sample.py"))}


class DetectionTests(unittest.TestCase):
    def test_aws_access_key(self):
        self.assertIn("aws-access-key", rules("key = AKIAIOSFODNN7EXAMPLE"))

    def test_slack_webhook_url(self):
        text = "url = https://hooks.slack.com/services/T00000000/B00000000/abcdefghijkl"
        self.assertIn("slack-webhook", rules(text))

    def test_slack_token(self):
        self.assertIn("slack-token", rules("t = xoxb-1234567890-abcdefgh"))

    def test_github_token(self):
        self.assertIn(
            "github-token", rules("t = ghp_" + "A" * 36)
        )

    def test_github_fine_grained_token(self):
        self.assertIn("github-token", rules("t = github_pat_" + "a" * 40))

    def test_anthropic_key(self):
        self.assertIn("anthropic-key", rules("k = sk-ant-" + "x" * 30))

    def test_private_key_block(self):
        self.assertIn(
            "private-key", rules("-----BEGIN RSA PRIVATE KEY-----")
        )

    def test_private_key_block_without_an_algorithm(self):
        self.assertIn("private-key", rules("-----BEGIN PRIVATE KEY-----"))

    def test_assigned_password(self):
        self.assertIn("assigned-credential", rules('password = "s7Kd93jfmQ2x"'))

    def test_assigned_password_in_json(self):
        self.assertIn("assigned-credential", rules('"api_key": "s7Kd93jfmQ2x"'))

    def test_assignment_is_case_insensitive(self):
        self.assertIn("assigned-credential", rules('API_KEY = "s7Kd93jfmQ2x"'))

    def test_reports_the_line_number(self):
        findings = scan_text("ok\nok\npassword = \"s7Kd93jfmQ2x\"\n", Path("x.py"))
        self.assertEqual(findings[0].line_number, 3)


class NonDetectionTests(unittest.TestCase):
    def test_ordinary_code_is_clean(self):
        self.assertEqual(rules("total = price * quantity\n"), set())

    def test_a_variable_reference_is_not_a_secret(self):
        self.assertEqual(rules('password = os.environ["PASSWORD"]'), set())

    def test_an_env_var_name_as_a_value_is_not_a_secret(self):
        self.assertEqual(rules('"password_env": "WATCHER_SMTP_PASSWORD"'), set())

    def test_placeholders_are_ignored(self):
        for value in (
            "changeme", "your-token-here", "example-secret", "placeholder",
            "xxxxxxxxxx", "test-value-here", "not-a-secret-here", "${MY_VAR}",
            "<your token>",
        ):
            with self.subTest(value=value):
                self.assertEqual(rules(f'password = "{value}"'), set())

    def test_a_short_value_is_ignored(self):
        # Under eight characters is not a credential worth flagging.
        self.assertEqual(rules('password = "abc"'), set())

    def test_a_minified_line_is_skipped(self):
        # One enormous line is build output, not a hand-typed key.
        line = "x" * 5000 + ' password = "s7Kd93jfmQ2x"'
        self.assertEqual(rules(line), set())

    def test_the_scanner_does_not_flag_its_own_rule_names(self):
        source = Path(__file__).parent.parent / "watcher" / "secretscan.py"
        self.assertEqual(scan_paths([source]), [])


class PlaceholderTests(unittest.TestCase):
    def test_obvious_placeholders(self):
        for value in ("CHANGEME", "your-key", "sample123", "REDACTED"):
            with self.subTest(value=value):
                self.assertTrue(looks_like_placeholder(value))

    def test_repeated_characters(self):
        self.assertTrue(looks_like_placeholder("--------"))
        self.assertTrue(looks_like_placeholder("xxxxxxxxxxxx"))

    def test_an_empty_value(self):
        self.assertTrue(looks_like_placeholder("   "))

    def test_a_real_looking_value_is_not_a_placeholder(self):
        self.assertFalse(looks_like_placeholder("s7Kd93jfmQ2xPl4z"))


class RedactTests(unittest.TestCase):
    def test_shows_enough_to_find_the_line(self):
        result = redact("s7Kd93jfmQ2xPl4z")
        self.assertTrue(result.startswith("s7Kd"))

    def test_never_shows_the_whole_value(self):
        secret = "s7Kd93jfmQ2xPl4z"
        self.assertNotIn(secret, redact(secret))

    def test_a_short_value_is_fully_masked(self):
        self.assertEqual(redact("abc"), "***")


class FileScanTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_finds_a_secret_in_a_file(self):
        path = self.write("app.py", 'token = "s7Kd93jfmQ2xPl4z"\n')
        findings = scan_paths([path])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, path)

    def test_binary_suffixes_are_skipped(self):
        path = self.write("logo.png", 'token = "s7Kd93jfmQ2xPl4z"')
        self.assertEqual(scan_paths([path]), [])

    def test_a_huge_file_is_skipped(self):
        path = self.write("big.txt", "x" * 1_000_001)
        self.assertEqual(scan_paths([path]), [])

    def test_an_unreadable_path_is_skipped_rather_than_raising(self):
        self.assertEqual(scan_paths([self.dir / "does-not-exist.py"]), [])

    def test_scanning_a_directory_without_git_still_works(self):
        self.write("app.py", 'token = "s7Kd93jfmQ2xPl4z"\n')
        findings, how = scan_repository(self.dir)
        self.assertEqual(len(findings), 1)
        self.assertIn("walked", how)

    def test_the_walk_skips_build_and_runtime_directories(self):
        self.write("__pycache__/x.py", 'token = "s7Kd93jfmQ2xPl4z"')
        self.write("state/snapshot.json", '"token": "s7Kd93jfmQ2xPl4z"')
        self.write("logs/runs.jsonl", '"token": "s7Kd93jfmQ2xPl4z"')
        findings, _ = scan_repository(self.dir)
        self.assertEqual(findings, [])

    def test_the_walk_leaves_a_local_env_file_alone(self):
        # .env is meant to hold secrets and is git-ignored. Without git we
        # cannot tell tracked from ignored, so flagging it would be noise.
        self.write(".env", "WATCHER_TOKEN=s7Kd93jfmQ2xPl4z\n")
        findings, _ = scan_repository(self.dir)
        self.assertEqual(findings, [])

    def test_tracked_files_reports_how_it_chose(self):
        self.write("app.py", "x = 1\n")
        paths, how = tracked_files(self.dir)
        self.assertTrue(any(p.name == "app.py" for p in paths))
        self.assertIsInstance(how, str)


class FindingTests(unittest.TestCase):
    def test_describe_uses_a_relative_path_when_it_can(self):
        finding = Finding(Path("/project/app.py"), 3, "rule", "abc...")
        self.assertTrue(finding.describe(Path("/project")).startswith("app.py:3"))

    def test_describe_falls_back_to_the_full_path(self):
        finding = Finding(Path("/elsewhere/app.py"), 3, "rule", "abc...")
        self.assertIn("app.py", finding.describe(Path("/project")))


class AllowMarkerTests(unittest.TestCase):
    """Exemptions must be narrow, deliberate, and visible."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_line_marker_exempts_that_line(self):
        text = 'password = "s7Kd93jfmQ2xPl4z"  # secretscan: allow\n'
        self.assertEqual(scan_text(text, Path("x.py")), [])

    def test_a_line_marker_exempts_only_its_own_line(self):
        text = (
            'password = "s7Kd93jfmQ2xPl4z"  # secretscan: allow\n'
            'token = "aB3xY9zQ1mN4pL7v"\n'
        )
        findings = scan_text(text, Path("x.py"))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line_number, 2)

    def test_a_file_marker_exempts_the_whole_file(self):
        text = '"""secretscan: allow-file"""\npassword = "s7Kd93jfmQ2xPl4z"\n'
        self.assertEqual(scan_text(text, Path("x.py")), [])

    def test_a_file_marker_further_down_does_not_count(self):
        # Otherwise a marker could be smuggled into the middle of a long file
        # where nobody reviewing the top of it would see it.
        text = "\n" * 40 + '# secretscan: allow-file\npassword = "s7Kd93jfmQ2xPl4z"\n'
        self.assertEqual(len(scan_text(text, Path("x.py"))), 1)

    def test_the_scan_says_how_many_files_it_skipped(self):
        # A blanket exemption is never silent.
        self.write("fixtures.py", '# secretscan: allow-file\nk = "s7Kd93jfmQ2xPl4z"\n')
        findings, how = scan_repository(self.dir)
        self.assertEqual(findings, [])
        self.assertIn("1 file(s) skipped", how)

    def test_no_skip_note_when_nothing_was_skipped(self):
        self.write("app.py", "x = 1\n")
        _, how = scan_repository(self.dir)
        self.assertNotIn("skipped", how)

    def test_the_scanner_does_not_exempt_itself(self):
        # The markers are assembled from fragments so the literal never appears
        # in the scanner's own source. If that ever breaks, the scanner would
        # quietly stop scanning itself.
        source = (Path(__file__).parent.parent / "watcher" / "secretscan.py")
        text = source.read_text(encoding="utf-8")
        self.assertFalse(has_file_marker(text))


class RepositorySelfScanTests(unittest.TestCase):
    """This project must pass its own scanner."""

    def test_the_repository_is_clean(self):
        root = Path(__file__).parent.parent
        findings, _ = scan_repository(root)
        self.assertEqual(
            [finding.describe(root) for finding in findings],
            [],
            "the repository's own tracked files must contain no credentials",
        )


if __name__ == "__main__":
    unittest.main()
