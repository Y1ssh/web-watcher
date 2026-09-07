"""Getting the message to you: console, file, webhook, email.

Two rules shape this module:

* A channel that fails must not end the watch. One unreachable SMTP server
  should cost you a notification, not the monitor.
* Nothing here reads a credential out of the config. Webhook URLs and SMTP
  passwords are named as environment variables and fetched at send time.
"""

import json
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Any, TextIO

from . import secrets
from .errors import ConfigError, WatcherError
from .fetching import ALLOWED_SCHEMES, DEFAULT_USER_AGENT

CHANNEL_KINDS = ("console", "file", "webhook", "email")

DEFAULT_SUBJECT = "Web Watcher: {condition_reason}"
DEFAULT_BODY = (
    "{url}\n\n"
    "now:      {value}\n"
    "before:   {previous_value}\n"
    "outcome:  {outcome}\n"
    "because:  {condition_reason}\n"
    "checked:  {at}"
)

DEFAULT_WEBHOOK_TIMEOUT = 15.0
DEFAULT_SMTP_TIMEOUT = 30.0

_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "console": frozenset({"kind"}),
    "file": frozenset({"kind", "path"}),
    "webhook": frozenset({"kind", "url_env", "text_key", "timeout_seconds"}),
    "email": frozenset({
        "kind", "to", "from", "host", "port", "use_tls",
        "username_env", "password_env", "timeout_seconds",
    }),
}

#: Config keys that would mean a secret was written into the file itself.
_FORBIDDEN_SECRET_KEYS = ("url", "password", "token", "secret", "api_key")


class NotifyError(WatcherError):
    """A channel could not deliver its message."""


@dataclass(frozen=True)
class Notification:
    """One message, already rendered."""

    subject: str
    body: str
    is_test: bool = False

    @property
    def display_subject(self) -> str:
        return f"[TEST] {self.subject}" if self.is_test else self.subject


@dataclass(frozen=True)
class Delivery:
    """What happened when one channel tried to send."""

    channel: str
    sent: bool
    detail: str = ""

    @property
    def summary(self) -> str:
        state = "sent" if self.sent else "FAILED"
        return f"{self.channel}: {state}{f' ({self.detail})' if self.detail else ''}"


class Channel:
    """A place a notification can go."""

    kind = "channel"

    def send(self, note: Notification) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        return self.kind


class ConsoleChannel(Channel):
    """Print to the terminal. Always available, needs no credentials."""

    kind = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream

    def send(self, note: Notification) -> None:
        stream = self._stream if self._stream is not None else sys.stdout
        print(f"\n--- {note.display_subject} ---", file=stream)
        print(note.body, file=stream)
        print("---", file=stream)


class FileChannel(Channel):
    """Append the message to a file, so alerts survive a closed terminal."""

    kind = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    def send(self, note: Notification) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(f"=== {note.display_subject}\n{note.body}\n\n")
        except OSError as exc:
            raise NotifyError(f"could not write to {self.path}: {exc}") from exc

    def describe(self) -> str:
        return f"file({self.path})"


