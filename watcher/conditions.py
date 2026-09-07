"""Decide whether a change is one you actually care about.

Slice 1 reacted to any change at all. A condition is the rule that makes the
watcher pickier: "only tell me if the price is below 50", "only if it now says
In Stock".

Two things matter here beyond the comparison itself:

* Every decision carries a plain-English reason, so you can read back exactly
  how the watcher concluded what it did instead of trusting that it looked
  right.
* A condition that cannot be evaluated returns UNKNOWN, never False and never
  True. Real pages are messy; when the number cannot be read the watcher says
  so and stays quiet rather than guessing.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .detect import Outcome
from .errors import ConfigError

CONDITION_KINDS = (
    "always",
    "changed",
    "contains",
    "equals",
    "matches",
    "number",
    "all",
    "any",
    "not",
)

_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "always": frozenset({"kind"}),
    "changed": frozenset({"kind"}),
    "contains": frozenset({"kind", "text", "case_sensitive"}),
    "equals": frozenset({"kind", "text", "case_sensitive"}),
    "matches": frozenset({"kind", "pattern", "ignore_case"}),
    "number": frozenset({"kind", "below", "above", "decimal_comma"}),
    "all": frozenset({"kind", "conditions"}),
    "any": frozenset({"kind", "conditions"}),
    "not": frozenset({"kind", "condition"}),
}

#: Symbols stripped before looking for a number.
_CURRENCY_SYMBOLS = "$€£¥₹₩¢"

#: 1,299.00 or 1299 or .5 or -3
_ANGLO_NUMBER = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d*\.?\d+")
#: 1.299,00 or 1299 -- dots group thousands, comma is the decimal point.
_EURO_NUMBER = re.compile(r"-?\d{1,3}(?:\.\d{3})+(?:,\d+)?|-?\d+(?:,\d+)?")
#: A comma followed by one or two digits: ambiguous in Anglo notation.
_AMBIGUOUS_COMMA = re.compile(r",\d{1,2}(?!\d)")


class Verdict(str, Enum):
    """Three-valued, because "I could not tell" is not the same as "no"."""

    TRUE = "true"
    FALSE = "false"
    #: The condition could not be evaluated. Never triggers anything.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Context:
    """Everything a condition is allowed to look at."""

    outcome: Outcome
    value: str
    previous_value: str | None


@dataclass(frozen=True)
class Decision:
    """A verdict plus the plain-English reasoning behind it."""

    verdict: Verdict
    reason: str

    @property
    def is_true(self) -> bool:
        return self.verdict is Verdict.TRUE

    @property
    def is_unknown(self) -> bool:
        return self.verdict is Verdict.UNKNOWN


Condition = Callable[[Context], Decision]


def parse_number(text: str, *, decimal_comma: bool = False) -> float | None:
    """Read the first number out of a messy string, or return None.

    Currency symbols and thousands separators are ignored, so "$1,299.00" reads
    as 1299.0. Returning None rather than a best guess is deliberate: a wrong
    number here would fire a real alert, or a real action, on a value that was
    never actually met.
    """
    cleaned = text
    for symbol in _CURRENCY_SYMBOLS:
        cleaned = cleaned.replace(symbol, " ")

    if decimal_comma:
        match = _EURO_NUMBER.search(cleaned)
        if match is None:
            return None
        token = match.group(0).replace(".", "").replace(",", ".")
    else:
        # "12,5" could be twelve-point-five or a typo. Refuse rather than guess.
        if _AMBIGUOUS_COMMA.search(cleaned):
            return None
        match = _ANGLO_NUMBER.search(cleaned)
        if match is None:
            return None
        token = match.group(0).replace(",", "")

    try:
        return float(token)
    except ValueError:
        return None


# -- individual conditions -------------------------------------------------


def _build_always(spec: Mapping[str, Any]) -> Condition:
    del spec

    def condition(context: Context) -> Decision:
        del context
        return Decision(Verdict.TRUE, "no condition was set, so every check counts")

    return condition


def _build_changed(spec: Mapping[str, Any]) -> Condition:
    del spec

    def condition(context: Context) -> Decision:
        if context.outcome is Outcome.CHANGED:
            return Decision(
                Verdict.TRUE,
                f"the value changed from {context.previous_value!r} to "
                f"{context.value!r}",
            )
        return Decision(
            Verdict.FALSE,
            f"the value did not change (this run was {context.outcome.value})",
        )

    return condition


def _build_contains(spec: Mapping[str, Any]) -> Condition:
    text = _require_text(spec, "contains")
    case_sensitive = _optional_bool(spec, "case_sensitive", "contains")

    def condition(context: Context) -> Decision:
        haystack = context.value if case_sensitive else context.value.lower()
        needle = text if case_sensitive else text.lower()
        if needle in haystack:
            return Decision(
                Verdict.TRUE, f"{text!r} appears in {context.value!r}"
            )
        return Decision(
            Verdict.FALSE, f"{text!r} does not appear in {context.value!r}"
        )

    return condition


def _build_equals(spec: Mapping[str, Any]) -> Condition:
    text = _require_text(spec, "equals")
    case_sensitive = _optional_bool(spec, "case_sensitive", "equals")

    def condition(context: Context) -> Decision:
        left = context.value if case_sensitive else context.value.lower()
        right = text if case_sensitive else text.lower()
        if left == right:
            return Decision(Verdict.TRUE, f"the value is exactly {text!r}")
        return Decision(
            Verdict.FALSE, f"the value is {context.value!r}, not {text!r}"
        )

    return condition


def _build_matches(spec: Mapping[str, Any]) -> Condition:
    pattern = spec.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError("condition 'matches': 'pattern' must be a non-empty string")
    ignore_case = _optional_bool(spec, "ignore_case", "matches")
    try:
        compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ConfigError(f"condition 'matches': invalid pattern -- {exc}") from exc

    def condition(context: Context) -> Decision:
        if compiled.search(context.value) is not None:
            return Decision(
                Verdict.TRUE, f"{context.value!r} matches pattern {pattern!r}"
            )
        return Decision(
            Verdict.FALSE, f"{context.value!r} does not match pattern {pattern!r}"
        )

    return condition


def _build_number(spec: Mapping[str, Any]) -> Condition:
    below = _optional_number(spec, "below")
    above = _optional_number(spec, "above")
    if below is None and above is None:
        raise ConfigError(
            "condition 'number': set 'below', 'above', or both"
        )
    if below is not None and above is not None and above >= below:
        raise ConfigError(
            f"condition 'number': 'above' ({above}) must be less than 'below' "
            f"({below}), otherwise no value can ever satisfy it"
        )
    decimal_comma = _optional_bool(spec, "decimal_comma", "number")

    def condition(context: Context) -> Decision:
        number = parse_number(context.value, decimal_comma=decimal_comma)
        if number is None:
            return Decision(
                Verdict.UNKNOWN,
                f"could not read a clear number from {context.value!r}, so the "
                f"condition was not triggered",
            )
        if below is not None and number >= below:
            return Decision(
                Verdict.FALSE, f"{number} is not below {below}"
            )
        if above is not None and number <= above:
            return Decision(
                Verdict.FALSE, f"{number} is not above {above}"
            )
        return Decision(Verdict.TRUE, f"{number} satisfies {_describe_range(below, above)}")

    return condition


def _describe_range(below: float | None, above: float | None) -> str:
    if below is not None and above is not None:
        return f"above {above} and below {below}"
    if below is not None:
        return f"below {below}"
    return f"above {above}"


# -- combinators -----------------------------------------------------------


def _build_all(spec: Mapping[str, Any]) -> Condition:
    children = _build_children(spec, "all")

    def condition(context: Context) -> Decision:
        decisions = [child(context) for child in children]
        for decision in decisions:
            if decision.verdict is Verdict.FALSE:
                return Decision(Verdict.FALSE, decision.reason)
        for decision in decisions:
            if decision.verdict is Verdict.UNKNOWN:
                return Decision(Verdict.UNKNOWN, decision.reason)
        return Decision(
            Verdict.TRUE, " and ".join(d.reason for d in decisions)
        )

    return condition


def _build_any(spec: Mapping[str, Any]) -> Condition:
    children = _build_children(spec, "any")

    def condition(context: Context) -> Decision:
        decisions = [child(context) for child in children]
        for decision in decisions:
            if decision.verdict is Verdict.TRUE:
                return Decision(Verdict.TRUE, decision.reason)
        for decision in decisions:
            if decision.verdict is Verdict.UNKNOWN:
                return Decision(Verdict.UNKNOWN, decision.reason)
        return Decision(
            Verdict.FALSE, " and ".join(d.reason for d in decisions)
        )

    return condition


def _build_not(spec: Mapping[str, Any]) -> Condition:
    inner_spec = spec.get("condition")
    if inner_spec is None:
        raise ConfigError("condition 'not': 'condition' is required")
    inner = build_condition(inner_spec)

    def condition(context: Context) -> Decision:
        decision = inner(context)
        if decision.verdict is Verdict.UNKNOWN:
            return decision
        flipped = (
            Verdict.FALSE if decision.verdict is Verdict.TRUE else Verdict.TRUE
        )
        return Decision(flipped, f"not ({decision.reason})")

    return condition


def _build_children(spec: Mapping[str, Any], kind: str) -> list[Condition]:
    raw = spec.get("conditions")
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ConfigError(
            f"condition {kind!r}: 'conditions' must be a non-empty list"
        )
    return [build_condition(child) for child in raw]


# -- validation ------------------------------------------------------------


def _require_text(spec: Mapping[str, Any], kind: str) -> str:
    text = spec.get("text")
    if not isinstance(text, str) or not text:
        raise ConfigError(f"condition {kind!r}: 'text' must be a non-empty string")
    return text


def _optional_bool(spec: Mapping[str, Any], key: str, kind: str) -> bool:
    value = spec.get(key, False)
    if not isinstance(value, bool):
        raise ConfigError(f"condition {kind!r}: {key!r} must be true or false")
    return value


def _optional_number(spec: Mapping[str, Any], key: str) -> float | None:
    if key not in spec:
        return None
    value = spec[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"condition 'number': {key!r} must be a number")
    return float(value)


_BUILDERS: dict[str, Callable[[Mapping[str, Any]], Condition]] = {
    "always": _build_always,
    "changed": _build_changed,
    "contains": _build_contains,
    "equals": _build_equals,
    "matches": _build_matches,
    "number": _build_number,
    "all": _build_all,
    "any": _build_any,
    "not": _build_not,
}


def build_condition(spec: Any) -> Condition:
    """Validate a condition specification and return the rule it describes."""
    if not isinstance(spec, Mapping):
        raise ConfigError("a condition must be an object")
    kind = spec.get("kind")
    if kind not in _BUILDERS:
        raise ConfigError(
            f"condition 'kind' must be one of {', '.join(CONDITION_KINDS)}; "
            f"got {kind!r}"
        )
    unknown = sorted(set(spec) - _ALLOWED_KEYS[kind])
    if unknown:
        allowed = ", ".join(sorted(_ALLOWED_KEYS[kind]))
        raise ConfigError(
            f"condition {kind!r}: unknown key(s) {', '.join(unknown)}; "
            f"allowed keys are {allowed}"
        )
    return _BUILDERS[kind](spec)


def describe(spec: Mapping[str, Any]) -> str:
    """Render a condition spec as a short phrase for logs and banners."""
    kind = spec.get("kind")
    if kind == "always":
        return "every check"
    if kind == "changed":
        return "the value changed"
    if kind == "contains":
        return f"the value contains {spec.get('text')!r}"
    if kind == "equals":
        return f"the value is {spec.get('text')!r}"
    if kind == "matches":
        return f"the value matches {spec.get('pattern')!r}"
    if kind == "number":
        return f"the number is {_describe_range(spec.get('below'), spec.get('above'))}"
    if kind in ("all", "any"):
        joiner = " and " if kind == "all" else " or "
        parts = [describe(child) for child in spec.get("conditions", [])]
        return "(" + joiner.join(parts) + ")"
    if kind == "not":
        return f"not ({describe(spec.get('condition', {}))})"
    return repr(dict(spec))
