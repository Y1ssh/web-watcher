"""The pre-launch checklist, as a command rather than a document.

On your own machine a mistake is private and reversible. In production the same
mistake leaks a credential, spams a channel, or fires a real action at a real
website. A checklist you have to remember to follow is one you will eventually
skip, so this runs the checks instead.

Every check reports PASS, WARN, or FAIL:

* FAIL means the watcher will not work, or will do something you did not
  intend. It exits non-zero, so CI and a deploy script can refuse.
* WARN means it will work, but something deserves a decision from you.
"""

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from . import extract, fetching, secretscan
from .config import Config
from .errors import ExtractionError, FetchError

#: Below this, a schedule starts to look like hammering the site you watch.
GENTLE_INTERVAL_SECONDS = 60

#: What .gitignore must exclude before any of this is safe to push.
REQUIRED_IGNORES = (".env", "config.json", "state/", "logs/")


class Level(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Result:
    name: str
    level: Level
    message: str
    advice: str = ""


@dataclass(frozen=True)
class Report:
    results: tuple[Result, ...]

    @property
    def failures(self) -> tuple[Result, ...]:
        return tuple(r for r in self.results if r.level is Level.FAIL)

    @property
    def warnings(self) -> tuple[Result, ...]:
        return tuple(r for r in self.results if r.level is Level.WARN)

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        passed = sum(1 for r in self.results if r.level is Level.PASS)
        return (
            f"{passed} passed, {len(self.warnings)} warning(s), "
            f"{len(self.failures)} failure(s)"
        )


def run(config: Config, *, project_root: Path, offline: bool = False) -> Report:
    """Run every check and collect the results."""
    results = [
        _check_secrets(project_root),
        _check_gitignore(project_root),
        _check_environment(config),
        _check_state(config),
        _check_notify(config),
        _check_action(config),
        _check_interval(config),
    ]
    results.append(
        _skipped_target() if offline else _check_target(config)
    )
    return Report(tuple(results))


# -- individual checks -----------------------------------------------------


def _check_secrets(root: Path) -> Result:
    findings, how = secretscan.scan_repository(root)
    if findings:
        listed = "; ".join(finding.describe(root) for finding in findings[:5])
        extra = "" if len(findings) <= 5 else f" (+{len(findings) - 5} more)"
        return Result(
            "secrets",
            Level.FAIL,
            f"{len(findings)} possible credential(s) in {how}: {listed}{extra}",
            "Move each value into an environment variable and rotate it -- "
            "anything already pushed should be treated as public.",
        )
    return Result("secrets", Level.PASS, f"nothing credential-shaped in {how}")


def _check_gitignore(root: Path) -> Result:
    path = root / ".gitignore"
    if not path.exists():
        return Result(
            "gitignore",
            Level.FAIL,
            "there is no .gitignore",
            f"Add one excluding {', '.join(REQUIRED_IGNORES)}.",
        )
    try:
        lines = {
            line.strip().rstrip("/")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
    except OSError as exc:
        return Result("gitignore", Level.FAIL, f"could not read .gitignore: {exc}")

    missing = [entry for entry in REQUIRED_IGNORES if entry.rstrip("/") not in lines]
    if missing:
        return Result(
            "gitignore",
            Level.FAIL,
            f".gitignore does not exclude: {', '.join(missing)}",
            "These hold your settings, your credentials, and your run data.",
        )
    return Result(
        "gitignore", Level.PASS, "secrets and runtime files are excluded from git"
    )


def _check_environment(config: Config) -> Result:
    required: list[str] = []
    for channel in config.notify_channels:
        required.extend(channel.required_env())
    if config.action is not None:
        required.extend(config.action.required_env())

    if not required:
        return Result(
            "environment", Level.PASS, "this config needs no environment variables"
        )

    missing = [
        name
        for name in dict.fromkeys(required)
        if not (os.environ.get(name) or "").strip()
    ]
    if missing:
        return Result(
            "environment",
            Level.FAIL,
            f"not set: {', '.join(missing)}",
            "Set these on the host (or in a local .env). Without them the "
            "alert or action fails at the moment it is needed most.",
        )
    return Result(
        "environment",
        Level.PASS,
        f"all {len(set(required))} required variable(s) are set",
    )


def _check_state(config: Config) -> Result:
    path = config.state_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".preflight-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Result(
            "state",
            Level.FAIL,
            f"cannot write to {path.parent}: {exc}",
            "The watcher stores its baseline here; without it there is nothing "
            "to compare against.",
        )

    if config.source is not None and _is_inside(path, config.source.parent):
        return Result(
            "state",
            Level.WARN,
            f"the snapshot lives inside the project at {path}",
            "Most hosts give a deployment a fresh filesystem, which would wipe "
            "this on every deploy -- the watcher would re-baseline and miss the "
            "next real change. Point state_path at a mounted disk such as "
            "/data/snapshot.json.",
        )
    return Result("state", Level.PASS, f"the snapshot directory is writable ({path.parent})")


def _check_notify(config: Config) -> Result:
    if not config.notify_channels:
        return Result(
            "notify",
            Level.WARN,
            "no notification channels are configured",
            "A watcher nobody hears from only fills a log file. Add a channel, "
            "then prove it with `test-notify`.",
        )
    described = ", ".join(channel.describe() for channel in config.notify_channels)
    return Result(
        "notify",
        Level.PASS,
        f"{len(config.notify_channels)} channel(s): {described}",
        "Run `test-notify` to confirm a message actually arrives.",
    )


def _check_action(config: Config) -> Result:
    if config.action is None:
        return Result("action", Level.PASS, "no action configured; alerts only")
    guards = config.action.safeguards
    if guards.dry_run:
        return Result(
            "action",
            Level.PASS,
            f"the action is in sandbox mode: {config.action.describe()}",
            "Nothing will really be sent until dry_run is set to false.",
        )
    return Result(
        "action",
        Level.WARN,
        f"the action is LIVE and will really run: {config.action.describe()}",
        f"Guards in force: run_once={guards.run_once}, "
        f"max_per_24h={guards.max_per_24h}, hours={guards.hours or 'any'}, "
        f"confirm={guards.confirm}. Rehearse a full cycle against a practice "
        f"target before pointing this at the real one.",
    )


def _check_interval(config: Config) -> Result:
    if config.interval_seconds < GENTLE_INTERVAL_SECONDS:
        return Result(
            "interval",
            Level.WARN,
            f"checking every {config.interval_seconds}s",
            "That is fine for testing, but a site may treat it as abusive and "
            "block you. Fifteen minutes suits most pages.",
        )
    return Result(
        "interval", Level.PASS, f"checking every {config.interval_seconds}s"
    )


def _check_target(config: Config) -> Result:
    """Actually fetch the page and read the value out of it."""
    try:
        page = fetching.fetch(
            config.url,
            timeout=config.timeout_seconds,
            user_agent=config.user_agent,
        )
    except FetchError as exc:
        return Result(
            "target",
            Level.FAIL,
            f"could not reach the page: {exc}",
            "A watcher that cannot load its target will do nothing but log "
            "errors once deployed.",
        )

    try:
        value = extract.build_extractor(config.extractor)(page.html)
    except ExtractionError as exc:
        return Result(
            "target",
            Level.FAIL,
            f"the page loaded but the value could not be read: {exc}",
            "Check the extractor against the live page; sites redesign.",
        )

    return Result(
        "target",
        Level.PASS,
        f"HTTP {page.status} from {page.url}, read {extract.truncate(value, 80)!r}",
        "Confirm with your own eyes that this is the value you meant to watch.",
    )


def _skipped_target() -> Result:
    return Result(
        "target",
        Level.WARN,
        "skipped the live page check (--offline)",
        "Run without --offline before deploying, so you know the site is "
        "reachable and the value still reads correctly.",
    )


def _is_inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except (ValueError, OSError):
        return False
    return True


def format_report(report: Report, root: Path) -> str:
    """Render the report for a terminal."""
    lines = ["Web Watcher pre-launch checklist", f"project: {root}", ""]
    for result in report.results:
        lines.append(f"[{result.level.value:4}] {result.name:<12} {result.message}")
        if result.advice and result.level is not Level.PASS:
            lines.append(f"{'':7}{'':<12} -> {result.advice}")
    lines.append("")
    lines.append(report.summary())
    if report.ok:
        lines.append("No blocking problems. Read the warnings before you deploy.")
    else:
        lines.append("Fix the failures above before deploying.")
    return "\n".join(lines)
