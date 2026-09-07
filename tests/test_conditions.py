"""Conditions decide whether a change matters, so a wrong answer here is either
a missed alert or a false one.

The three-valued logic gets the most attention: UNKNOWN must never be treated
as True (which would fire on a value nobody could read) and never silently as
False (which would hide a problem).
"""

import unittest

from watcher.conditions import (
    Context,
    Decision,
    Verdict,
    build_condition,
    describe,
    parse_number,
)
from watcher.detect import Outcome
from watcher.errors import ConfigError


def context(value="In Stock", previous="Sold Out", outcome=Outcome.CHANGED):
    return Context(outcome=outcome, value=value, previous_value=previous)


def verdict(spec, ctx=None) -> Verdict:
    return build_condition(spec)(ctx or context()).verdict


class ParseNumberTests(unittest.TestCase):
    def test_plain_number(self):
        self.assertEqual(parse_number("1299"), 1299.0)

    def test_decimal(self):
        self.assertEqual(parse_number("49.99"), 49.99)

    def test_strips_currency_symbols(self):
        self.assertEqual(parse_number("$49.99"), 49.99)
        self.assertEqual(parse_number("£10"), 10.0)
        self.assertEqual(parse_number("€ 25.50"), 25.50)

    def test_strips_thousands_commas(self):
        self.assertEqual(parse_number("$1,299.00"), 1299.0)

    def test_reads_a_number_out_of_a_sentence(self):
        self.assertEqual(parse_number("Sale price $49.99 today only"), 49.99)

    def test_negative(self):
        self.assertEqual(parse_number("-5"), -5.0)

    def test_no_number_returns_none(self):
        self.assertIsNone(parse_number("Out of stock"))
        self.assertIsNone(parse_number(""))

    def test_ambiguous_decimal_comma_is_refused_rather_than_guessed(self):
        # "12,5" is twelve-point-five in much of Europe and a typo elsewhere.
        # Guessing wrong here would fire a real alert on a price never reached.
        self.assertIsNone(parse_number("12,5"))

    def test_decimal_comma_mode_reads_european_notation(self):
        self.assertEqual(parse_number("12,5", decimal_comma=True), 12.5)
        self.assertEqual(parse_number("1.299,00", decimal_comma=True), 1299.0)

    def test_thousands_comma_is_not_treated_as_ambiguous(self):
        self.assertEqual(parse_number("1,299"), 1299.0)


class SimpleConditionTests(unittest.TestCase):
    def test_always_is_true(self):
        self.assertIs(verdict({"kind": "always"}), Verdict.TRUE)

    def test_changed_is_true_only_on_a_change(self):
        self.assertIs(verdict({"kind": "changed"}), Verdict.TRUE)
        self.assertIs(
            verdict({"kind": "changed"}, context(outcome=Outcome.UNCHANGED)),
            Verdict.FALSE,
        )

    def test_changed_is_false_on_a_baseline_run(self):
        # The first run has nothing to compare against, so it is not a change.
        self.assertIs(
            verdict({"kind": "changed"}, context(outcome=Outcome.BASELINE)),
            Verdict.FALSE,
        )

    def test_changed_is_false_after_a_retarget(self):
        self.assertIs(
            verdict({"kind": "changed"}, context(outcome=Outcome.RETARGETED)),
            Verdict.FALSE,
        )

    def test_contains(self):
        self.assertIs(verdict({"kind": "contains", "text": "In"}), Verdict.TRUE)
        self.assertIs(verdict({"kind": "contains", "text": "Sold"}), Verdict.FALSE)

    def test_contains_is_case_insensitive_by_default(self):
        self.assertIs(verdict({"kind": "contains", "text": "in stock"}), Verdict.TRUE)

    def test_contains_case_sensitive(self):
        self.assertIs(
            verdict({"kind": "contains", "text": "in stock", "case_sensitive": True}),
            Verdict.FALSE,
        )

    def test_equals(self):
        self.assertIs(verdict({"kind": "equals", "text": "In Stock"}), Verdict.TRUE)
        self.assertIs(verdict({"kind": "equals", "text": "In"}), Verdict.FALSE)

    def test_matches(self):
        self.assertIs(verdict({"kind": "matches", "pattern": r"^In\b"}), Verdict.TRUE)
        self.assertIs(verdict({"kind": "matches", "pattern": r"^\d+$"}), Verdict.FALSE)


