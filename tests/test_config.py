"""A bad config must fail at startup, not silently during an unattended run."""

import json
import tempfile
import unittest
from pathlib import Path

from watcher.config import (
    DEFAULT_INTERVAL_SECONDS,
    Config,
    from_mapping,
    load,
)
from watcher.errors import ConfigError

VALID = {
    "url": "https://example.com/product",
    "extractor": {"kind": "element", "tag": "h1"},
}


def build(**overrides) -> Config:
    return from_mapping({**VALID, **overrides}, base_dir=Path("/base"))


class ValidConfigTests(unittest.TestCase):
    def test_minimal_config_loads(self):
        config = build()
        self.assertEqual(config.url, "https://example.com/product")
        self.assertEqual(config.extractor["tag"], "h1")

    def test_interval_defaults_when_omitted(self):
        self.assertEqual(build().interval_seconds, DEFAULT_INTERVAL_SECONDS)

    def test_explicit_values_win_over_defaults(self):
        config = build(interval_seconds=60, timeout_seconds=5, user_agent="bot")
        self.assertEqual(config.interval_seconds, 60)
        self.assertEqual(config.timeout_seconds, 5.0)
        self.assertEqual(config.user_agent, "bot")

    def test_surrounding_whitespace_is_trimmed_from_the_url(self):
        self.assertEqual(build(url="  https://example.com  ").url, "https://example.com")

    def test_http_is_accepted_as_well_as_https(self):
        self.assertEqual(build(url="http://example.com").url, "http://example.com")

    def test_fingerprint_changes_with_the_target(self):
        self.assertNotEqual(build().fingerprint, build(url="https://other.test").fingerprint)

    def test_describe_target_mentions_the_url(self):
        self.assertIn("example.com", build().describe_target())


class UrlValidationTests(unittest.TestCase):
    def test_url_is_required(self):
        with self.assertRaises(ConfigError):
            from_mapping({"extractor": {"kind": "full_text"}}, base_dir=Path("/base"))

    def test_empty_url_is_rejected(self):
        with self.assertRaises(ConfigError):
            build(url="   ")

    def test_a_file_url_is_refused(self):
        # Left unchecked this would point the watcher at the local disk.
        with self.assertRaises(ConfigError):
            build(url="file:///etc/passwd")

    def test_a_url_without_a_scheme_is_refused(self):
        with self.assertRaises(ConfigError):
            build(url="example.com/product")

    def test_a_url_without_a_host_is_refused(self):
        with self.assertRaises(ConfigError):
            build(url="https:///just-a-path")


class ExtractorValidationTests(unittest.TestCase):
    def test_extractor_is_required(self):
        with self.assertRaises(ConfigError):
            from_mapping({"url": "https://example.com"}, base_dir=Path("/base"))

    def test_a_broken_extractor_is_reported_here(self):
        with self.assertRaises(ConfigError):
            build(extractor={"kind": "regex", "pattern": "([unclosed"})

    def test_an_unknown_extractor_kind_is_reported_here(self):
        with self.assertRaises(ConfigError):
            build(extractor={"kind": "css", "selector": ".price"})


class FieldValidationTests(unittest.TestCase):
    def test_unknown_top_level_key_is_rejected(self):
        # "intervals_seconds" would otherwise be ignored and the default used.
        with self.assertRaises(ConfigError) as caught:
            build(intervals_seconds=60)
        self.assertIn("intervals_seconds", str(caught.exception))

    def test_interval_must_be_a_whole_number(self):
        with self.assertRaises(ConfigError):
            build(interval_seconds="60")

    def test_interval_must_be_positive(self):
        with self.assertRaises(ConfigError):
            build(interval_seconds=0)

    def test_boolean_is_not_accepted_as_an_interval(self):
        with self.assertRaises(ConfigError):
            build(interval_seconds=True)

    def test_timeout_must_be_positive(self):
        with self.assertRaises(ConfigError):
            build(timeout_seconds=0)

    def test_timeout_may_be_fractional(self):
        self.assertEqual(build(timeout_seconds=2.5).timeout_seconds, 2.5)

    def test_user_agent_must_not_be_empty(self):
        with self.assertRaises(ConfigError):
            build(user_agent="  ")

    def test_the_config_must_be_an_object(self):
        with self.assertRaises(ConfigError):
            from_mapping([1, 2, 3], base_dir=Path("/base"))


class PathResolutionTests(unittest.TestCase):
    def test_relative_paths_are_anchored_to_the_config_file(self):
        # A scheduler runs the app from an arbitrary directory; anchoring to the
        # config keeps it reading and writing the same files regardless.
        config = from_mapping(
            {**VALID, "state_path": "state/snapshot.json"},
            base_dir=Path("/base"),
        )
        self.assertEqual(config.state_path, Path("/base/state/snapshot.json"))

    def test_absolute_paths_are_left_alone(self):
        absolute = Path(tempfile.gettempdir()).resolve() / "snapshot.json"
        config = from_mapping(
            {**VALID, "state_path": str(absolute)}, base_dir=Path("/base")
        )
        self.assertEqual(config.state_path, absolute)

    def test_an_empty_path_is_rejected(self):
        with self.assertRaises(ConfigError):
            build(log_path="")


class LoadFromDiskTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)

    def test_loads_a_real_file(self):
        path = self.dir / "config.json"
        path.write_text(json.dumps(VALID), encoding="utf-8")
        self.assertEqual(load(path).url, VALID["url"])
        self.assertEqual(load(path).source, path)

    def test_paths_resolve_next_to_the_config_file(self):
        path = self.dir / "config.json"
        path.write_text(json.dumps(VALID), encoding="utf-8")
        self.assertEqual(load(path).state_path, self.dir / "state" / "snapshot.json")

    def test_a_missing_file_says_how_to_create_one(self):
        with self.assertRaises(ConfigError) as caught:
            load(self.dir / "config.json")
        self.assertIn("config.example.json", str(caught.exception))

    def test_malformed_json_is_reported_clearly(self):
        path = self.dir / "config.json"
        path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(ConfigError):
            load(path)


if __name__ == "__main__":
    unittest.main()
