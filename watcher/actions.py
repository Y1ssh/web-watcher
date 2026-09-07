"""Doing something when the condition is met, behind several safety catches.

An alert is harmless if it fires wrongly. An action is not: it submits a form,
books a slot, posts to a service. Everything here is arranged so the dangerous
default is impossible to reach by accident.

* `dry_run` is **on** unless the config explicitly turns it off, and the CLI can
  only ever turn it back on. There is no flag that makes the watcher act for
  real against a config that did not ask for it.
* Blocking guards are evaluated *before* the dry-run check, so a rehearsal tells
  you truthfully whether the real thing would have been stopped.
* A performed action records itself in the snapshot, so `run_once` and the
  rolling rate limit survive a restart.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import Enum
from typing import Any

from . import clock, secrets
from .errors import ConfigError, WatcherError
from .fetching import ALLOWED_SCHEMES, DEFAULT_USER_AGENT

ACTION_KINDS = ("http_request",)

DEFAULT_ACTION_TIMEOUT = 30.0
DEFAULT_MAX_PER_24H = 1

#: Keep the stored history small; only the last day of it is ever consulted.
MAX_RECORDED_RUNS = 50

_ACTION_KEYS = frozenset({
    "kind", "method", "url", "fields", "encoding", "headers",
    "timeout_seconds", "safeguards",
})
_SAFEGUARD_KEYS = frozenset({
    "dry_run", "run_once", "max_per_24h", "hours", "confirm",
})
_METHODS = ("POST", "GET", "PUT", "PATCH")
_ENCODINGS = ("form", "json")

#: Config keys that would mean a credential was written into the file.
_FORBIDDEN_SECRET_KEYS = ("password", "token", "secret", "api_key", "authorization")

#: ${NAME} in a header value, resolved from the environment at send time.
_ENV_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ActionError(WatcherError):
    """The action was attempted and failed."""


class ActionStatus(str, Enum):
    #: The condition was not met, or no action is configured.
    SKIPPED = "skipped"
    #: Everything was prepared but nothing was sent, because of dry-run mode.
    DRY_RUN = "dry_run"
    #: A safeguard stopped it.
    BLOCKED = "blocked"
    #: It really happened.
    PERFORMED = "performed"
    #: It was attempted for real and failed.
    FAILED = "failed"


@dataclass(frozen=True)
class ActionOutcome:
    status: ActionStatus
    reason: str
    description: str = ""

    @property
    def took_effect(self) -> bool:
        return self.status is ActionStatus.PERFORMED


@dataclass(frozen=True)
class Safeguards:
    """The limits standing between a met condition and a real-world effect."""

    dry_run: bool = True
    run_once: bool = True
    max_per_24h: int = DEFAULT_MAX_PER_24H
    hours: tuple[int, int] | None = None
    confirm: bool = False


@dataclass(frozen=True)
class Action:
    """A single HTTP request to make when the condition is met."""

    method: str
    url: str
    fields: dict[str, str] = field(default_factory=dict)
    encoding: str = "form"
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_ACTION_TIMEOUT
    safeguards: Safeguards = field(default_factory=Safeguards)

    def describe(self) -> str:
        names = ", ".join(sorted(self.fields)) or "no fields"
        return f"{self.method} {self.url} ({names})"

    def required_env(self) -> tuple[str, ...]:
        """Environment variables named by ${...} placeholders in the headers.

        The pre-launch check reads this so a missing token is found before
        deployment, not at the moment the action tries to fire.
        """
        names: list[str] = []
        for value in self.headers.values():
            for match in _ENV_PLACEHOLDER.finditer(value):
                if match.group(1) not in names:
                    names.append(match.group(1))
        return tuple(names)


def build_action(spec: Any, *, force_dry_run: bool = False) -> Action:
    """Validate an action specification.

    `force_dry_run` can only ever make the action safer; nothing in this module
    can turn dry-run off when the config asked for it.
    """
    if not isinstance(spec, Mapping):
        raise ConfigError("'action' must be an object")

    kind = spec.get("kind")
    if kind not in ACTION_KINDS:
        raise ConfigError(
            f"action 'kind' must be one of {', '.join(ACTION_KINDS)}; got {kind!r}"
        )

    for key in _FORBIDDEN_SECRET_KEYS:
        if key in spec:
            raise ConfigError(
                f"action: {key!r} must not be written into the config file. "
                f"Put it in a header value as ${{ENV_VAR}} and keep the value "
                f"in .env."
            )

    unknown = sorted(set(spec) - _ACTION_KEYS)
    if unknown:
        raise ConfigError(
            f"action: unknown key(s) {', '.join(unknown)}; allowed keys are "
            f"{', '.join(sorted(_ACTION_KEYS))}"
        )

    method = str(spec.get("method", "POST")).upper()
    if method not in _METHODS:
        raise ConfigError(f"action 'method' must be one of {', '.join(_METHODS)}")

    url = spec.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError("action: 'url' is required")
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ConfigError("action: 'url' must start with http:// or https://")
    if not parts.netloc:
        raise ConfigError(f"action: 'url' has no host: {url!r}")

    encoding = str(spec.get("encoding", "form")).lower()
    if encoding not in _ENCODINGS:
        raise ConfigError(f"action 'encoding' must be one of {', '.join(_ENCODINGS)}")

    fields = _string_map(spec.get("fields", {}), "fields")
    headers = _string_map(spec.get("headers", {}), "headers")

    timeout = spec.get("timeout_seconds", DEFAULT_ACTION_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ConfigError("action: 'timeout_seconds' must be a positive number")

    safeguards = _build_safeguards(spec.get("safeguards", {}))
    if force_dry_run:
        safeguards = replace(safeguards, dry_run=True)

    return Action(
        method=method,
        url=url,
        fields=fields,
        encoding=encoding,
        headers=headers,
        timeout_seconds=float(timeout),
        safeguards=safeguards,
    )


def _build_safeguards(spec: Any) -> Safeguards:
    if not isinstance(spec, Mapping):
        raise ConfigError("action: 'safeguards' must be an object")
    unknown = sorted(set(spec) - _SAFEGUARD_KEYS)
    if unknown:
        raise ConfigError(
            f"action safeguards: unknown key(s) {', '.join(unknown)}; allowed "
            f"keys are {', '.join(sorted(_SAFEGUARD_KEYS))}"
        )

    for key in ("dry_run", "run_once", "confirm"):
        if key in spec and not isinstance(spec[key], bool):
            raise ConfigError(f"action safeguards: {key!r} must be true or false")

    max_per_24h = spec.get("max_per_24h", DEFAULT_MAX_PER_24H)
    if (
        isinstance(max_per_24h, bool)
        or not isinstance(max_per_24h, int)
        or max_per_24h < 1
    ):
        raise ConfigError("action safeguards: 'max_per_24h' must be 1 or more")

    hours = spec.get("hours")
    if hours is not None:
        if (
            not isinstance(hours, (list, tuple))
            or len(hours) != 2
            or any(isinstance(h, bool) or not isinstance(h, int) for h in hours)
            or not all(0 <= h <= 23 for h in hours)
        ):
            raise ConfigError(
                "action safeguards: 'hours' must be two hour numbers 0-23, "
                "for example [9, 17]"
            )
        hours = (int(hours[0]), int(hours[1]))

    return Safeguards(
        dry_run=bool(spec.get("dry_run", True)),
        run_once=bool(spec.get("run_once", True)),
        max_per_24h=max_per_24h,
        hours=hours,
        confirm=bool(spec.get("confirm", False)),
    )


def _string_map(value: Any, key: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"action: {key!r} must be an object of name/value pairs")
    result: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not name:
            raise ConfigError(f"action {key!r}: every name must be a string")
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            raise ConfigError(
                f"action {key!r}: value for {name!r} must be text or a number"
            )
        result[name] = str(item)
    return result


# -- guards ----------------------------------------------------------------


def within_hours(hour: int, window: tuple[int, int]) -> bool:
    """Is `hour` inside the window? Windows may wrap past midnight."""
    start, end = window
    if start <= end:
        return start <= hour <= end
    return hour >= start or hour <= end


def runs_in_last_24h(recorded: tuple[str, ...], now: str) -> int:
    """Count recorded runs within 24 hours of `now`.

    A rolling window rather than a calendar day: a calendar limit would happily
    allow two runs a minute apart either side of midnight.
    """
    try:
        cutoff = clock.parse_iso(now) - timedelta(hours=24)
    except ValueError:
        return len(recorded)
    total = 0
    for stamp in recorded:
        try:
            if clock.parse_iso(stamp) > cutoff:
                total += 1
        except ValueError:
            # An unreadable timestamp counts against you, never for you.
            total += 1
    return total


def check_guards(
    action: Action,
    *,
    now: str,
    previous_runs: tuple[str, ...],
    total_runs: int,
    confirm_fn: Callable[[str], bool] | None = None,
) -> ActionOutcome | None:
    """Return the outcome that blocks the action, or None if it may proceed."""
    guards = action.safeguards

    if guards.run_once and total_runs > 0:
        return ActionOutcome(
            ActionStatus.BLOCKED,
            f"run_once is on and this action already ran {total_runs} time(s). "
            f"Use `reset --action-only --yes` to allow it again.",
            action.describe(),
        )

    recent = runs_in_last_24h(previous_runs, now)
    if recent >= guards.max_per_24h:
        return ActionOutcome(
            ActionStatus.BLOCKED,
            f"the limit of {guards.max_per_24h} run(s) per 24 hours is already "
            f"used up ({recent} in the last day)",
            action.describe(),
        )

    if guards.hours is not None:
        hour = clock.local_hour()
        if not within_hours(hour, guards.hours):
            start, end = guards.hours
            return ActionOutcome(
                ActionStatus.BLOCKED,
                f"the local time is {hour:02d}:00, outside the permitted window "
                f"{start:02d}:00-{end:02d}:00",
                action.describe(),
            )

    if guards.confirm:
        if confirm_fn is None:
            return ActionOutcome(
                ActionStatus.BLOCKED,
                "confirmation is required but nothing is available to ask "
                "(this run is not interactive)",
                action.describe(),
            )
        if not confirm_fn(action.describe()):
            return ActionOutcome(
                ActionStatus.BLOCKED, "you did not confirm", action.describe()
            )

    return None


def run_action(
    action: Action,
    *,
    now: str,
    previous_runs: tuple[str, ...],
    total_runs: int,
    confirm_fn: Callable[[str], bool] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
) -> ActionOutcome:
    """Perform the action if every guard allows it."""
    blocked = check_guards(
        action,
        now=now,
        previous_runs=previous_runs,
        total_runs=total_runs,
        confirm_fn=confirm_fn,
    )
    if blocked is not None:
        return blocked

    if action.safeguards.dry_run:
        return ActionOutcome(
            ActionStatus.DRY_RUN,
            "dry run: every guard passed, but nothing was sent",
            action.describe(),
        )

    try:
        status = _perform(action, opener=opener)
    except ActionError as exc:
        return ActionOutcome(ActionStatus.FAILED, str(exc), action.describe())
    return ActionOutcome(
        ActionStatus.PERFORMED,
        f"the request was sent and returned HTTP {status}",
        action.describe(),
    )


def _perform(
    action: Action, *, opener: urllib.request.OpenerDirector | None = None
) -> int:
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        **{name: _expand(value) for name, value in action.headers.items()},
    }

    url = action.url
    data: bytes | None = None
    if action.method == "GET":
        if action.fields:
            separator = "&" if urllib.parse.urlsplit(url).query else "?"
            url = url + separator + urllib.parse.urlencode(action.fields)
    elif action.encoding == "json":
        data = json.dumps(action.fields).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
    else:
        data = urllib.parse.urlencode(action.fields).encode("utf-8")
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    request = urllib.request.Request(
        url, data=data, method=action.method, headers=headers
    )
    sender = opener or urllib.request.build_opener()
    try:
        with sender.open(request, timeout=action.timeout_seconds) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as exc:
        exc.close()
        raise ActionError(f"the target returned HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ActionError(f"could not reach {action.url}: {exc.reason}") from exc
    except OSError as exc:
        raise ActionError(f"the request failed: {exc}") from exc


def _expand(value: str) -> str:
    """Replace ${NAME} in a header with an environment variable's value.

    This is how an action carries an auth token without the token ever being
    written into the config file.
    """
    def substitute(match) -> str:
        name = match.group(1)
        return secrets.require(name, used_for=f"the ${{{name}}} header value")

    return _ENV_PLACEHOLDER.sub(substitute, value)


def record_run(previous: tuple[str, ...], now: str) -> tuple[str, ...]:
    """Append a run timestamp, keeping only the most recent entries."""
    return (*previous, now)[-MAX_RECORDED_RUNS:]
