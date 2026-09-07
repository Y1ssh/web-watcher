"""One complete cycle: fetch, extract, compare, decide, notify, act, store, log.

Kept separate from the command line so the whole thing can be driven from a
test with a fake fetcher, a fake clock, and channels that record instead of
send.

Ordering matters here. The snapshot is written **once**, at the end, carrying
the change, the notification bookkeeping, and the action bookkeeping together.
A half-written state -- value updated but "already notified" not recorded --
would re-alert on the next run.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from . import actions, clock, conditions, detect, extract, fetching, notify, runlog, state
from .config import Config
from .errors import ExtractionError, FetchError
from .state import Snapshot

Fetcher = Callable[..., fetching.Page]


@dataclass(frozen=True)
class Check:
    """Everything that happened during one check."""

    result: detect.CheckResult
    page_url: str
    status: int
    decision: conditions.Decision
    deliveries: tuple[notify.Delivery, ...] = ()
    action: actions.ActionOutcome | None = None

    @property
    def outcome(self) -> detect.Outcome:
        return self.result.outcome

    @property
    def value(self) -> str:
        return self.result.value

    @property
    def previous_value(self) -> str | None:
        return self.result.previous_value

    @property
    def snapshot(self) -> Snapshot:
        return self.result.snapshot

    @property
    def notified(self) -> bool:
        return any(delivery.sent for delivery in self.deliveries)


def run_check(
    config: Config,
    *,
    fetcher: Fetcher | None = None,
    now_fn: Callable[[], str] = clock.now_iso,
    confirm_fn: Callable[[str], bool] | None = None,
) -> Check:
    """Perform one check and persist everything it decided.

    Raises FetchError if the page could not be retrieved and ExtractionError if
    it was retrieved but no longer contains the watched value. Both are written
    to the run log before being re-raised, so an unattended watch leaves a
    record of why a run produced nothing.
    """
    fetch = fetcher if fetcher is not None else fetching.fetch
    extractor = extract.build_extractor(config.extractor)
    condition = config.build_condition()

    try:
        page = fetch(
            config.url,
            timeout=config.timeout_seconds,
            user_agent=config.user_agent,
        )
        value = extractor(page.html)
    except (FetchError, ExtractionError) as exc:
        runlog.append(
            config.log_path,
            {
                "at": now_fn(),
                "event": "error",
                "url": config.url,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )
        raise

    now = now_fn()
    previous = state.load(config.state_path)
    result = detect.evaluate(
        previous,
        url=config.url,
        fingerprint=config.fingerprint,
        value=value,
        now=now,
    )
    snapshot = result.snapshot

    decision = condition(
        conditions.Context(
            outcome=result.outcome,
            value=result.value,
            previous_value=result.previous_value,
        )
    )

    deliveries, snapshot = _notify(config, result, decision, snapshot, now)
    action_outcome, snapshot = _act(config, decision, snapshot, now, confirm_fn)

    state.save(config.state_path, snapshot)
    _log(config, result, decision, snapshot, page, deliveries, action_outcome)

    return Check(
        result=replace(result, snapshot=snapshot),
        page_url=page.url,
        status=page.status,
        decision=decision,
        deliveries=tuple(deliveries),
        action=action_outcome,
    )


def _notify(
    config: Config,
    result: detect.CheckResult,
    decision: conditions.Decision,
    snapshot: Snapshot,
    now: str,
) -> tuple[list[notify.Delivery], Snapshot]:
    """Send an alert if the condition held and this value is not old news."""
    if not config.notify_channels:
        return [], snapshot

    if decision.is_unknown:
        if not config.warn_on_unknown:
            return [], snapshot
        note = notify.build_notification(
            _context(config, result, decision, now),
            subject_template="Web Watcher could not evaluate its condition",
            body_template=(
                "{url}\n\n"
                "value:   {value}\n"
                "problem: {condition_reason}\n"
                "checked: {at}\n\n"
                "No action was taken."
            ),
        )
        # A warning is not a match, so it does not consume the once-per-value
        # slot -- the real alert must still fire once the value becomes readable.
        return notify.deliver(list(config.notify_channels), note), snapshot

    if not decision.is_true:
        return [], snapshot

    if (
        config.notify_once_per_value
        and snapshot.last_notified_hash == snapshot.value_hash
    ):
        return [], snapshot

    note = notify.build_notification(
        _context(config, result, decision, now),
        subject_template=config.notify_subject,
        body_template=config.notify_body,
    )
    deliveries = notify.deliver(list(config.notify_channels), note)

    # Only record the alert if something actually got through; otherwise the
    # next run should try again rather than treat it as delivered.
    if any(delivery.sent for delivery in deliveries):
        snapshot = replace(
            snapshot, last_notified_hash=snapshot.value_hash, last_notified_at=now
        )
    return deliveries, snapshot


def _act(
    config: Config,
    decision: conditions.Decision,
    snapshot: Snapshot,
    now: str,
    confirm_fn: Callable[[str], bool] | None,
) -> tuple[actions.ActionOutcome | None, Snapshot]:
    """Run the configured action, if the condition held and the guards allow."""
    if config.action is None:
        return None, snapshot

    if not decision.is_true:
        reason = (
            "the condition could not be evaluated"
            if decision.is_unknown
            else "the condition was not met"
        )
        return (
            actions.ActionOutcome(
                actions.ActionStatus.SKIPPED, reason, config.action.describe()
            ),
            snapshot,
        )

    outcome = actions.run_action(
        config.action,
        now=now,
        previous_runs=snapshot.action_runs,
        total_runs=snapshot.action_total_runs,
        confirm_fn=confirm_fn,
    )

    # Only a real, successful action is recorded. A dry run, a blocked run, and
    # a failed attempt all leave the guards exactly as they were.
    if outcome.took_effect:
        snapshot = replace(
            snapshot,
            action_runs=actions.record_run(snapshot.action_runs, now),
            action_total_runs=snapshot.action_total_runs + 1,
        )
    return outcome, snapshot


def _context(
    config: Config,
    result: detect.CheckResult,
    decision: conditions.Decision,
    now: str,
) -> dict[str, str]:
    """The placeholders available in a notification template."""
    return {
        "url": config.url,
        "value": result.value,
        "previous_value": (
            "(nothing stored yet)"
            if result.previous_value is None
            else result.previous_value
        ),
        "outcome": result.outcome.value,
        "condition": config.describe_condition(),
        "condition_reason": decision.reason,
        "at": now,
    }


def _log(
    config: Config,
    result: detect.CheckResult,
    decision: conditions.Decision,
    snapshot: Snapshot,
    page: fetching.Page,
    deliveries: list[notify.Delivery],
    action_outcome: actions.ActionOutcome | None,
) -> None:
    runlog.append(
        config.log_path,
        {
            "at": snapshot.last_checked_at,
            "event": "check",
            "outcome": result.outcome.value,
            "url": config.url,
            "final_url": page.url,
            "status": page.status,
            "value": extract.truncate(result.value),
            "previous_value": (
                None
                if result.previous_value is None
                else extract.truncate(result.previous_value)
            ),
            "value_hash": snapshot.value_hash,
            "check_count": snapshot.check_count,
            "change_count": snapshot.change_count,
            "condition": decision.verdict.value,
            "condition_reason": decision.reason,
            "notified": [delivery.summary for delivery in deliveries],
            "action": None if action_outcome is None else action_outcome.status.value,
            "action_reason": None if action_outcome is None else action_outcome.reason,
        },
    )
