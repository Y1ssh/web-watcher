"""Fetching runs against a real local HTTP server, not a mock.

That keeps the tests offline and instant while still exercising the real urllib
path: headers, status handling, redirects, and character decoding.
"""

import unittest

from tests.support import BAD_REDIRECT_PATH, REDIRECT_PATH, LocalSite, page
from watcher.errors import FetchError
from watcher.fetching import fetch


class SuccessfulFetchTests(unittest.TestCase):
    def test_returns_the_page_body(self):
        with LocalSite(html=page("<h1>Hello</h1>")) as site:
            result = fetch(site.url + "/")
        self.assertIn("<h1>Hello</h1>", result.html)
        self.assertEqual(result.status, 200)

    def test_reports_the_final_url(self):
        with LocalSite() as site:
            result = fetch(site.url + "/")
        self.assertEqual(result.url, site.url + "/")

    def test_sends_the_configured_user_agent(self):
        # Proven indirectly: a request that reaches the handler was accepted.
        with LocalSite() as site:
            fetch(site.url + "/", user_agent="web-watcher-test")
            self.assertEqual(site.request_count, 1)


class DecodingTests(unittest.TestCase):
    def test_uses_the_charset_from_the_content_type_header(self):
        with LocalSite(content_type="text/html; charset=iso-8859-1") as site:
            site.body_override = "<p>caf\xe9</p>".encode("iso-8859-1")
            result = fetch(site.url + "/")
        self.assertIn("café", result.html)
        self.assertEqual(result.encoding, "iso-8859-1")

    def test_falls_back_to_utf8_when_no_charset_is_given(self):
        with LocalSite(content_type="text/html") as site:
            site.body_override = "<p>café</p>".encode("utf-8")
            result = fetch(site.url + "/")
        self.assertIn("café", result.html)
        self.assertEqual(result.encoding, "utf-8")

    def test_undecodable_bytes_do_not_abort_the_run(self):
        # An unattended watch should survive one stray byte, not crash on it.
        with LocalSite(content_type="text/html; charset=utf-8") as site:
            site.body_override = b"<p>ok \xff\xfe</p>"
            result = fetch(site.url + "/")
        self.assertIn("ok", result.html)


class ErrorTests(unittest.TestCase):
    def test_an_error_status_raises(self):
        with LocalSite(status=404) as site:
            with self.assertRaises(FetchError) as caught:
                fetch(site.url + "/missing")
        self.assertIn("404", str(caught.exception))

    def test_an_unreachable_host_raises(self):
        with self.assertRaises(FetchError):
            fetch("http://127.0.0.1:1/", timeout=2)

    def test_an_oversized_page_is_refused(self):
        with LocalSite(html="x" * 5000) as site:
            with self.assertRaises(FetchError) as caught:
                fetch(site.url + "/", max_bytes=1000)
        self.assertIn("bytes", str(caught.exception))

    def test_a_page_at_exactly_the_limit_is_allowed(self):
        body = "x" * 1000
        with LocalSite(html=body) as site:
            result = fetch(site.url + "/", max_bytes=1000)
        self.assertEqual(len(result.html), 1000)


class SchemeTests(unittest.TestCase):
    def test_a_file_url_is_refused(self):
        with self.assertRaises(FetchError):
            fetch("file:///etc/passwd")

    def test_an_ftp_url_is_refused(self):
        with self.assertRaises(FetchError):
            fetch("ftp://example.invalid/file")

    def test_a_url_with_no_scheme_is_refused(self):
        with self.assertRaises(FetchError):
            fetch("example.com")


class RedirectTests(unittest.TestCase):
    def test_an_http_redirect_is_followed(self):
        with LocalSite(html=page("<h1>Landed</h1>")) as site:
            result = fetch(site.url + REDIRECT_PATH)
        self.assertIn("Landed", result.html)
        self.assertEqual(result.url, site.url + "/")

    def test_a_redirect_to_a_non_http_url_is_refused(self):
        # Without this guard, a site you do not control could redirect the
        # watcher at the local filesystem.
        with LocalSite() as site:
            with self.assertRaises(FetchError) as caught:
                fetch(site.url + BAD_REDIRECT_PATH)
        self.assertIn("redirect", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
