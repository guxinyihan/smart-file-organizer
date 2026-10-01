"""CLI workflows call the same core services used by the desktop application."""
import argparse
from dataclasses import asdict
import json
import sqlite3
from pathlib import Path
import sys

from smartsort.config.manager import default_state_dir, load_config, save_config, default_config_document
from smartsort.core.models import OperationType, ScanOptions, Status
from smartsort.core.organizer import execute_plan, plan_organization
from smartsort.core.history import HistoryStore
from smartsort.core.scanner import scan
from smartsort.core.rules import destination_dirs
from smartsort.core.duplicates import find_duplicates
from smartsort.utils.logging_utils import configure_logging


def _extensions(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _common(parser: argparse.ArgumentParser, scan_options: bool = False) -> None:
    parser.add_argument("--config", type=Path, help="Versioned rules JSON or legacy extension mapping")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(), help="History/log storage")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    if scan_options:
        parser.add_argument("path", type=Path)
        parser.add_argument("--recursive", action="store_true")
        parser.add_argument("--include-hidden", action="store_true")
        parser.add_argument("--exclude-dir", action="append", default=[], help="Directory name or relative path; repeatable")
        parser.add_argument("--include-ext", type=_extensions, default=(), help="Comma-separated extensions")
        parser.add_argument("--exclude-ext", type=_extensions, default=(), help="Comma-separated extensions")
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--copy", dest="mode", action="store_const", const="copy")
        mode.add_argument("--move", dest="mode", action="store_const", const="move")
        parser.set_defaults(mode="move")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="smartsort", description="Preview, organize and safely undo file operations.")
    result.add_argument("--version", action="version", version="SmartSort 1.0.0")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("preview", "organize"):
        item = commands.add_parser(name, help="Read-only operation plan" if name == "preview" else "Approve and execute a plan")
        _common(item, True)
        item.add_argument("--duplicates", choices=("keep", "skip", "quarantine"), default="keep")
        if name == "organize":
            item.add_argument("--yes", action="store_true", help="Explicitly approve the displayed plan without a prompt")
    duplicates = commands.add_parser("duplicates", help="SHA-256 duplicate report; never automatic deletion")
    _common(duplicates, True)
    duplicates.add_argument("--action", choices=("keep", "skip", "quarantine"), default="keep")
    duplicates.add_argument("--yes", action="store_true")
    history = commands.add_parser("history", help="List durable sessions or inspect one")
    _common(history)
    history.add_argument("session_id", nargs="?")
    undo = commands.add_parser("undo", help="Undo a session with identity and collision checks")
    _common(undo)
    undo.add_argument("session_id")
    undo.add_argument("--yes", action="store_true")
    recover = commands.add_parser("recover", help="Read-only inspection of an interrupted session")
    _common(recover)
    recover.add_argument("session_id")
    _common(commands.add_parser("stats", help="Statistics from actual successful operations"))
    rules = commands.add_parser("rules", help="Validate or create configuration")
    rules.add_argument("action", choices=("validate", "init"))
    rules.add_argument("output", type=Path, nargs="?")
    _common(rules)
    watch = commands.add_parser("watch", help="Explicit opt-in monitoring; Ctrl+C stops")
    _common(watch, True)
    watch.add_argument("--stable-seconds", type=float, default=5.0)
    watch.add_argument("--poll-seconds", type=float, default=1.0)
    _common(commands.add_parser("gui", help="Launch the desktop application"))
    return result


def scan_options(args, config=None) -> ScanOptions:
    if args.state_dir.resolve() == args.path.resolve():
        raise ValueError("History/log storage must differ from the organization root; choose a separate folder.")
    destinations = (args.state_dir.resolve(),)
    if config is not None:
        destinations += destination_dirs(args.path.resolve(), config)
    return ScanOptions(
        recursive=args.recursive, include_hidden=args.include_hidden,
        excluded_dirs=tuple(args.exclude_dir), include_extensions=args.include_ext,
        exclude_extensions=args.exclude_ext, destination_dirs=destinations,
        excluded_files=(args.config.resolve(),) if args.config else (),
    )


def _dump(value) -> None:
    if hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    print(json.dumps(value, default=str, indent=2))


def show_plan(plan, as_json: bool = False) -> None:
    if as_json:
        _dump(plan)
        return
    for item in plan.items:
        print(f"{item.operation.value.upper()} {item.source} -> {item.destination}")
        print(f"  {item.reason} | {item.conflict}")
    for error in plan.errors:
        print(f"SCAN ERROR: {error}", file=sys.stderr)
    print(f"Planned: {len(plan.items)}; scan errors: {len(plan.errors)}")


