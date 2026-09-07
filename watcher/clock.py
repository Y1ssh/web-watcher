"""Timestamps for the run log and the stored snapshot.

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
