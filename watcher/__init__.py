"""Web Watcher: fetch a page, read one value, report and act when it changes."""

from .actions import ActionError
from .errors import (
    ConfigError,
    ExtractionError,
    FetchError,
    StateError,
    WatcherError,
)
from .notify import NotifyError

__all__ = [
    "ActionError",
    "ConfigError",
    "ExtractionError",
    "FetchError",
    "NotifyError",
    "StateError",
    "WatcherError",
]
