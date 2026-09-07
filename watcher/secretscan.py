"""Find credentials that were written into files instead of the environment.

The config schema already refuses inline secrets, but that only covers the
config. This scans everything git is tracking, because tracked files are the
ones that actually leave your machine.

It is a net, not a proof. A scanner that reported every long string would be
ignored within a day, so the rules below aim at the shapes real credentials
take, and values that announce themselves as placeholders are skipped. A clean
result means nothing obvious was found -- not that nothing is there.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Files bigger than this are almost certainly not hand-written source.
MAX_FILE_BYTES = 1_000_000

# Some files legitimately contain credential-shaped text: this scanner's own
# tests, fixtures, documentation examples. Without a way to say so, the only
# options are a failing check or a weakened rule, and both end with the scanner
# being ignored.
#
# The markers are assembled from fragments on purpose. Spelled out as literals
# they would appear in this file's own source, and the scanner would exempt
# itself.
_MARKER = "secret" + "scan"
#: Put this in a comment on a line to exempt that one line.
ALLOW_LINE_MARKER = f"{_MARKER}: allow"
#: Put this near the top of a file to exempt the whole file. The scan reports
#: how many files were skipped, so a blanket exemption is never silent.
ALLOW_FILE_MARKER = f"{_MARKER}: allow-file"
#: How far into a file to look for the file-level marker.
MARKER_SEARCH_LINES = 30

#: Never scanned: build output, virtual environments, and runtime data.
SKIP_DIRECTORIES = frozenset({
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "state", "logs", "dist", "build",
})

SKIP_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".whl", ".exe", ".dll", ".so", ".dylib", ".pyc", ".woff", ".woff2",
})

#: A value containing one of these is announcing that it is not real.
PLACEHOLDER_MARKERS = (
    "example", "changeme", "change-me", "change_me", "placeholder", "your-",
    "your_", "yourname", "dummy", "sample", "redacted", "not-a-secret",
    "notasecret", "fake", "test-", "test_", "xxxx", "todo", "insert", "<",
    "${", "abc123", "foo", "bar",
)

#: (rule name, pattern, which capture group holds the value to judge)
_RULES: tuple[tuple[str, re.Pattern[str], int], ...] = (
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), 0),
    (
        "slack-webhook",
        re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9_/+-]{20,}"),
        0,
    ),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), 0),
    (
        "github-token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
        0,
    ),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), 0),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"), 0),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
        0,
    ),
    (
        "assigned-credential",
        re.compile(
            r"""["']?(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|auth)["']?"""
            r"""\s*[:=]\s*["']([^"'\n]{8,})["']""",
            re.IGNORECASE,
        ),
        1,
    ),
)


@dataclass(frozen=True)
class Finding:
    """One suspicious value, located precisely enough to go and look at it."""

    path: Path
    line_number: int
    rule: str
    excerpt: str

    def describe(self, root: Path | None = None) -> str:
        location = self.path
        if root is not None:
            try:
                location = self.path.relative_to(root)
            except ValueError:
                pass
        return f"{location}:{self.line_number}: {self.rule}: {self.excerpt}"


def looks_like_placeholder(value: str) -> bool:
    """Is this value openly pretending, rather than a real credential?"""
    lowered = value.strip().lower()
    if not lowered:
        return True
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    # "xxxxxxxx", "--------", and friends.
    if len(set(lowered)) <= 2:
        return True
    # A value that is just the name of an environment variable.
    if re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", value.strip()):
        return True
    return False


def redact(value: str) -> str:
    """Show enough to find the line, never enough to use the credential."""
    stripped = value.strip()
    if len(stripped) <= 6:
        return "*" * len(stripped)
    return f"{stripped[:4]}...{'*' * 6} ({len(stripped)} chars)"


def has_file_marker(text: str) -> bool:
    """Does this file exempt itself near the top?"""
    head = text.splitlines()[:MARKER_SEARCH_LINES]
    return any(ALLOW_FILE_MARKER in line for line in head)


def scan_text(text: str, path: Path) -> list[Finding]:
    """Scan one file's contents."""
    if has_file_marker(text):
        return []
    findings: list[Finding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 4000:
            # A single enormous line is minified output, not a hand-typed key.
            continue
        if ALLOW_LINE_MARKER in line:
            continue
        for rule, pattern, group in _RULES:
            for match in pattern.finditer(line):
                value = match.group(group)
                if group != 0 and looks_like_placeholder(value):
                    continue
                findings.append(
                    Finding(
                        path=path,
                        line_number=number,
                        rule=rule,
                        excerpt=redact(value),
                    )
                )
    return findings


def scan_paths(paths: list[Path]) -> list[Finding]:
    """Scan a list of files, skipping what cannot hold a hand-written secret."""
    findings: list[Finding] = []
    for path in paths:
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # An unreadable file is not evidence of a secret; move on.
            continue
        findings.extend(scan_text(text, path))
    return findings


def tracked_files(root: Path) -> tuple[list[Path], str]:
    """List the files worth scanning, preferring the ones git is tracking.

    Git-tracked files are the exposure surface: they are what gets pushed. If
    git is unavailable this falls back to walking the tree, which is less
    precise -- it cannot tell an ignored file from a tracked one.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(root),
            capture_output=True,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return _walk(root), "walked the directory (git was unavailable)"

    names = completed.stdout.decode("utf-8", errors="replace").split("\0")
    paths = [root / name for name in names if name]
    return [path for path in paths if path.is_file()], "git-tracked files"


def _walk(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.parts):
            continue
        # Without git we cannot tell tracked from ignored, so leave the file
        # that is *meant* to hold secrets alone rather than crying wolf.
        if path.name == ".env" or path.name.startswith(".env."):
            if path.name != ".env.example":
                continue
        found.append(path)
    return found


def scan_repository(root: Path) -> tuple[list[Finding], str]:
    """Scan a project. Returns the findings and how the files were chosen.

    Files that exempt themselves are counted in the description rather than
    passed over quietly -- a blanket exemption should be visible in the output.
    """
    paths, how = tracked_files(root)
    findings = scan_paths(paths)
    skipped = sum(1 for path in paths if _is_exempt(path))
    if skipped:
        how = f"{how} ({skipped} file(s) skipped by an allow-file marker)"
    return findings, how


def _is_exempt(path: Path) -> bool:
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return False
        return has_file_marker(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
