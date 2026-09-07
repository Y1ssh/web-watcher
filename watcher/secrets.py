"""Credentials come from the environment, never from the config file.

config.json is a file people copy, paste into chat, and commit by accident. A
webhook URL or an SMTP password written into it is a leaked credential. So the
config only ever names an environment variable, and the value is read from the
environment at run time.
"""

import os
from pathlib import Path

from .errors import ConfigError

DEFAULT_ENV_FILENAME = ".env"


def get(name: str) -> str | None:
    """Return an environment variable's value, treating blank as missing."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def require(name: str, *, used_for: str) -> str:
    """Return an environment variable, or explain precisely what is missing."""
    value = get(name)
    if value is None:
        raise ConfigError(
            f"environment variable {name} is not set, and it holds the "
            f"{used_for}. Set it in your shell, or put it in a .env file next "
            f"to your config (.env is git-ignored)."
        )
    return value


def load_env_file(path: Path) -> list[str]:
    """Read a .env file into the environment. Returns the names it set.

    A value already present in the real environment always wins, so a
    deployment's platform settings are never overwritten by a stray local file.
    Missing files are fine -- .env is optional.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"could not read {path}: {exc}") from exc

    applied: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        if not separator:
            raise ConfigError(
                f"{path} line {number}: expected NAME=value, got {raw.strip()!r}"
            )
        name = name.strip()
        if not name:
            raise ConfigError(f"{path} line {number}: missing a variable name")
        value = _unquote(value.strip())
        if name in os.environ:
            continue
        os.environ[name] = value
        applied.append(name)
    return applied


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def redact(value: str) -> str:
    """Render a secret safely for logs: never print one in full."""
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}...{value[-2:]} ({len(value)} chars)"
