"""Load and validate config.json.

Everything checkable without a live page is checked here, at startup, so a
mistake surfaces the moment you run the watcher rather than silently during an
unattended run hours later. Unknown keys are rejected too: a typo like
"intervals_seconds" would otherwise be ignored and the default used instead.
"""

import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import actions, conditions, extract, notify, state
from .errors import ConfigError
from .fetching import DEFAULT_TIMEOUT_SECONDS, DEFAULT_USER_AGENT

DEFAULT_CONFIG_FILENAME = "config.json"

#: Fifteen minutes. Often enough for most pages, gentle enough that the site
#: being watched has no reason to treat the watcher as abusive.
DEFAULT_INTERVAL_SECONDS = 900
MIN_INTERVAL_SECONDS = 1

DEFAULT_STATE_PATH = "state/snapshot.json"
DEFAULT_LOG_PATH = "logs/runs.jsonl"

#: With no condition set, the watcher behaves as it did in slice 1: it reacts
#: to any change and nothing else.
DEFAULT_CONDITION: dict[str, Any] = {"kind": "changed"}

ALLOWED_SCHEMES = frozenset({"http", "https"})

_ALLOWED_KEYS = frozenset({
    "url",
    "extractor",
    "interval_seconds",
    "timeout_seconds",
    "user_agent",
    "state_path",
    "log_path",
    "condition",
    "notify",
    "action",
})

_NOTIFY_KEYS = frozenset({
    "channels", "subject", "body", "once_per_value", "warn_on_unknown",
})


@dataclass(frozen=True)
class Config:
    """A validated configuration. Every path here is already absolute."""

    url: str
    extractor: dict[str, Any] = field(default_factory=dict)
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    user_agent: str = DEFAULT_USER_AGENT
    state_path: Path = Path(DEFAULT_STATE_PATH)
    log_path: Path = Path(DEFAULT_LOG_PATH)
    source: Path | None = None

    # -- slice 2 ----------------------------------------------------------
    condition: dict[str, Any] = field(
        default_factory=lambda: dict(DEFAULT_CONDITION)
    )
    notify_channels: tuple[notify.Channel, ...] = ()
    notify_subject: str = notify.DEFAULT_SUBJECT
    notify_body: str = notify.DEFAULT_BODY
    #: Alert once per distinct value, not once per check. Without this a
    #: fifteen-minute schedule would re-send the same alert ninety-six times.
    notify_once_per_value: bool = True
    #: Tell me when the condition could not be evaluated at all.
    warn_on_unknown: bool = True
    action: actions.Action | None = None

    @property
    def fingerprint(self) -> str:
        """Identifies what this config watches. See state.fingerprint.

        Deliberately covers only `url` and `extractor`. Editing a condition or
        a notification channel does not change what is being measured, so it
        must not throw away the baseline.
        """
        return state.fingerprint(self.url, self.extractor)

    def describe_target(self) -> str:
        return f"{extract.describe(self.extractor)} at {self.url}"

    def describe_condition(self) -> str:
        return conditions.describe(self.condition)

    def build_condition(self) -> conditions.Condition:
        return conditions.build_condition(self.condition)


