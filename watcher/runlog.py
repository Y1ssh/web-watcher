"""Append-only record of every run.

The log is the evidence. A schedule that looks configured but never fires is
worthless, and the only way to tell the difference is a timestamped line per
run that you can read back afterwards.

One JSON object per line (JSON Lines), so the file stays appendable, greppable,
and parseable without loading the whole history into memory.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def append(path: Path, entry: Mapping[str, Any]) -> None:
    """Add one entry to the run log, creating the file if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(entry), sort_keys=True, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def read(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    """Return log entries oldest-first, or the last `limit` of them.

    Unparseable lines are skipped rather than raising: a truncated final line
    from an interrupted write should not make the whole history unreadable.
    """
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    if limit is not None and limit >= 0:
        return entries[-limit:] if limit else []
    return entries
