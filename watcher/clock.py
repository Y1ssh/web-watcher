"""Timestamps for the run log, the stored snapshot, and the rate limits.

Kept in one place so tests can substitute a fixed value instead of reaching for
the real clock, and so every timestamp the project writes has the same shape.
"""

from datetime import datetime, timezone


def now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``.

    UTC avoids the daylight-saving jumps that make a local-time run log lie
    about how far apart two checks were.
    """
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return stamp.replace("+00:00", "Z")


def parse_iso(stamp: str) -> datetime:
    """Parse a timestamp written by `now_iso` back into a datetime.

    Raises ValueError on anything unparseable. `datetime.fromisoformat` only
    learned to accept a trailing ``Z`` in 3.11, so it is normalised first and
    this keeps working on 3.10.
    """
    text = stamp.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        # Everything this project writes is UTC; assume that for older files.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def local_hour() -> int:
    """The hour of the day, 0-23, in the machine's own timezone.

    Action time windows are the one place local time is the right answer:
    "only between 9am and 5pm" means the user's 9am, not UTC's.
    """
    return datetime.now().hour