def load(path: Path, *, force_dry_run: bool = False) -> Config:
    """Read and validate a config file."""
    if not path.exists():
        raise ConfigError(
            f"no config file at {path}. Copy config.example.json to "
            f"{DEFAULT_CONFIG_FILENAME} and edit it, or pass --config."
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc

    return from_mapping(
        data, base_dir=path.parent, source=path, force_dry_run=force_dry_run
    )


def from_mapping(
    data: Any,
    *,
    base_dir: Path,
    source: Path | None = None,
    force_dry_run: bool = False,
) -> Config:
    """Validate an already-parsed configuration mapping."""
    if not isinstance(data, Mapping):
        raise ConfigError("the configuration must be a JSON object")

    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS))
        raise ConfigError(
            f"unknown configuration key(s): {', '.join(unknown)}. "
            f"Allowed keys are {allowed}."
        )

    url = _read_url(data)

    if "extractor" not in data:
        raise ConfigError(
            "'extractor' is required: it says which part of the page to watch"
        )
    extractor = data["extractor"]
    # Building the extractor is the validation. It raises ConfigError on a bad
    # kind, an unknown key, a broken regex, or an impossible element target.
    extract.build_extractor(extractor)

    condition = data.get("condition", DEFAULT_CONDITION)
    conditions.build_condition(condition)

    notify_settings = _read_notify(data.get("notify", {}), base_dir)

    action_spec = data.get("action")
    action = (
        actions.build_action(action_spec, force_dry_run=force_dry_run)
        if action_spec is not None
        else None
    )

    return Config(
        url=url,
        extractor=dict(extractor),
        interval_seconds=_read_interval(data),
        timeout_seconds=_read_timeout(data),
        user_agent=_read_user_agent(data),
        state_path=_read_path(data, "state_path", DEFAULT_STATE_PATH, base_dir),
        log_path=_read_path(data, "log_path", DEFAULT_LOG_PATH, base_dir),
        source=source,
        condition=dict(condition),
        action=action,
        **notify_settings,
    )


def _read_notify(spec: Any, base_dir: Path) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise ConfigError("'notify' must be an object")

    unknown = sorted(set(spec) - _NOTIFY_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_NOTIFY_KEYS))
        raise ConfigError(
            f"notify: unknown key(s) {', '.join(unknown)}; allowed keys are "
            f"{allowed}"
        )

    raw_channels = spec.get("channels", [{"kind": "console"}])
    if not isinstance(raw_channels, (list, tuple)):
        raise ConfigError("notify: 'channels' must be a list")
    channels = tuple(
        notify.build_channel(channel, base_dir=base_dir) for channel in raw_channels
    )

    subject = spec.get("subject", notify.DEFAULT_SUBJECT)
    body = spec.get("body", notify.DEFAULT_BODY)
    for name, template in (("subject", subject), ("body", body)):
        if not isinstance(template, str) or not template:
            raise ConfigError(f"notify: {name!r} must be a non-empty string")

    for name in ("once_per_value", "warn_on_unknown"):
        if name in spec and not isinstance(spec[name], bool):
            raise ConfigError(f"notify: {name!r} must be true or false")

    return {
        "notify_channels": channels,
        "notify_subject": subject,
        "notify_body": body,
        "notify_once_per_value": bool(spec.get("once_per_value", True)),
        "warn_on_unknown": bool(spec.get("warn_on_unknown", True)),
    }


def _read_url(data: Mapping[str, Any]) -> str:
    url = data.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError("'url' is required and must be a non-empty string")
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise ConfigError(
            f"'url' must start with http:// or https://; got {url!r}"
        )
    if not parts.netloc:
        raise ConfigError(f"'url' has no host: {url!r}")
    return url


def _read_interval(data: Mapping[str, Any]) -> int:
    value = data.get("interval_seconds", DEFAULT_INTERVAL_SECONDS)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError("'interval_seconds' must be a whole number of seconds")
    if value < MIN_INTERVAL_SECONDS:
        raise ConfigError(
            f"'interval_seconds' must be at least {MIN_INTERVAL_SECONDS}"
        )
    return value


def _read_timeout(data: Mapping[str, Any]) -> float:
    value = data.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("'timeout_seconds' must be a number")
    if value <= 0:
        raise ConfigError("'timeout_seconds' must be greater than zero")
    return float(value)


def _read_user_agent(data: Mapping[str, Any]) -> str:
    value = data.get("user_agent", DEFAULT_USER_AGENT)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("'user_agent' must be a non-empty string")
    return value


def _read_path(
    data: Mapping[str, Any], key: str, default: str, base_dir: Path
) -> Path:
    """Resolve a configured path relative to the config file, not the shell.

    A scheduler runs the watcher from whatever directory it likes. Anchoring
    paths to the config file means the same config reads and writes the same
    files no matter where it is launched from.
    """
    value = data.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key!r} must be a non-empty string")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    return candidate
