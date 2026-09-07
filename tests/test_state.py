"""The stored snapshot is the app's only memory, so it has to be trustworthy."""

import json
import tempfile
import unittest
from pathlib import Path

from watcher.errors import StateError
from watcher.state import (
    STATE_VERSION,
    Snapshot,
    clear,
    fingerprint,
    hash_value,
    load,
    save,
)

SAMPLE = Snapshot(
    url="https://example.com/product",
    fingerprint="abc",
    value="In Stock",
    value_hash=hash_value("In Stock"),
    first_seen_at="2000-01-01T00:00:00Z",
    last_checked_at="2000-01-01T00:15:00Z",
    last_changed_at=None,
    check_count=2,
    change_count=0,
)


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.dir = Path(self._temp.name)
        self.path = self.dir / "state" / "snapshot.json"


class SaveAndLoadTests(TempDirTestCase):
    def test_round_trip(self):
        save(self.path, SAMPLE)
        self.assertEqual(load(self.path), SAMPLE)

    def test_missing_file_means_no_previous_run(self):
        self.assertIsNone(load(self.path))

    def test_save_creates_missing_directories(self):
        save(self.path, SAMPLE)
        self.assertTrue(self.path.exists())

    def test_saved_file_is_readable_json_a_human_can_check(self):
        save(self.path, SAMPLE)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], STATE_VERSION)
        self.assertEqual(data["snapshot"]["value"], "In Stock")

    def test_save_leaves_no_temporary_files_behind(self):
        save(self.path, SAMPLE)
        save(self.path, SAMPLE)
        leftovers = [p.name for p in self.path.parent.iterdir() if ".tmp" in p.name]
        self.assertEqual(leftovers, [])

    def test_saving_twice_overwrites_rather_than_appends(self):
        save(self.path, SAMPLE)
        updated = Snapshot(**{**SAMPLE.to_dict(), "value": "Sold Out"})
        save(self.path, updated)
        self.assertEqual(load(self.path).value, "Sold Out")

    def test_non_ascii_values_survive_the_round_trip(self):
        snapshot = Snapshot(**{**SAMPLE.to_dict(), "value": "10 € – en réassort"})
        save(self.path, snapshot)
        self.assertEqual(load(self.path).value, "10 € – en réassort")


class CorruptStateTests(TempDirTestCase):
    def _write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_invalid_json_raises_instead_of_silently_re_baselining(self):
        # A silent reset here would make the watcher report "first run" forever
        # and never notice a real change.
        self._write("{not json")
        with self.assertRaises(StateError):
            load(self.path)

    def test_a_json_array_is_rejected(self):
        self._write("[]")
        with self.assertRaises(StateError):
            load(self.path)

    def test_a_future_state_version_is_rejected(self):
        self._write(json.dumps({"version": STATE_VERSION + 1, "snapshot": {}}))
        with self.assertRaises(StateError):
            load(self.path)

    def test_a_missing_field_is_reported_by_name(self):
        partial = SAMPLE.to_dict()
        del partial["value_hash"]
        self._write(json.dumps({"version": STATE_VERSION, "snapshot": partial}))
        with self.assertRaises(StateError) as caught:
            load(self.path)
        self.assertIn("value_hash", str(caught.exception))

    def test_a_wrongly_typed_field_is_rejected(self):
        broken = {**SAMPLE.to_dict(), "value": 42}
        self._write(json.dumps({"version": STATE_VERSION, "snapshot": broken}))
        with self.assertRaises(StateError):
            load(self.path)

    def test_a_negative_counter_is_rejected(self):
        broken = {**SAMPLE.to_dict(), "check_count": -1}
        self._write(json.dumps({"version": STATE_VERSION, "snapshot": broken}))
        with self.assertRaises(StateError):
            load(self.path)

    def test_a_boolean_counter_is_rejected(self):
        broken = {**SAMPLE.to_dict(), "change_count": True}
        self._write(json.dumps({"version": STATE_VERSION, "snapshot": broken}))
        with self.assertRaises(StateError):
            load(self.path)


