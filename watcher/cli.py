"""Command line for the watcher: check, watch, show, reset."""

import argparse
import dataclasses
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from . import checker, config as config_module, extract, runlog, schedule, state
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
        description="Watch one value on one web page and report when it changes.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    check = subcommands.add_parser(
        "check", help="run a single check now", epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(check)

    watch = subcommands.add_parser(
        "watch", help="check repeatedly on the configured interval",
        epilog=_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_arguments(watch)
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

    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(config_module.DEFAULT_CONFIG_FILENAME),
        metavar="PATH",
        help="configuration file (default: %(default)s)",
    )
    parser.add_argument("--url", metavar="URL", help="override the configured url")
    parser.add_argument(
        "--state", type=Path, metavar="PATH", help="override state_path"
    )
    parser.add_argument(
        "--log-file", type=Path, metavar="PATH", help="override log_path"
    )


def main(argv: Sequence[str] | None = None, stream: TextIO | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = stream if stream is not None else sys.stdout

    try:
        settings = _load_config(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=out)
        return EXIT_CONFIG

    handlers = {
        "check": _command_check,
        "watch": _command_watch,
        "show": _command_show,
        "reset": _command_reset,
    }
    try:
        return handlers[args.command](settings, args, out)
    except StateError as exc:
        print(f"state error: {exc}", file=out)
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\nstopped", file=out)
        return EXIT_OK


def _load_config(args: argparse.Namespace) -> config_module.Config:
    settings = config_module.load(args.config)
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


def _command_check(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
    try:
        result = checker.run_check(settings)
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

    print(
        f"watching {settings.describe_target()} "
        f"every {settings.interval_seconds}s "
        f"(Ctrl+C to stop)",
        file=out,
    )
    failures = 0

    def one_run() -> None:
        _report(checker.run_check(settings), out)

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
        print(f"value:       {extract.truncate(snapshot.value)}", file=out)
        print(f"first seen:  {snapshot.first_seen_at}", file=out)
        print(f"last check:  {snapshot.last_checked_at}", file=out)
        print(f"last change: {snapshot.last_changed_at or 'never'}", file=out)
        print(
            f"counts:      {snapshot.check_count} check(s), "
            f"{snapshot.change_count} change(s)",
            file=out,
        )
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


def _command_reset(
    settings: config_module.Config, args: argparse.Namespace, out: TextIO
) -> int:
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


def _report(result: checker.Check, out: TextIO) -> None:
    print(_HEADLINE[result.outcome], file=out)
    print(f"  url:   {result.page_url}", file=out)
    if result.outcome is Outcome.CHANGED:
        print(f"  from:  {extract.truncate(result.previous_value or '')}", file=out)
        print(f"  to:    {extract.truncate(result.value)}", file=out)
    else:
        print(f"  value: {extract.truncate(result.value)}", file=out)
    print(f"  saved: {result.snapshot.last_checked_at}", file=out)