class NumberConditionTests(unittest.TestCase):
    def _verdict(self, value, **spec):
        return verdict({"kind": "number", **spec}, context(value=value))

    def test_below_threshold(self):
        self.assertIs(self._verdict("$49.99", below=50), Verdict.TRUE)
        self.assertIs(self._verdict("$50.01", below=50), Verdict.FALSE)

    def test_exactly_the_threshold_is_not_below_it(self):
        self.assertIs(self._verdict("$50.00", below=50), Verdict.FALSE)

    def test_above_threshold(self):
        self.assertIs(self._verdict("120", above=100), Verdict.TRUE)
        self.assertIs(self._verdict("80", above=100), Verdict.FALSE)

    def test_range(self):
        self.assertIs(self._verdict("50", above=10, below=100), Verdict.TRUE)
        self.assertIs(self._verdict("5", above=10, below=100), Verdict.FALSE)
        self.assertIs(self._verdict("500", above=10, below=100), Verdict.FALSE)

    def test_unreadable_value_is_unknown_not_false(self):
        # The course's rule: if you cannot find a clear number, do not trigger,
        # and say so. UNKNOWN is what carries that "say so".
        self.assertIs(self._verdict("Out of stock", below=50), Verdict.UNKNOWN)

    def test_unknown_carries_an_explanation(self):
        decision = build_condition({"kind": "number", "below": 50})(
            context(value="Currently unavailable")
        )
        self.assertIs(decision.verdict, Verdict.UNKNOWN)
        self.assertIn("could not read a clear number", decision.reason)

    def test_a_range_that_can_never_be_satisfied_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "number", "above": 100, "below": 50})

    def test_at_least_one_bound_is_required(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "number"})


class ReasoningTests(unittest.TestCase):
    """Every decision must explain itself, so the logic can be checked by eye."""

    def test_a_true_number_decision_shows_the_comparison(self):
        decision = build_condition({"kind": "number", "below": 50})(
            context(value="$49.99")
        )
        self.assertIn("49.99", decision.reason)
        self.assertIn("below 50", decision.reason)

    def test_a_false_number_decision_shows_the_comparison(self):
        decision = build_condition({"kind": "number", "below": 50})(
            context(value="$99.00")
        )
        self.assertIn("99.0", decision.reason)
        self.assertIn("not below 50", decision.reason)

    def test_a_changed_decision_names_both_values(self):
        decision = build_condition({"kind": "changed"})(context())
        self.assertIn("Sold Out", decision.reason)
        self.assertIn("In Stock", decision.reason)

    def test_every_decision_has_a_non_empty_reason(self):
        specs = [
            {"kind": "always"},
            {"kind": "changed"},
            {"kind": "contains", "text": "In"},
            {"kind": "equals", "text": "In Stock"},
            {"kind": "matches", "pattern": "In"},
            {"kind": "number", "below": 50},
        ]
        for spec in specs:
            with self.subTest(spec=spec):
                self.assertTrue(build_condition(spec)(context()).reason)


