"""Extraction is where a watcher most often goes quietly wrong.

If it grabs the wrong slice of the page, every later stage still "works": the
snapshot saves, the comparison runs, the app reports "no change" forever. These
tests pin the exact string each extractor produces.
"""

import unittest

from watcher.errors import ConfigError, ExtractionError
from watcher.extract import (
    build_extractor,
    collect_text,
    describe,
    normalize,
    truncate,
)


class NormalizeTests(unittest.TestCase):
    def test_collapses_runs_of_whitespace(self):
        self.assertEqual(normalize("  a \n\t b  "), "a b")

    def test_collapses_non_breaking_space(self):
        # &nbsp; is everywhere in real pages; if it survived here it would show
        # up as a difference against a normal space and fake a change.
        self.assertEqual(normalize("a\xa0b"), "a b")

    def test_empty_input(self):
        self.assertEqual(normalize("   \n  "), "")


class CollectTextTests(unittest.TestCase):
    def test_inline_tags_do_not_split_a_value(self):
        html = '<div>$1<span>,299</span>.00</div>'
        self.assertEqual(collect_text(html), "$1,299.00")

    def test_block_tags_keep_neighbouring_text_apart(self):
        self.assertEqual(collect_text("<p>a</p><p>b</p>"), "a b")

    def test_skips_script_and_style_contents(self):
        html = (
            "<style>.x{color:red}</style>"
            "<p>real</p>"
            '<script>var s = "<p>ghost</p>";</script>'
        )
        self.assertEqual(collect_text(html), "real")

    def test_skips_noscript_and_template(self):
        html = "<noscript>hidden</noscript><template>also</template><p>shown</p>"
        self.assertEqual(collect_text(html), "shown")

    def test_decodes_entities(self):
        self.assertEqual(collect_text("<p>a&amp;b&nbsp;c</p>"), "a&b c")

    def test_includes_the_title(self):
        self.assertIn("Doc", collect_text("<title>Doc</title><p>x</p>"))


class ContainsTests(unittest.TestCase):
    def _extract(self, html, **spec):
        return build_extractor({"kind": "contains", **spec})(html)

    def test_reports_present_and_absent(self):
        html = "<p>In Stock</p>"
        self.assertEqual(self._extract(html, needle="In Stock"), "present")
        self.assertEqual(self._extract(html, needle="Sold Out"), "absent")

    def test_is_case_insensitive_by_default(self):
        self.assertEqual(self._extract("<p>IN STOCK</p>", needle="in stock"), "present")

    def test_case_sensitive_when_asked(self):
        self.assertEqual(
            self._extract("<p>IN STOCK</p>", needle="in stock", case_sensitive=True),
            "absent",
        )

    def test_does_not_match_text_hidden_in_a_script(self):
        html = '<script>var s = "In Stock";</script><p>Sold Out</p>'
        self.assertEqual(self._extract(html, needle="In Stock"), "absent")

    def test_requires_a_needle(self):
        with self.assertRaises(ConfigError):
            build_extractor({"kind": "contains"})


class RegexTests(unittest.TestCase):
    def test_returns_the_whole_match_by_default(self):
        extract = build_extractor({"kind": "regex", "pattern": r"\d+"})
        self.assertEqual(extract("<p>abc 42 def</p>"), "42")

    def test_returns_a_numbered_group(self):
        extract = build_extractor(
            {"kind": "regex", "pattern": r"\$([\d,]+\.\d{2})", "group": 1}
        )
        self.assertEqual(extract("<p>Now $1,299.00 only</p>"), "1,299.00")

    def test_returns_a_named_group(self):
        extract = build_extractor(
            {"kind": "regex", "pattern": r"\$(?P<price>[\d.]+)", "group": "price"}
        )
        self.assertEqual(extract("<p>$49.99</p>"), "49.99")

    def test_ignore_case(self):
        extract = build_extractor(
            {"kind": "regex", "pattern": "in stock", "ignore_case": True}
        )
        self.assertEqual(extract("<p>IN STOCK</p>"), "IN STOCK")

    def test_no_match_raises_rather_than_returning_empty(self):
        # Returning "" here would look exactly like a change on the next run.
        extract = build_extractor({"kind": "regex", "pattern": r"\d+"})
        with self.assertRaises(ExtractionError):
            extract("<p>no digits</p>")

    def test_invalid_pattern_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            build_extractor({"kind": "regex", "pattern": "([unclosed"})

    def test_missing_group_number_is_caught_before_the_first_run(self):
        with self.assertRaises(ConfigError):
            build_extractor({"kind": "regex", "pattern": r"\d+", "group": 3})

    def test_missing_group_name_is_caught_before_the_first_run(self):
        with self.assertRaises(ConfigError):
            build_extractor(
                {"kind": "regex", "pattern": r"(?P<a>\d+)", "group": "b"}
            )