class WebhookChannel(Channel):
    """POST JSON to an incoming webhook -- Slack, Discord, Teams, or your own.

    The URL is a credential (anyone holding a Slack webhook URL can post to the
    channel), so it is read from the environment and never stored in config.
    """

    kind = "webhook"

    def __init__(
        self,
        url_env: str,
        *,
        text_key: str = "text",
        timeout: float = DEFAULT_WEBHOOK_TIMEOUT,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        self.url_env = url_env
        self.text_key = text_key
        self.timeout = timeout
        self._opener = opener

    def send(self, note: Notification) -> None:
        url = secrets.require(self.url_env, used_for="webhook URL")
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise NotifyError(
                f"{self.url_env} must hold an http or https URL"
            )
        payload = json.dumps(
            {self.text_key: f"{note.display_subject}\n\n{note.body}"}
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
        )
        opener = self._opener or urllib.request.build_opener()
        try:
            with opener.open(request, timeout=self.timeout) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise NotifyError(
                f"webhook returned HTTP {exc.code} {exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise NotifyError(f"could not reach the webhook: {exc.reason}") from exc
        except OSError as exc:
            raise NotifyError(f"webhook request failed: {exc}") from exc

    def describe(self) -> str:
        return f"webhook(${self.url_env})"


class EmailChannel(Channel):
    """Send mail over SMTP. The password is only ever an environment variable."""

    kind = "email"

    def __init__(
        self,
        *,
        to: str,
        sender: str,
        host: str,
        port: int,
        use_tls: bool = True,
        username_env: str | None = None,
        password_env: str | None = None,
        timeout: float = DEFAULT_SMTP_TIMEOUT,
        smtp_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.to = to
        self.sender = sender
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.username_env = username_env
        self.password_env = password_env
        self.timeout = timeout
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def send(self, note: Notification) -> None:
        message = EmailMessage()
        message["Subject"] = note.display_subject
        message["From"] = self.sender
        message["To"] = self.to
        message.set_content(note.body)

        username = (
            secrets.require(self.username_env, used_for="SMTP username")
            if self.username_env
            else None
        )
        password = (
            secrets.require(self.password_env, used_for="SMTP password")
            if self.password_env
            else None
        )

        try:
            with self._smtp_factory(self.host, self.port, timeout=self.timeout) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if username is not None and password is not None:
                    smtp.login(username, password)
                smtp.send_message(message)
        except smtplib.SMTPException as exc:
            raise NotifyError(f"SMTP error: {exc}") from exc
        except OSError as exc:
            raise NotifyError(
                f"could not reach {self.host}:{self.port}: {exc}"
            ) from exc

    def describe(self) -> str:
        return f"email({self.to})"


# -- rendering -------------------------------------------------------------


class _SafeFormat(dict):
    """Leave an unknown placeholder visible instead of raising mid-alert."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render(template: str, context: Mapping[str, Any]) -> str:
    """Fill a message template, tolerating placeholders that do not exist."""
    return template.format_map(_SafeFormat(context))


def build_notification(
    context: Mapping[str, Any],
    *,
    subject_template: str = DEFAULT_SUBJECT,
    body_template: str = DEFAULT_BODY,
    is_test: bool = False,
) -> Notification:
    return Notification(
        subject=render(subject_template, context),
        body=render(body_template, context),
        is_test=is_test,
    )


def deliver(channels: list[Channel], note: Notification) -> list[Delivery]:
    """Send to every channel, collecting failures instead of raising.

    One dead channel must not stop the others, and must not end the watch.
    """
    results: list[Delivery] = []
    for channel in channels:
        try:
            channel.send(note)
            results.append(Delivery(channel.describe(), sent=True))
        except NotifyError as exc:
            results.append(Delivery(channel.describe(), sent=False, detail=str(exc)))
        except Exception as exc:  # noqa: BLE001 - never let a channel kill the run
            results.append(
                Delivery(
                    channel.describe(),
                    sent=False,
                    detail=f"unexpected {type(exc).__name__}: {exc}",
                )
            )
    return results


# -- construction from config ---------------------------------------------


def build_channel(spec: Any, *, base_dir: Path) -> Channel:
    """Validate one channel specification and build it."""
    if not isinstance(spec, Mapping):
        raise ConfigError("each notify channel must be an object")
    kind = spec.get("kind")
    if kind not in _ALLOWED_KEYS:
        raise ConfigError(
            f"notify channel 'kind' must be one of {', '.join(CHANNEL_KINDS)}; "
            f"got {kind!r}"
        )

    _reject_inline_secrets(spec, kind)

    unknown = sorted(set(spec) - _ALLOWED_KEYS[kind])
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS[kind]))
        raise ConfigError(
            f"notify channel {kind!r}: unknown key(s) {', '.join(unknown)}; "
            f"allowed keys are {allowed}"
        )

    if kind == "console":
        return ConsoleChannel()
    if kind == "file":
        return FileChannel(_resolve(spec, "path", "logs/notifications.log", base_dir))
    if kind == "webhook":
        return _build_webhook(spec)
    return _build_email(spec)


def _reject_inline_secrets(spec: Mapping[str, Any], kind: str) -> None:
    """Refuse a config that carries a credential in the file itself."""
    for key in _FORBIDDEN_SECRET_KEYS:
        if key in spec and key not in _ALLOWED_KEYS[kind]:
            raise ConfigError(
                f"notify channel {kind!r}: {key!r} must not be written into the "
                f"config file. Name an environment variable instead "
                f"(for example {key}_env) and keep the value in .env."
            )


def _build_webhook(spec: Mapping[str, Any]) -> WebhookChannel:
    url_env = spec.get("url_env")
    if not isinstance(url_env, str) or not url_env.strip():
        raise ConfigError(
            "notify channel 'webhook': 'url_env' must name the environment "
            "variable holding the webhook URL"
        )
    text_key = spec.get("text_key", "text")
    if not isinstance(text_key, str) or not text_key:
        raise ConfigError("notify channel 'webhook': 'text_key' must be a string")
    timeout = _optional_timeout(spec, "webhook", DEFAULT_WEBHOOK_TIMEOUT)
    return WebhookChannel(url_env.strip(), text_key=text_key, timeout=timeout)


def _build_email(spec: Mapping[str, Any]) -> EmailChannel:
    to = _require_str(spec, "to", "email")
    sender = _require_str(spec, "from", "email")
    host = _require_str(spec, "host", "email")

    port = spec.get("port", 587)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("notify channel 'email': 'port' must be a port number")

    use_tls = spec.get("use_tls", True)
    if not isinstance(use_tls, bool):
        raise ConfigError("notify channel 'email': 'use_tls' must be true or false")

    username_env = _optional_str(spec, "username_env", "email")
    password_env = _optional_str(spec, "password_env", "email")
    if (username_env is None) != (password_env is None):
        raise ConfigError(
            "notify channel 'email': set both 'username_env' and "
            "'password_env', or neither"
        )

    return EmailChannel(
        to=to,
        sender=sender,
        host=host,
        port=port,
        use_tls=use_tls,
        username_env=username_env,
        password_env=password_env,
        timeout=_optional_timeout(spec, "email", DEFAULT_SMTP_TIMEOUT),
    )


def _require_str(spec: Mapping[str, Any], key: str, kind: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"notify channel {kind!r}: {key!r} must be a non-empty string"
        )
    return value.strip()


def _optional_str(spec: Mapping[str, Any], key: str, kind: str) -> str | None:
    if key not in spec:
        return None
    return _require_str(spec, key, kind)


def _optional_timeout(spec: Mapping[str, Any], kind: str, default: float) -> float:
    value = spec.get("timeout_seconds", default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(
            f"notify channel {kind!r}: 'timeout_seconds' must be a positive number"
        )
    return float(value)


def _resolve(
    spec: Mapping[str, Any], key: str, default: str, base_dir: Path
) -> Path:
    value = spec.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"notify channel: {key!r} must be a non-empty string")
    path = Path(value)
    return path if path.is_absolute() else base_dir / path
