"""Decide what a freshly captured value means, given what was stored before.

This module is deliberately pure: no network, no disk, no clock. Everything it
needs arrives as an argument, so all four outcomes can be tested exhaustively
without a live website.
"""

from dataclasses import dataclass, replace
from enum import Enum

from .state import Snapshot, hash_value


class Outcome(str, Enum):
    """The four things a single check can conclude."""

    #: Nothing was stored, so this run only records a starting point.
    BASELINE = "baseline"
    #: The captured value matches the stored one.
    UNCHANGED = "unchanged"
    #: The captured value differs from the stored one.
    CHANGED = "changed"
    #: The config now watches something else, so the old value is not comparable.
    RETARGETED = "retargeted"


@dataclass(frozen=True)
class CheckResult:
    """The conclusion of one check, plus the snapshot to store for next time."""

    outcome: Outcome
    value: str
    previous_value: str | None
    snapshot: Snapshot

    @property
    def changed(self) -> bool:
        return self.outcome is Outcome.CHANGED


def evaluate(
    previous: Snapshot | None,
    *,
    url: str,
    fingerprint: str,
    value: str,
    now: str,
) -> CheckResult:
    """Compare a captured value against the stored snapshot."""
    value_hash = hash_value(value)

    if previous is None:
        return CheckResult(
            outcome=Outcome.BASELINE,
            value=value,
            previous_value=None,
            snapshot=_fresh(url, fingerprint, value, value_hash, now),
        )

    if previous.fingerprint != fingerprint:
        return CheckResult(
            outcome=Outcome.RETARGETED,
            value=value,
            previous_value=previous.value,
            snapshot=_fresh(url, fingerprint, value, value_hash, now),
        )

    if previous.value_hash == value_hash:
        return CheckResult(
            outcome=Outcome.UNCHANGED,
            value=value,
            previous_value=previous.value,
            snapshot=replace(
                previous,
                last_checked_at=now,
                check_count=previous.check_count + 1,
            ),
        )

    return CheckResult(
        outcome=Outcome.CHANGED,
        value=value,
        previous_value=previous.value,
        snapshot=replace(
            previous,
            value=value,
            value_hash=value_hash,
            last_checked_at=now,
            last_changed_at=now,
            check_count=previous.check_count + 1,
            change_count=previous.change_count + 1,
        ),
    )


def _fresh(
    url: str, fingerprint: str, value: str, value_hash: str, now: str
) -> Snapshot:
    return Snapshot(
        url=url,
        fingerprint=fingerprint,
        value=value,
        value_hash=value_hash,
        first_seen_at=now,
        last_checked_at=now,
        last_changed_at=None,
        check_count=1,
        change_count=0,
    )
