"""The run log is the evidence that the schedule actually fired."""

import tempfile
import unittest
from pathlib import Path

from watcher.runlog import append, read


class RunLogTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "logs" / "runs.jsonl"

    def test_round_trip(self):
        append(self.path, {"event": "check", "outcome": "baseline"})
        self.assertEqual(read(self.path), [{"event": "check", "outcome": "baseline"}])

    def test_creates_missing_directories(self):
        append(self.path, {"event": "check"})
        self.assertTrue(self.path.exists())

    def test_entries_are_kept_oldest_first(self):
        for index in range(3):
            append(self.path, {"n": index})
        self.assertEqual([entry["n"] for entry in read(self.path)], [0, 1, 2])

    def test_each_entry_is_one_line(self):
        append(self.path, {"a": 1})
        append(self.path, {"b": 2})
        lines = self.path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)

    def test_limit_returns_the_most_recent_entries(self):
        for index in range(5):
            append(self.path, {"n": index})
        self.assertEqual([entry["n"] for entry in read(self.path, limit=2)], [3, 4])

    def test_a_limit_larger_than_the_log_returns_everything(self):
        append(self.path, {"n": 0})
        self.assertEqual(len(read(self.path, limit=10)), 1)

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(read(self.path), [])

    def test_a_truncated_final_line_does_not_break_the_history(self):
        # An interrupted write should cost one entry, not the whole log.
        append(self.path, {"n": 0})
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write('{"n": 1')
        self.assertEqual([entry["n"] for entry in read(self.path)], [0])

    def test_non_ascii_values_survive(self):
        append(self.path, {"value": "10 € – en réassort"})
        self.assertEqual(read(self.path)[0]["value"], "10 € – en réassort")


if __name__ == "__main__":
    unittest.main()
