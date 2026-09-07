"""One complete check: fetch, extract, compare, store, log.

Kept separate from the command line so the whole cycle can be driven from a
test with a fake fetcher and a fixed clock.
"""

from collections.abc import Callable
from dataclasses import dataclass

from . import clock, detect, extract, fetching, runlog, state
from .config import Config
from .errors import ExtractionError, FetchError
from .state import Snapshot

Fetcher = Callable[..., fetching.Page]


@dataclass(frozen=True)
class Check:
    """The outcome of a check, together with how the page responded."""

    result: detect.CheckResult
    page_url: str
    status: int

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


def run_check(
    config: Config,
    *,
    fetcher: Fetcher | None = None,
    now_fn: Callable[[], str] = clock.now_iso,
) -> Check:
    """Perform one check and persist its result.

    Raises FetchError if the page could not be retrieved and ExtractionError if
    it was retrieved but no longer contains the watched value. Both are written
    to the run log before being re-raised, so an unattended watch leaves a
    record of why a run produced nothing.
    """
    fetch = fetcher if fetcher is not None else fetching.fetch
    extractor = extract.build_extractor(config.extractor)

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

    previous = state.load(config.state_path)
    result = detect.evaluate(
        previous,
        url=config.url,
        fingerprint=config.fingerprint,
        value=value,
        now=now_fn(),
    )
    state.save(config.state_path, result.snapshot)

    runlog.append(
        config.log_path,
        {
            "at": result.snapshot.last_checked_at,
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
            "value_hash": result.snapshot.value_hash,
            "check_count": result.snapshot.check_count,
            "change_count": result.snapshot.change_count,
        },
    )

    return Check(result=result, page_url=page.url, status=page.status)
