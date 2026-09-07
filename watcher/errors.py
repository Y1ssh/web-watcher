"""Exception types raised deliberately by the watcher.

Every failure the watcher can anticipate gets its own class so callers can tell
a broken config apart from an unreachable site apart from a page that loaded
but no longer contains the thing we watch. Anything else escaping as a plain
Exception is a bug, not an expected condition.
"""


class WatcherError(Exception):
    """Base class for every error this package raises on purpose."""


class ConfigError(WatcherError):
    """The configuration is missing, malformed, or internally inconsistent."""


class FetchError(WatcherError):
    """The target page could not be retrieved."""


class ExtractionError(WatcherError):
    """The page loaded, but the watched value could not be read out of it."""


class StateError(WatcherError):
    """The stored snapshot is unreadable, corrupt, or from another version."""
