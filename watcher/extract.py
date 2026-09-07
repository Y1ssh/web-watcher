"""Reduce a page of HTML to the single string the watcher compares between runs.

The watcher never diffs raw HTML. Raw markup churns constantly -- session ids,
analytics tags, reordered attributes -- and diffing it would report a change on
almost every run. Instead every extractor returns one normalised string, and
change detection is a comparison of that string against the one stored last time.
"""

import re
from collections.abc import Callable, Mapping
from html.parser import HTMLParser
from typing import Any

from .errors import ConfigError, ExtractionError

#: Tags whose contents a reader never sees, so they must not reach the value.
SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})

#: Tags that force a visual break. Text either side of one must not fuse.
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre",
    "section", "table", "tbody", "td", "tfoot", "th", "thead", "title", "tr",
    "ul",
})

#: Tags with no closing tag, so they can never wrap text.
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
})

#: How much of a captured value the log is allowed to echo.
LOG_VALUE_LIMIT = 500

EXTRACTOR_KINDS = ("full_text", "contains", "regex", "element")

_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "full_text": frozenset({"kind"}),
    "contains": frozenset({"kind", "needle", "case_sensitive"}),
    "regex": frozenset({"kind", "pattern", "group", "ignore_case"}),
    "element": frozenset({"kind", "tag", "id", "class", "index"}),
}

Extractor = Callable[[str], str]


def normalize(text: str) -> str:
    """Collapse every run of whitespace to a single space and trim the ends.

    ``str.split`` treats the non-breaking space (U+00A0) as whitespace, so the
    ``&nbsp;`` that litters real pages collapses here too and does not show up
    later as a phantom change.
    """
    return " ".join(text.split())


def truncate(value: str, limit: int = LOG_VALUE_LIMIT) -> str:
    """Shorten a value for display, saying how much was left out."""
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... (+{len(value) - limit} more characters)"


class _TextCollector(HTMLParser):
    """Gather the visible text of a document as a list of chunks.

    Chunks are joined with no separator so ``in<b>line</b>`` stays ``inline``
    and a price split across tags stays one number. Block-level tags push an
    explicit space, so ``<p>a</p><p>b</p>`` becomes ``a b`` and not ``ab``.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skipping: list[str] = []

    # -- collection points, overridden by the element collector -----------

    def emit(self, chunk: str) -> None:
        self._chunks.append(chunk)

    def enter(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Called for each visible start tag."""

    def leave(self, tag: str) -> None:
        """Called for each visible end tag."""

    # -- HTMLParser hooks -------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skipping.append(tag)
            return
        if self._skipping:
            return
        self.enter(tag, attrs)
        if tag in BLOCK_TAGS:
            self.emit(" ")

    def handle_endtag(self, tag):
        if self._skipping:
            # Only the tag that opened the skip can close it; anything else
            # inside a <script> body is text, not markup.
            if self._skipping[-1] == tag:
                self._skipping.pop()
            return
        if tag in BLOCK_TAGS:
            self.emit(" ")
        self.leave(tag)

    def handle_data(self, data):
        if not self._skipping:
            self.emit(data)

    @property
    def text(self) -> str:
        return normalize("".join(self._chunks))


class _ElementCollector(_TextCollector):
    """Capture the text of the nth element matching a tag, id, and class."""

    def __init__(
        self,
        tag: str,
        element_id: str | None,
        class_name: str | None,
        index: int,
    ) -> None:
        super().__init__()
        self._target = tag
        self._id = element_id
        self._class = class_name
        self._index = index
        self._seen = -1
        self._depth = 0
        self._capturing = False
        self._closed = False

    def emit(self, chunk):
        if self._capturing:
            self._chunks.append(chunk)

    def enter(self, tag, attrs):
        if tag != self._target:
            return
        if self._capturing:
            # A same-named descendant. Track it so its end tag does not look
            # like the end of the element we are capturing.
            self._depth += 1
            return
        if self._closed or not self._matches(attrs):
            return
        self._seen += 1
        if self._seen == self._index:
            self._capturing = True
            self._depth = 1

    def leave(self, tag):
        if not self._capturing or tag != self._target:
            return
        self._depth -= 1
        if self._depth == 0:
            self._capturing = False
            self._closed = True

    def _matches(self, attrs: list[tuple[str, str | None]]) -> bool:
        values: dict[str, str | None] = {}
        for name, value in attrs:
            # HTML says the first of a duplicated attribute wins.
            values.setdefault(name, value)
        if self._id is not None and values.get("id") != self._id:
            return False
        if self._class is not None:
            classes = (values.get("class") or "").split()
            if self._class not in classes:
                return False
        return True

    @property
    def found(self) -> bool:
        # A page that never closes the element still gave us its text.
        return self._closed or self._capturing


def collect_text(html: str) -> str:
    """Return the normalised visible text of a whole document."""
    parser = _TextCollector()
    parser.feed(html)
    parser.close()
    return parser.text


# -- specification validation ---------------------------------------------


def _require_str(spec: Mapping[str, Any], key: str, kind: str) -> str:
    value = spec.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"extractor {kind!r}: {key!r} must be a non-empty string"
        )
    return value


