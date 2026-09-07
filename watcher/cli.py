"""Command line for the watcher: check, watch, show, reset, test-notify."""

import argparse
import dataclasses
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from . import (
    actions,
    checker,
    clock,
    config as config_module,
    extract,
    notify,
    preflight,
    runlog,
    schedule,
    secrets,
    secretscan,
    state,
)
from .actions import ActionStatus
from .conditions import Verdict
from .detect import Outcome
from .errors import ConfigError, ExtractionError, FetchError, StateError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_FETCH = 3

_EPILOG = """exit codes:
  0  the command completed (for `check`, whatever the outcome)
  1  an unexpected internal error
  2  the configuration or the stored snapshot could not be used
  3  the page could not be fetched, or the watched value was not on it
"""

_HEADLINE = {
    Outcome.BASELINE: "BASELINE   first run - nothing to compare against yet",
    Outcome.UNCHANGED: "UNCHANGED",
    Outcome.CHANGED: "CHANGED",
    Outcome.RETARGETED: "RETARGETED  config now watches something else - baseline reset",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m watcher",
        description="Watch one value on one web page, alert when it changes, "
                    "and optionally act on it.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    check = subcommands.add_parser(
        "check", help="run a single check now", epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(check)
    _add_dry_run(check)

    watch = subcommands.add_parser(
        "watch", help="check repeatedly on the configured interval",
        epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(watch)
    _add_dry_run(watch)
    watch.add_argument(
        "--interval",
        type=int,
        metavar="SECONDS",
        help="override interval_seconds (use a small value to prove the "
             "schedule fires without waiting out a real interval)",
    )
    watch.add_argument(
        "--max-runs",
        type=int,
        metavar="N",
        help="stop after N runs instead of running until interrupted",
    )

    show = subcommands.add_parser(
        "show", help="print the stored snapshot without fetching anything"
    )
    _add_common_arguments(show)
    show.add_argument(
        "--json", action="store_true", help="print the raw stored JSON"
    )
    show.add_argument(
        "--log",
        type=int,
        nargs="?",
        const=10,
        metavar="N",
        help="also print the last N run-log entries (default 10)",
    )

    reset = subcommands.add_parser(
        "reset", help="delete the stored snapshot so the next check re-baselines"
    )
    _add_common_arguments(reset)
    reset.add_argument(
        "--yes", action="store_true", help="required, to confirm the deletion"
    )
    reset.add_argument(
        "--action-only",
        action="store_true",
        help="clear only the action guards, keeping the value baseline",
    )

    test_notify = subcommands.add_parser(
        "test-notify",
        help="send a sample notification now, without waiting for a change",
    )
    _add_common_arguments(test_notify)

    preflight_command = subcommands.add_parser(
        "preflight",
        help="run the pre-launch checklist before deploying",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(preflight_command)
    preflight_command.add_argument(
        "--offline",
        action="store_true",
        help="skip the live page check (for CI, which may have no network)",
    )
    preflight_command.add_argument(
        "--project",
        type=Path,
        metavar="DIR",
        help="project root to scan (default: the config file's directory)",
    )

    scan = subcommands.add_parser(
        "scan-secrets",
        help="look for credentials written into files instead of the environment",
    )
    scan.add_argument(
        "--path",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="directory to scan (default: %(default)s)",
    )

    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help=f"configuration file (default: {config_module.DEFAULT_CONFIG_FILENAME}, "
             f"or the {config_module.CONFIG_ENV_VAR} environment variable)",
    )
    parser.add_argument("--url", metavar="URL", help="override the configured url")
    parser.add_argument(
        "--state", type=Path, metavar="PATH", help="override state_path"
    )
    parser.add_argument(
        "--log-file", type=Path, metavar="PATH", help="override log_path"
    )


def _add_dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="force the action into rehearsal mode for this run. There is no "
             "opposite flag: only the config file can permit a real action.",
    )


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = stream if stream is not None else sys.stdout

    # Scanning for secrets must work on a checkout with no config at all --
    # that is exactly the situation CI runs in.
    if args.command == "scan-secrets":
        return _command_scan_secrets(args, out)

    try:
        args.env_loaded = secrets.load_env_file(
            _env_dir(args) / secrets.DEFAULT_ENV_FILENAME
        )
        settings = _bind_console(_load_config(args), out)
    except ConfigError as exc:
        print(f"config error: {exc}", file=out)
        return EXIT_CONFIG

    handlers = {
        "check": _command_check,
        "watch": _command_watch,
        "show": _command_show,
        "reset": _command_reset,
        "test-notify": _command_test_notify,
        "preflight": _command_preflight,
    }
    try:
        return handlers[args.command](settings, args, out)
    except StateError as exc:
        print(f"state error: {exc}", file=out)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\nstopped", file=out)
        return EXIT_OK


def _bind_console(
    settings: config_module.Config, out: TextIO
) -> config_module.Config:
    """Point console notifications at the same stream the CLI is writing to.

    The channel is built by the config loader, which has no idea where output
    is going. Binding it here keeps an alert and the check that caused it in
    the same place.
    """
    if not any(
        isinstance(channel, notify.ConsoleChannel)
        for channel in settings.notify_channels
    ):
        return settings
    channels = tuple(
        notify.ConsoleChannel(out)
        if isinstance(channel, notify.ConsoleChannel)
        else channel
        for channel in settings.notify_channels
    )
    return dataclasses.replace(settings, notify_channels=channels)


def _env_dir(args: argparse.Namespace) -> Path:
    """Where to look for a .env file: next to the config, else the working dir."""
    return args.config.parent if args.config is not None else Path.cwd()


def _load_config(args: argparse.Namespace) -> config_module.Config:
    """Resolve the config from --config, then the environment, then the default.

    An explicit --config always wins, so a deployment's WATCHER_CONFIG cannot
    silently shadow a file you deliberately pointed at.
    """
    force_dry_run = bool(getattr(args, "dry_run", False))
    if args.config is not None:
        settings = config_module.load(args.config, force_dry_run=force_dry_run)
    else:
        settings = config_module.load_from_env(
            base_dir=Path.cwd(), force_dry_run=force_dry_run
        )
        if settings is None:
            settings = config_module.load(
                Path(config_module.DEFAULT_CONFIG_FILENAME),
                force_dry_run=force_dry_run,
            )

    overrides: dict[str, Any] = {}
    if args.url:
        overrides["url"] = args.url
    if args.state:
        overrides["state_path"] = args.state
    if args.log_file:
        overrides["log_path"] = args.log_file
    if getattr(args, "interval", None) is not None:
        if args.interval < config_module.MIN_INTERVAL_SECONDS:
            raise ConfigError(
                f"--interval must be at least {config_module.MIN_INTERVAL_SECONDS}"
            )
        overrides["interval_seconds"] = args.interval
    return dataclasses.replace(settings, **overrides) if overrides else settings


def _confirm_fn(out: TextIO):
    """Ask on the terminal, or return None when there is no terminal to ask."""
    if not sys.stdin or not sys.stdin.isatty():
        return None

    def ask(description: str) -> bool:
        print(f"\nAbout to perform: {description}", file=out)
        try:
            answer = input("Type 'go' to continue, anything else to cancel: ")
        except (EOFError, KeyboardInterrupt):
            # isatty() can still be true with nothing on the other end. Read
            # silence as "no": never perform a real action on an unanswered
            # prompt.
            print("\nno answer received, so the action was not performed", file=out)
            return False
        return answer.strip().lower() == "go"

    return ask


def _print_startup(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> None:
    """The three things to confirm in a host's log dashboard after a deploy.

    Started, found its settings, and knows where its memory lives. The third --
    that it actually reached the site -- shows up on the first check's own line.
    """
    if settings.source is not None:
        source = str(settings.source)
    elif os.environ.get(config_module.CONFIG_ENV_VAR):
        source = f"${config_module.CONFIG_ENV_VAR}"
    else:
        source = "defaults"

    loaded = getattr(args, "env_loaded", []) or []
    environment = (
        f"{len(loaded)} variable(s) from .env"
        if loaded
        else "using the platform environment"
    )
    print(
        f"web-watcher starting | config: {source} | {environment} | "
        f"state: {settings.state_path}",
        file=out,
    )


def _print_mode_banner(settings: config_module.Config, out: TextIO) -> None:
    """Say plainly, before anything happens, whether an action can fire."""
    if settings.action is None:
        return
    guards = settings.action.safeguards
    if guards.dry_run:
        print(
            "SANDBOX ON - the action will be rehearsed, nothing will really be sent",
            file=out,
        )
    else:
        print(
            f"LIVE - the action WILL really be performed: "
            f"{settings.action.describe()}",
            file=out,
        )


def _command_check(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    _print_mode_banner(settings, out)
    try:
        result = checker.run_check(settings, confirm_fn=_confirm_fn(out))
    except (FetchError, ExtractionError) as exc:
        print(f"check failed: {exc}", file=out)
        return EXIT_FETCH
    _report(result, out)
    return EXIT_OK


def _command_watch(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    if args.max_runs is not None and args.max_runs < 1:
        print("--max-runs must be at least 1", file=out)
        return EXIT_CONFIG

    _print_startup(settings, args, out)
    _print_mode_banner(settings, out)
    print(
        f"watching {settings.describe_target()} "
        f"every {settings.interval_seconds}s, "
        f"alerting when {settings.describe_condition()} "
        f"(Ctrl+C to stop)",
        file=out,
    )
    failures = 0
    confirm = _confirm_fn(out)

    def one_run() -> None:
        _report(checker.run_check(settings, confirm_fn=confirm), out)

    def on_error(exc: BaseException) -> None:
        nonlocal failures
        failures += 1
        print(f"check failed: {exc}", file=out)

    completed = schedule.run_schedule(
        one_run,
        interval_seconds=settings.interval_seconds,
        max_runs=args.max_runs,
        on_error=on_error,
    )
    print(f"stopped after {completed} run(s), {failures} failed", file=out)
    # Every run failing is a real problem, not a quiet success.
    return EXIT_FETCH if completed and failures == completed else EXIT_OK


def _command_show(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    snapshot = state.load(settings.state_path)
    if snapshot is None:
        print(
            f"no snapshot stored yet at {settings.state_path}\n"
            f"run `check` once to record a baseline",
            file=out,
        )
    elif args.json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True), file=out)
    else:
        print(f"watching:    {settings.describe_target()}", file=out)
        print(f"alert when:  {settings.describe_condition()}", file=out)
        print(f"value:       {extract.truncate(snapshot.value)}", file=out)
        print(f"first seen:  {snapshot.first_seen_at}", file=out)
        print(f"last check:  {snapshot.last_checked_at}", file=out)
        print(f"last change: {snapshot.last_changed_at or 'never'}", file=out)
        print(f"last alert:  {snapshot.last_notified_at or 'never'}", file=out)
        print(
            f"counts:      {snapshot.check_count} check(s), "
            f"{snapshot.change_count} change(s)",
            file=out,
        )
        print(
            f"channels:    "
            f"{', '.join(c.describe() for c in settings.notify_channels) or 'none'}",
            file=out,
        )
        _show_action(settings, snapshot, out)
        if snapshot.fingerprint != settings.fingerprint:
            print(
                "note:        the config no longer matches this snapshot, so "
                "the next check will re-baseline",
                file=out,
            )

    if args.log:
        entries = runlog.read(settings.log_path, limit=args.log)
        print(f"\nlast {len(entries)} run-log entries:", file=out)
        for entry in entries:
            print(json.dumps(entry, sort_keys=True), file=out)
    return EXIT_OK


def _show_action(
    settings: config_module.Config, snapshot: state.Snapshot, out: TextIO
) -> None:
    if settings.action is None:
        print("action:      none configured", file=out)
        return
    guards = settings.action.safeguards
    mode = "sandbox (dry run)" if guards.dry_run else "LIVE"
    print(f"action:      {settings.action.describe()}", file=out)
    print(f"action mode: {mode}", file=out)
    print(
        f"action runs: {snapshot.action_total_runs} total, "
        f"{actions.runs_in_last_24h(snapshot.action_runs, clock.now_iso())} "
        f"in the last 24h (limit {guards.max_per_24h})",
        file=out,
    )


def _command_reset(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    if args.action_only:
        return _reset_action_only(settings, args, out)

    if not args.yes:
        print(
            f"this deletes {settings.state_path}, so the next check records a "
            f"new baseline instead of reporting a change.\n"
            f"re-run with --yes to confirm.",
            file=out,
        )
        return EXIT_CONFIG
    if state.clear(settings.state_path):
        print(f"deleted {settings.state_path}", file=out)
    else:
        print(f"nothing to delete at {settings.state_path}", file=out)
    return EXIT_OK


def _reset_action_only(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    snapshot = state.load(settings.state_path)
    if snapshot is None:
        print(f"nothing stored at {settings.state_path}", file=out)
        return EXIT_OK
    if not args.yes:
        print(
            f"this clears the action guards ({snapshot.action_total_runs} "
            f"recorded run(s)), allowing the action to fire again.\n"
            f"re-run with --yes to confirm.",
            file=out,
        )
        return EXIT_CONFIG
    state.save(
        settings.state_path,
        dataclasses.replace(snapshot, action_runs=(), action_total_runs=0),
    )
    print("action guards cleared; the action may fire again", file=out)
    return EXIT_OK


def _command_test_notify(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    """Prove the notification path works without waiting for a real change."""
    if not settings.notify_channels:
        print(
            "no notify channels are configured, so there is nothing to test.\n"
            "add a 'notify.channels' list to your config.",
            file=out,
        )
        return EXIT_CONFIG

    note = notify.build_notification(
        {
            "url": settings.url,
            "value": "SAMPLE-NEW-VALUE",
            "previous_value": "SAMPLE-OLD-VALUE",
            "outcome": "changed",
            "condition": settings.describe_condition(),
            "condition_reason": "this is a test, no real check was performed",
            "at": clock.now_iso(),
        },
        subject_template=settings.notify_subject,
        body_template=settings.notify_body,
        is_test=True,
    )
    deliveries = notify.deliver(list(settings.notify_channels), note)
    print("test notification results:", file=out)
    for delivery in deliveries:
        print(f"  {delivery.summary}", file=out)

    if all(delivery.sent for delivery in deliveries):
        print("\nall channels accepted the message.", file=out)
        return EXIT_OK
    print(
        "\nat least one channel failed. Check the detail above, and confirm "
        "the environment variables it needs are set.",
        file=out,
    )
    return EXIT_FETCH


def _command_preflight(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    root = args.project
    if root is None:
        root = settings.source.parent if settings.source is not None else Path.cwd()
    report = preflight.run(settings, project_root=root, offline=args.offline)
    print(preflight.format_report(report, root), file=out)
    return EXIT_OK if report.ok else EXIT_CONFIG


def _command_scan_secrets(args: argparse.Namespace, out: TextIO) -> int:
    """Runs without a config, so CI can check a bare checkout."""
    root = args.path
    if not root.exists():
        print(f"no such directory: {root}", file=out)
        return EXIT_CONFIG
    findings, how = secretscan.scan_repository(root)
    if not findings:
        print(f"no credential-shaped values found in {how} under {root}", file=out)
        return EXIT_OK
    print(f"{len(findings)} possible credential(s) in {how} under {root}:", file=out)
    for finding in findings:
        print(f"  {finding.describe(root)}", file=out)
    print(
        "\nMove each value into an environment variable. Anything already "
        "pushed should be treated as public: rotate it as well as removing it.",
        file=out,
    )
    return EXIT_CONFIG


def _report(result: checker.Check, out: TextIO) -> None:
    print(_HEADLINE[result.outcome], file=out)
    # The status code is what proves the site was actually reached, which is
    # the one thing easiest to miss when reading a host's logs after a deploy.
    print(f"  url:   {result.page_url} (HTTP {result.status})", file=out)
    if result.outcome is Outcome.CHANGED:
        print(f"  from:  {extract.truncate(result.previous_value or '')}", file=out)
        print(f"  to:    {extract.truncate(result.value)}", file=out)
    else:
        print(f"  value: {extract.truncate(result.value)}", file=out)
    print(f"  saved: {result.snapshot.last_checked_at}", file=out)

    verdict = result.decision.verdict
    label = {
        Verdict.TRUE: "MET",
        Verdict.FALSE: "not met",
        Verdict.UNKNOWN: "UNKNOWN",
    }[verdict]
    print(f"  rule:  {label} - {result.decision.reason}", file=out)

    for delivery in result.deliveries:
        print(f"  alert: {delivery.summary}", file=out)

    if result.action is not None and result.action.status is not ActionStatus.SKIPPED:
        marker = "DONE" if result.action.status is ActionStatus.PERFORMED else (
            result.action.status.value
        )
        print(f"  action: {marker} - {result.action.reason}", file=out)