def show_result(result, as_json: bool = False) -> int:
    if as_json:
        _dump(result)
    else:
        print(f"Session: {result.session_id or 'none'}")
        for item in result.results:
            print(f"{item.status.value}: {item.item.source.name}" + (f" — {item.error}" if item.error else ""))
        counts = result.counts()
        print(f"Succeeded: {counts['succeeded']}; failed: {counts['failed']}; skipped: {counts['skipped']}; needs replan: {counts['needs_replan']}; undone: {counts['undone']}; conflicts: {counts['conflict']}")
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
    return 2 if result.errors or any(r.status in (Status.FAILED, Status.NEEDS_REPLAN, Status.CONFLICT, Status.PENDING) for r in result.results) else 0


def _approve(yes: bool, description: str) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ValueError("Non-interactive execution requires --yes; run preview first.")
    try:
        return input(f"{description} [y/N] ").strip().lower() == "y"
    except EOFError as error:
        raise ValueError("Non-interactive execution requires --yes; run preview first.") from error


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "gui":
            from smartsort.gui.app import main as gui_main
            return gui_main(config_path=args.config, state_dir=args.state_dir)
        if args.command == "rules":
            if args.action == "init":
                if args.output is None:
                    raise ValueError("rules init requires an output JSON path")
                if args.output.exists():
                    raise ValueError("Configuration already exists; choose another output path")
                save_config(args.output, default_config_document())
                print(f"Saved validated default rules to {args.output}")
            else:
                config = load_config(args.config)
                print(f"Valid configuration: {len(config.rules)} ordered rules")
            return 0
        store = HistoryStore(args.state_dir / "history.sqlite3")
        if args.command in ("history", "stats", "recover"):
            if args.command == "history":
                value = store.get_session(args.session_id) if args.session_id else store.list_sessions()
            elif args.command == "stats":
                value = store.statistics()
            else:
                value = store.recover_session(args.session_id)
            _dump(value)
            return 0
        if args.command == "undo":
            if not _approve(args.yes, f"Safely undo session {args.session_id}?"):
                print("Cancelled")
                return 0
            logger = configure_logging(args.state_dir)
            result = store.undo_session(args.session_id)
            logger.info("Undo session %s: %s", args.session_id, result.counts())
            return show_result(result, args.json)
        config = load_config(args.config)
        options = scan_options(args, config)
        if args.command == "watch":
            from smartsort.services.watcher import WatchService
            logger = configure_logging(args.state_dir)
            service = WatchService(args.path, config, options, store, mode=OperationType(args.mode),
                                   stable_seconds=args.stable_seconds, poll_seconds=args.poll_seconds,
                                   on_result=lambda result: show_result(result))
            print("Watch mode explicitly enabled. Ctrl+C stops it; existing files are not automatically processed.")
            service.start()
            try:
                service.join()
            except KeyboardInterrupt:
                service.stop()
                service.join()
            logger.info("Watch stopped")
            return 2 if service.error else 0
        if args.command == "duplicates":
            scanned = scan(args.path, options)
            report = find_duplicates(scanned.files)
            if args.json and args.action != "quarantine":
                _dump(report)
            elif not args.json:
                for number, group in enumerate(report.groups, 1):
                    print(f"Group {number}: {group.size} bytes, SHA-256 {group.sha256}")
                    for path in group.files:
                        print(f"  {path}")
                for error in (*scanned.errors, *report.errors):
                    print(f"ERROR: {error}", file=sys.stderr)
                print(f"Duplicate groups: {len(report.groups)}; candidate files hashed: {report.hashed_files}")
            if hasattr(store, "record_duplicate_report"):
                store.record_duplicate_report(len(report.groups))
            if args.action != "quarantine":
                if args.action == "skip" and not args.json:
                    print("Keep the first path in each group; skip the remaining paths when organizing with --duplicates skip.")
                return 2 if scanned.errors or report.errors else 0
            paths = tuple(path for group in report.groups for path in group.files[1:])
            if not paths:
                if args.json:
                    _dump(report)
                return 0
            plan = plan_organization(args.path, config, options, OperationType.MOVE, "quarantine", only_paths=paths)
        else:
            plan = plan_organization(args.path, config, options, OperationType(args.mode), args.duplicates)
        if args.command == "preview":
            show_plan(plan, args.json)
            return 2 if plan.errors else 0
        if not args.json:
            show_plan(plan)
        if not _approve(args.yes, f"Execute {len(plan.items)} planned operations?"):
            print("Cancelled")
            return 0
        logger = configure_logging(args.state_dir)
        result = execute_plan(plan, store)
        logger.info("Session %s: %s", result.session_id, result.counts())
        return show_result(result, args.json)
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as error:
        print(f"SmartSort: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