class CombinatorTests(unittest.TestCase):
    CHANGED = {"kind": "changed"}
    HAS_IN = {"kind": "contains", "text": "In"}
    HAS_ZZZ = {"kind": "contains", "text": "zzz"}
    UNREADABLE = {"kind": "number", "below": 50}

    def test_all_requires_every_child(self):
        self.assertIs(
            verdict({"kind": "all", "conditions": [self.CHANGED, self.HAS_IN]}),
            Verdict.TRUE,
        )
        self.assertIs(
            verdict({"kind": "all", "conditions": [self.CHANGED, self.HAS_ZZZ]}),
            Verdict.FALSE,
        )

    def test_any_requires_one_child(self):
        self.assertIs(
            verdict({"kind": "any", "conditions": [self.HAS_ZZZ, self.HAS_IN]}),
            Verdict.TRUE,
        )
        self.assertIs(
            verdict({"kind": "any", "conditions": [self.HAS_ZZZ, self.HAS_ZZZ]}),
            Verdict.FALSE,
        )

    def test_not_flips_true_and_false(self):
        self.assertIs(verdict({"kind": "not", "condition": self.HAS_IN}), Verdict.FALSE)
        self.assertIs(verdict({"kind": "not", "condition": self.HAS_ZZZ}), Verdict.TRUE)

    def test_not_leaves_unknown_alone(self):
        # "not unknown" is still unknown, never a licence to fire.
        self.assertIs(
            verdict({"kind": "not", "condition": self.UNREADABLE}), Verdict.UNKNOWN
        )

    def test_all_is_false_when_one_child_is_definitely_false(self):
        # False beats unknown: the whole thing cannot be true either way.
        self.assertIs(
            verdict({"kind": "all", "conditions": [self.HAS_ZZZ, self.UNREADABLE]}),
            Verdict.FALSE,
        )

    def test_all_is_unknown_when_only_unknowns_stand_in_the_way(self):
        self.assertIs(
            verdict({"kind": "all", "conditions": [self.HAS_IN, self.UNREADABLE]}),
            Verdict.UNKNOWN,
        )

    def test_any_is_true_when_one_child_is_definitely_true(self):
        # True beats unknown for `any`, mirroring the `all` case.
        self.assertIs(
            verdict({"kind": "any", "conditions": [self.HAS_IN, self.UNREADABLE]}),
            Verdict.TRUE,
        )

    def test_any_is_unknown_when_nothing_is_true_and_something_is_unreadable(self):
        self.assertIs(
            verdict({"kind": "any", "conditions": [self.HAS_ZZZ, self.UNREADABLE]}),
            Verdict.UNKNOWN,
        )

    def test_combinators_nest(self):
        spec = {
            "kind": "all",
            "conditions": [
                self.CHANGED,
                {"kind": "any", "conditions": [self.HAS_ZZZ, self.HAS_IN]},
            ],
        }
        self.assertIs(verdict(spec), Verdict.TRUE)

    def test_an_empty_condition_list_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "all", "conditions": []})


class ValidationTests(unittest.TestCase):
    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "sometimes"})

    def test_non_mapping_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_condition("changed")

    def test_misspelled_key_is_rejected_rather_than_ignored(self):
        with self.assertRaises(ConfigError) as caught:
            build_condition({"kind": "contains", "txt": "In Stock"})
        self.assertIn("txt", str(caught.exception))

    def test_contains_requires_text(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "contains"})

    def test_matches_rejects_a_broken_pattern(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "matches", "pattern": "([unclosed"})

    def test_number_bounds_must_be_numbers(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "number", "below": "50"})

    def test_a_boolean_is_not_a_number(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "number", "below": True})

    def test_not_requires_an_inner_condition(self):
        with self.assertRaises(ConfigError):
            build_condition({"kind": "not"})

    def test_a_broken_nested_condition_is_caught(self):
        with self.assertRaises(ConfigError):
            build_condition(
                {"kind": "all", "conditions": [{"kind": "contains"}]}
            )


class DescribeTests(unittest.TestCase):
    def test_describes_each_kind_readably(self):
        self.assertEqual(describe({"kind": "changed"}), "the value changed")
        self.assertIn("below 50", describe({"kind": "number", "below": 50}))
        self.assertIn("In Stock", describe({"kind": "contains", "text": "In Stock"}))

    def test_describes_a_combinator(self):
        text = describe({
            "kind": "all",
            "conditions": [{"kind": "changed"}, {"kind": "number", "below": 50}],
        })
        self.assertIn(" and ", text)


class DecisionTests(unittest.TestCase):
    def test_is_true_and_is_unknown_helpers(self):
        self.assertTrue(Decision(Verdict.TRUE, "x").is_true)
        self.assertFalse(Decision(Verdict.UNKNOWN, "x").is_true)
        self.assertTrue(Decision(Verdict.UNKNOWN, "x").is_unknown)
        self.assertFalse(Decision(Verdict.FALSE, "x").is_unknown)


if __name__ == "__main__":
    unittest.main()
