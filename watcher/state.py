"""The snapshot of the last run -- the memory that makes change detection possible.

Without a stored previous value the watcher has nothing to compare against and
can never report a change. The snapshot is written as JSON so you can open it
and check what the app actually recorded, which is the manual verification step
that catches a watcher quietly reading the wrong part of a page.
"""

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import StateError

#: Bumped whenever the stored shape changes.
#: v1 = slice 1 (change detection only). v2 adds the notification and action
#: bookkeeping. A v1 file is upgraded on read rather than rejected: losing the
#: baseline on an upgrade would mean missing the next real change.
STATE_VERSION = 2
READABLE_VERSIONS = (1, 2)

_TEXT_FIELDS = (
    "url",
    "fingerprint",
    "value",
    "value_hash",
    "first_seen_at",
    "last_checked_at",
)
_COUNT_FIELDS = ("check_count", "change_count", "action_total_runs")

#: Added in v2. A v1 snapshot is read with these defaults.
_V2_DEFAULTS: dict[str, Any] = {
    "last_notified_hash": None,
    "last_notified_at": None,
    "action_runs": (),
    "action_total_runs": 0,
}


@dataclass(frozen=True)
class Snapshot:
    """What the watcher saw last time, plus a little history."""

    url: str
    fingerprint: str
    value: str
    value_hash: str
    first_seen_at: str
    last_checked_at: str
    last_changed_at: str | None
    check_count: int
    change_count: int

    #: Hash of the value the last notification was sent about. This is what
    #: makes an alert fire once per change instead of on every single check.
    last_notified_hash: str | None = None
    last_notified_at: str | None = None

    #: Timestamps of real (non-dry-run) action runs, oldest first, capped.
    #: Persisted so the rate limit and run-once guard survive a restart.
    action_runs: tuple[str, ...] = ()
    action_total_runs: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["action_runs"] = list(self.action_runs)
        return data

    @classmethod
    def from_dict(cls, data: Any, source: Path | None = None) -> "Snapshot":
        where = f" in {source}" if source is not None else ""
        if not isinstance(data, Mapping):
            raise StateError(f"snapshot{where} is not an object")

        fields = tuple(cls.__dataclass_fields__)
        required = [name for name in fields if name not in _V2_DEFAULTS]
        missing = sorted(set(required) - set(data))
        if missing:
            raise StateError(
                f"snapshot{where} is missing field(s): {', '.join(missing)}"
            )
        values = {
            name: data.get(name, _V2_DEFAULTS.get(name)) for name in fields
        }

        for name in _TEXT_FIELDS:
            if not isinstance(values[name], str):
                raise StateError(f"snapshot{where}: {name!r} must be a string")

        for name in ("last_changed_at", "last_notified_hash", "last_notified_at"):
            value = values[name]
            if value is not None and not isinstance(value, str):
                raise StateError(
                    f"snapshot{where}: {name!r} must be a string or null"
                )

        runs = values["action_runs"]
        if not isinstance(runs, (list, tuple)) or any(
            not isinstance(item, str) for item in runs
        ):
            raise StateError(
                f"snapshot{where}: 'action_runs' must be a list of timestamps"
            )
        values["action_runs"] = tuple(runs)

        for name in _COUNT_FIELDS:
            count = values[name]
            # bool is a subclass of int, so reject it explicitly.
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise StateError(
                    f"snapshot{where}: {name!r} must be a whole number"
                )

        return cls(**values)


def hash_value(value: str) -> str:
    """Return a stable fingerprint of a captured value."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(url: str, extractor: Mapping[str, Any]) -> str:
    """Identify *what is being watched*, so a retarget is not read as a change.

    If you point the watcher at a different page, or change which element it
    reads, the new value has nothing to do with the old one. Comparing them
    would announce a change that never happened on the site. Storing this
    fingerprint next to the value lets the watcher notice that the question
    itself changed and re-baseline instead.
    """
    payload = json.dumps(
        {"url": url, "extractor": dict(extractor)},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load(path: Path) -> Snapshot | None:
    """Read the stored snapshot, or return None when there is not one yet.

    A corrupt file raises rather than silently starting over: a watcher that
    quietly re-baselines every run would report "first run" forever and never
    tell you about a change.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"could not read {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(
            f"{path} is not valid JSON ({exc}). Delete the file to start from a "
            f"fresh baseline."
        ) from exc

    if not isinstance(data, Mapping):
        raise StateError(f"{path} does not contain a JSON object")

    version = data.get("version")
    if version not in READABLE_VERSIONS:
        readable = ", ".join(str(v) for v in READABLE_VERSIONS)
        raise StateError(
            f"{path} was written by state version {version!r}, but this build "
            f"reads version(s) {readable}. Delete the file to re-baseline."
        )
    # A v1 snapshot is upgraded in place by from_dict's defaults, and written
    # back as v2 on the next save.
    return Snapshot.from_dict(data.get("snapshot"), source=path)


def save(path: Path, snapshot: Snapshot) -> None:
    """Write the snapshot atomically.

    The write goes to a temporary file in the same directory and is then moved
    into place, so a run interrupted mid-write leaves the previous snapshot
    intact instead of a half-written file the next run cannot read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": STATE_VERSION, "snapshot": snapshot.to_dict()}
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def clear(path: Path) -> bool:
    """Delete the stored snapshot. Returns True if a file was removed."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