class ElementTests(unittest.TestCase):
    def test_finds_by_tag(self):
        extract = build_extractor({"kind": "element", "tag": "h1"})
        self.assertEqual(extract("<h1>Title</h1><h1>Second</h1>"), "Title")

    def test_finds_by_id(self):
        extract = build_extractor({"kind": "element", "tag": "div", "id": "price"})
        html = '<div>other</div><div id="price">$10</div>'
        self.assertEqual(extract(html), "$10")

    def test_finds_by_one_class_among_several(self):
        extract = build_extractor(
            {"kind": "element", "tag": "span", "class": "status"}
        )
        html = '<span class="badge status large">Available</span>'
        self.assertEqual(extract(html), "Available")

    def test_index_selects_a_later_match(self):
        extract = build_extractor({"kind": "element", "tag": "li", "index": 1})
        self.assertEqual(extract("<ul><li>one</li><li>two</li></ul>"), "two")

    def test_nested_same_tag_does_not_end_the_capture_early(self):
        extract = build_extractor({"kind": "element", "tag": "div", "id": "outer"})
        html = '<div id="outer">a<div>b</div>c</div>'
        self.assertEqual(extract(html), "a b c")

    def test_index_counts_a_nested_match(self):
        extract = build_extractor(
            {"kind": "element", "tag": "div", "class": "i", "index": 1}
        )
        html = '<div class="i">outer <div class="i">inner</div></div>'
        self.assertEqual(extract(html), "inner")

    def test_unclosed_element_still_yields_its_text(self):
        extract = build_extractor({"kind": "element", "tag": "div", "id": "x"})
        self.assertEqual(extract('<div id="x">value'), "value")

    def test_no_match_raises(self):
        extract = build_extractor({"kind": "element", "tag": "div", "id": "gone"})
        with self.assertRaises(ExtractionError):
            extract("<div id='here'>x</div>")

    def test_matching_element_with_no_text_is_not_an_error(self):
        extract = build_extractor({"kind": "element", "tag": "div", "id": "x"})
        self.assertEqual(extract('<div id="x"></div>'), "")

    def test_void_tag_is_rejected_at_build_time(self):
        with self.assertRaises(ConfigError):
            build_extractor({"kind": "element", "tag": "img"})

    def test_tag_is_matched_case_insensitively(self):
        extract = build_extractor({"kind": "element", "tag": "H1"})
        self.assertEqual(extract("<h1>Title</h1>"), "Title")

    def test_negative_index_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_extractor({"kind": "element", "tag": "p", "index": -1})


class BuildExtractorTests(unittest.TestCase):
    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_extractor({"kind": "xpath", "path": "//div"})

    def test_missing_kind_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_extractor({"tag": "h1"})

    def test_non_mapping_is_rejected(self):
        with self.assertRaises(ConfigError):
            build_extractor("h1")

    def test_misspelled_key_is_rejected_rather_than_ignored(self):
        # Silently ignoring "needl" would leave the extractor with no needle.
        with self.assertRaises(ConfigError) as caught:
            build_extractor({"kind": "contains", "needl": "In Stock"})
        self.assertIn("needl", str(caught.exception))

    def test_boolean_is_not_accepted_as_an_index(self):
        with self.assertRaises(ConfigError):
            build_extractor({"kind": "element", "tag": "p", "index": True})


class DisplayTests(unittest.TestCase):
    def test_truncate_leaves_short_values_alone(self):
        self.assertEqual(truncate("short", limit=10), "short")

    def test_truncate_reports_how_much_was_cut(self):
        result = truncate("x" * 30, limit=10)
        self.assertTrue(result.startswith("x" * 10))
        self.assertIn("+20 more", result)

    def test_describe_names_the_target(self):
        self.assertIn("<div>", describe({"kind": "element", "tag": "div"}))
        self.assertIn("In Stock", describe({"kind": "contains", "needle": "In Stock"}))


if __name__ == "__main__":
    unittest.main()
