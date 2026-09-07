"""Retrieve the target page over HTTP.

Only http and https are ever opened, on the first request and on every redirect.
Left unguarded, urllib's default opener also handles file:// and ftp://, which
would turn a typo (or a redirect from a site you do not control) into the
watcher reading your local disk.
"""

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .errors import FetchError

DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_USER_AGENT = "web-watcher (+https://github.com/Y1ssh/web-watcher)"

#: Refuse to buffer more than this. A watcher should never be the reason a
#: machine runs out of memory because a URL pointed at something enormous.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class Page:
    """A fetched document."""

    #: The URL actually served, after any redirects.
    url: str
    status: int
    html: str
    encoding: str


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but only to another http or https URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        scheme = urllib.parse.urlsplit(newurl).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise FetchError(f"refused to follow a redirect to {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener() -> urllib.request.OpenerDirector:
    """Return an opener whose redirects are restricted to http and https."""
    # Passing a HTTPRedirectHandler subclass makes build_opener use it in place
    # of the default one.
    return urllib.request.build_opener(_SafeRedirectHandler)


_DEFAULT_OPENER: urllib.request.OpenerDirector | None = None


def _default_opener() -> urllib.request.OpenerDirector:
    global _DEFAULT_OPENER
    if _DEFAULT_OPENER is None:
        _DEFAULT_OPENER = build_opener()
    return _DEFAULT_OPENER


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
    max_bytes: int = MAX_RESPONSE_BYTES,
    opener: urllib.request.OpenerDirector | None = None,
) -> Page:
    """Download a page, or raise FetchError explaining why it could not be."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise FetchError(
            f"{url!r} is not an http or https URL, so the watcher will not open it"
        )

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            "Accept-Language": "en",
        },
    )
    opener = opener if opener is not None else _default_opener()

    try:
        with opener.open(request, timeout=timeout) as response:
            # Read one byte past the cap so an oversized page is detectable.
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise FetchError(
                    f"{url} returned more than {max_bytes} bytes; refusing to "
                    f"load it into memory"
                )
            charset = response.headers.get_content_charset()
            status = response.status
            final_url = response.geturl()
    except FetchError:
        raise
    except urllib.error.HTTPError as exc:
        # HTTPError subclasses URLError, so it has to be caught first. It also
        # holds the error response open; without this close() a watcher polling
        # a failing page would leak a socket on every run.
        exc.close()
        raise FetchError(f"{url} returned HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise FetchError(f"could not reach {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise FetchError(f"{url} did not respond within {timeout} seconds") from exc
    except OSError as exc:
        raise FetchError(f"could not read from {url}: {exc}") from exc

    encoding = charset or "utf-8"
    try:
        # errors="replace" keeps one stray byte from failing an unattended run.
        html = raw.decode(encoding, errors="replace")
    except LookupError:
        encoding = "utf-8"
        html = raw.decode(encoding, errors="replace")

    return Page(url=final_url, status=status, html=html, encoding=encoding)