def _optional_str(spec: Mapping[str, Any], key: str, kind: str) -> str | None:
    if key not in spec:
        return None
    return _require_str(spec, key, kind)


def _optional_bool(spec: Mapping[str, Any], key: str, kind: str) -> bool:
    value = spec.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"extractor {kind!r}: {key!r} must be true or false")
    return value


def _optional_index(spec: Mapping[str, Any], kind: str) -> int:
    value = spec.get("index", 0)
    # bool is a subclass of int, so reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"extractor {kind!r}: 'index' must be 0 or greater")
    return value


def _build_full_text(spec: Mapping[str, Any]) -> Extractor:
    del spec  # no options
    return collect_text


def _build_contains(spec: Mapping[str, Any]) -> Extractor:
    needle = _require_str(spec, "needle", "contains")
    case_sensitive = _optional_bool(spec, "case_sensitive", "contains")

    def extract(html: str) -> str:
        text = collect_text(html)
        haystack = text if case_sensitive else text.lower()
        target = needle if case_sensitive else needle.lower()
        return "present" if target in haystack else "absent"

    return extract


def _build_regex(spec: Mapping[str, Any]) -> Extractor:
    pattern = _require_str(spec, "pattern", "regex")
    ignore_case = _optional_bool(spec, "ignore_case", "regex")
    group = spec.get("group", 0)
    if isinstance(group, bool) or not isinstance(group, (int, str)):
        raise ConfigError(
            "extractor 'regex': 'group' must be a group number or a group name"
        )
    try:
        compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ConfigError(f"extractor 'regex': invalid pattern -- {exc}") from exc

    # Catch a bad group now rather than on the first live run.
    if isinstance(group, int) and group > compiled.groups:
        raise ConfigError(
            f"extractor 'regex': pattern has {compiled.groups} capture group(s), "
            f"so group {group} does not exist"
        )
    if isinstance(group, str) and group not in compiled.groupindex:
        known = ", ".join(sorted(compiled.groupindex)) or "none"
        raise ConfigError(
            f"extractor 'regex': pattern has no group named {group!r} "
            f"(named groups: {known})"
        )

    def extract(html: str) -> str:
        match = compiled.search(collect_text(html))
        if match is None:
            raise ExtractionError(
                f"pattern {pattern!r} matched nothing in the page text"
            )
        captured = match.group(group)
        if captured is None:
            raise ExtractionError(
                f"pattern {pattern!r} matched, but group {group!r} captured nothing"
            )
        return normalize(captured)

    return extract


def _build_element(spec: Mapping[str, Any]) -> Extractor:
    tag = _require_str(spec, "tag", "element").strip().lower()
    if tag in VOID_TAGS:
        raise ConfigError(
            f"extractor 'element': <{tag}> never wraps text, so it has nothing "
            f"to extract"
        )
    element_id = _optional_str(spec, "id", "element")
    class_name = _optional_str(spec, "class", "element")
    index = _optional_index(spec, "element")

    def extract(html: str) -> str:
        parser = _ElementCollector(tag, element_id, class_name, index)
        parser.feed(html)
        parser.close()
        if not parser.found:
            raise ExtractionError(f"no element matched {describe(spec)}")
        return parser.text

    return extract


_BUILDERS: dict[str, Callable[[Mapping[str, Any]], Extractor]] = {
    "full_text": _build_full_text,
    "contains": _build_contains,
    "regex": _build_regex,
    "element": _build_element,
}


def build_extractor(spec: Any) -> Extractor:
    """Validate an extractor specification and return the function it describes.

    Every problem that can be spotted without a live page -- a misspelled key,
    an unknown kind, a broken regex -- is raised here as a ConfigError, so a bad
    config fails on startup instead of halfway through an unattended run.
    """
    if not isinstance(spec, Mapping):
        raise ConfigError("'extractor' must be an object")
    kind = spec.get("kind")
    if kind not in _BUILDERS:
        raise ConfigError(
            f"extractor 'kind' must be one of {', '.join(EXTRACTOR_KINDS)}; "
            f"got {kind!r}"
        )
    unknown = sorted(set(spec) - _ALLOWED_KEYS[kind])
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS[kind]))
        raise ConfigError(
            f"extractor {kind!r}: unknown key(s) {', '.join(unknown)}; "
            f"allowed keys are {allowed}"
        )
    return _BUILDERS[kind](spec)


def describe(spec: Mapping[str, Any]) -> str:
    """Render an extractor spec as a short phrase for logs and error messages."""
    kind = spec.get("kind")
    if kind == "full_text":
        return "the whole page text"
    if kind == "contains":
        return f"whether {spec.get('needle')!r} appears in the page text"
    if kind == "regex":
        return f"pattern {spec.get('pattern')!r} in the page text"
    if kind == "element":
        parts = [f"<{spec.get('tag')}>"]
        if spec.get("id") is not None:
            parts.append(f"id={spec['id']!r}")
        if spec.get("class") is not None:
            parts.append(f"class={spec['class']!r}")
        index = spec.get("index", 0)
        if index:
            parts.append(f"index={index}")
        return " ".join(parts)
    return repr(dict(spec))