class VersionTwoTests(TempDirTestCase):
    """Slice 2 added notification and action bookkeeping to the snapshot."""

    def test_new_fields_default_to_empty(self):
        self.assertIsNone(SAMPLE.last_notified_hash)
        self.assertIsNone(SAMPLE.last_notified_at)
        self.assertEqual(SAMPLE.action_runs, ())
        self.assertEqual(SAMPLE.action_total_runs, 0)

    def test_new_fields_survive_a_round_trip(self):
        snapshot = Snapshot(**{
            **SAMPLE.to_dict(),
            "last_notified_hash": "deadbeef",
            "last_notified_at": "2000-01-01T00:20:00Z",
            "action_runs": ("2000-01-01T00:20:00Z",),
            "action_total_runs": 1,
        })
        save(self.path, snapshot)
        loaded = load(self.path)
        self.assertEqual(loaded.last_notified_hash, "deadbeef")
        self.assertEqual(loaded.action_runs, ("2000-01-01T00:20:00Z",))
        self.assertEqual(loaded.action_total_runs, 1)

    def test_action_runs_is_stored_as_a_list_and_read_back_as_a_tuple(self):
        save(self.path, Snapshot(**{**SAMPLE.to_dict(), "action_runs": ("a", "b")}))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["snapshot"]["action_runs"], ["a", "b"])
        self.assertEqual(load(self.path).action_runs, ("a", "b"))

    def test_a_version_one_file_is_upgraded_rather_than_rejected(self):
        # Losing the baseline on an upgrade would mean missing the next real
        # change, so a v1 snapshot is read with the new fields defaulted.
        v1 = {
            name: value
            for name, value in SAMPLE.to_dict().items()
            if name not in (
                "last_notified_hash",
                "last_notified_at",
                "action_runs",
                "action_total_runs",
            )
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"version": 1, "snapshot": v1}), encoding="utf-8"
        )
        loaded = load(self.path)
        self.assertEqual(loaded.value, "In Stock")
        self.assertEqual(loaded.check_count, 2)
        self.assertIsNone(loaded.last_notified_hash)
        self.assertEqual(loaded.action_runs, ())

    def test_an_upgraded_snapshot_is_written_back_as_the_current_version(self):
        v1 = {
            name: value
            for name, value in SAMPLE.to_dict().items()
            if name not in (
                "last_notified_hash",
                "last_notified_at",
                "action_runs",
                "action_total_runs",
            )
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"version": 1, "snapshot": v1}), encoding="utf-8"
        )
        save(self.path, load(self.path))
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["version"], STATE_VERSION)

    def test_a_malformed_action_runs_list_is_rejected(self):
        broken = {**SAMPLE.to_dict(), "action_runs": [1, 2, 3]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"version": STATE_VERSION, "snapshot": broken}),
            encoding="utf-8",
        )
        with self.assertRaises(StateError):
            load(self.path)

    def test_a_wrongly_typed_notify_hash_is_rejected(self):
        broken = {**SAMPLE.to_dict(), "last_notified_hash": 42}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"version": STATE_VERSION, "snapshot": broken}),
            encoding="utf-8",
        )
        with self.assertRaises(StateError):
            load(self.path)


class ClearTests(TempDirTestCase):
    def test_clear_removes_the_file(self):
        save(self.path, SAMPLE)
        self.assertTrue(clear(self.path))
        self.assertFalse(self.path.exists())

    def test_clear_on_a_missing_file_reports_that_nothing_happened(self):
        self.assertFalse(clear(self.path))


class HashTests(unittest.TestCase):
    def test_same_value_hashes_the_same(self):
        self.assertEqual(hash_value("In Stock"), hash_value("In Stock"))

    def test_different_values_hash_differently(self):
        self.assertNotEqual(hash_value("In Stock"), hash_value("Sold Out"))


class FingerprintTests(unittest.TestCase):
    def test_same_target_gives_the_same_fingerprint(self):
        a = fingerprint("https://example.com", {"kind": "element", "tag": "h1"})
        b = fingerprint("https://example.com", {"kind": "element", "tag": "h1"})
        self.assertEqual(a, b)

    def test_key_order_does_not_matter(self):
        a = fingerprint("https://example.com", {"kind": "element", "tag": "h1"})
        b = fingerprint("https://example.com", {"tag": "h1", "kind": "element"})
        self.assertEqual(a, b)

    def test_a_different_url_gives_a_different_fingerprint(self):
        a = fingerprint("https://example.com/a", {"kind": "full_text"})
        b = fingerprint("https://example.com/b", {"kind": "full_text"})
        self.assertNotEqual(a, b)

    def test_a_different_extractor_gives_a_different_fingerprint(self):
        a = fingerprint("https://example.com", {"kind": "element", "tag": "h1"})
        b = fingerprint("https://example.com", {"kind": "element", "tag": "h2"})
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
