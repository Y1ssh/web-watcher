"""Web Watcher: fetch a page, read one value from it, and report when it changes."""

from .errors import (
    ConfigError,
    ExtractionError,
    FetchError,
    StateError,
    WatcherError,
)

__all__ = [
    "ConfigError",
    "ExtractionError",
    "FetchError",
    "StateError",
    "WatcherError",
]
