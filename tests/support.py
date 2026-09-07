"""Shared test helpers.

The end-to-end tests run against a real HTTP server on localhost rather than a
mocked one. That is deliberate: it exercises the actual urllib path, headers,
redirects, and decoding, while staying entirely offline and instant.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Redirects to this path bounce back to the site root.
REDIRECT_PATH = "/redirect"
#: Redirects to this path aim at a non-HTTP URL, which must be refused.
BAD_REDIRECT_PATH = "/bad-redirect"


class _JoiningServer(ThreadingHTTPServer):
    """A server that waits for its handler threads when it closes.

    ThreadingHTTPServer runs handlers as daemon threads and does not wait for
    them, which leaves connection sockets to be collected later and makes the
    test run emit ResourceWarnings.
    """

    daemon_threads = False
    block_on_close = True


class LocalSite:
    """A throwaway web server whose page a test can change between requests."""

    def __init__(
        self,
        html: str = "<html><body><p>hello</p></body></html>",
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.html = html
        self.status = status
        self.content_type = content_type
        #: Set to raw bytes to bypass UTF-8 encoding (for charset tests).
        self.body_override: bytes | None = None
        self.request_count = 0
        #: Every POST/PUT/PATCH received, so a test can assert on what was sent.
        self.received: list[dict] = []
        #: Status returned to writes; set to 500 to make an action fail.
        self.write_status = 200
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "LocalSite":
        site = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.0 closes each connection after its response. Keep-alive
            # would leave handler sockets open past server_close and make the
            # test run emit ResourceWarnings.
            protocol_version = "HTTP/1.0"

            def do_GET(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
                site.request_count += 1
                if self.path == REDIRECT_PATH:
                    self._redirect_to(f"{site.url}/")
                    return
                if self.path == BAD_REDIRECT_PATH:
                    self._redirect_to("ftp://example.invalid/secret")
                    return
                body = (
                    site.body_override
                    if site.body_override is not None
                    else site.html.encode("utf-8")
                )
                self.send_response(site.status)
                self.send_header("Content-Type", site.content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802 - name fixed by BaseHTTPRequestHandler
                self._record_write()

            def do_PUT(self):  # noqa: N802
                self._record_write()

            def do_PATCH(self):  # noqa: N802
                self._record_write()

            def _record_write(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                site.received.append({
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body.decode("utf-8", errors="replace"),
                })
                self.send_response(site.write_status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def _redirect_to(self, location: str) -> None:
                self.send_response(302)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass  # keep the test output readable

        self._server = _JoiningServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None, "server is only running inside `with`"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"


def page(body: str) -> str:
    """Wrap a fragment in a minimal HTML document."""
    return f"<html><head><title>Test</title></head><body>{body}</body></html>"
