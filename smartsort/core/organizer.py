"""Pure organization plans and journaled, exclusive file operations."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import threading
from typing import Callable

from . import filesystem as fs
from .duplicates import find_duplicates
from .history import HistoryStore, JournalFailure, journal_call, notify
from .models import AppConfig, BatchResult, OperationPlan, OperationResult, OperationType, PlannedOperation, ScanOptions, Status
from .rules import destination_dirs, resolve_destination, select_destination
from .scanner import scan


def _name_key(path: Path) -> str:
    return str(path).casefold()


def _occupied(path: Path) -> bool:
    if os.path.lexists(path):
        return True
    # Reserve case-insensitive names on every platform for portable previews.
    if path.parent.exists():
        return any(p.name.casefold() == path.name.casefold() for p in path.parent.iterdir())
    return False


def _reserve(destination: Path, reserved: set[str]) -> tuple[Path, str]:
    original = destination
    number = 0
    while _name_key(destination) in reserved or _occupied(destination):
        number += 1
        destination = original.with_name(f"{original.stem} ({number}){original.suffix}")
    reserved.add(_name_key(destination))
    return destination, "renamed" if number else "none"


def plan_organization(root: Path, config: AppConfig, options: ScanOptions = ScanOptions(),
                      mode: OperationType = OperationType.MOVE, duplicate_action: str = "keep",
                      only_paths: tuple[Path, ...] | None = None) -> OperationPlan:
    """Read and plan only: no folders, databases, locks, or files are created."""
    root = fs.validate_root(Path(root))
    mode = OperationType(mode)
    if duplicate_action not in {"keep", "skip", "quarantine"}:
        raise ValueError("duplicate_action must be keep, skip, or quarantine")
    if duplicate_action == "quarantine" and mode != OperationType.MOVE:
        raise ValueError("Quarantine requires move mode; use the dedicated duplicate quarantine workflow.")
    targets = (*options.destination_dirs, *destination_dirs(root, config), resolve_destination(root, config.duplicates_folder))
    options = replace(options, destination_dirs=tuple(dict.fromkeys(targets)))
    scanned = scan(root, options)
    errors = list(scanned.errors)
    duplicate_members: set[Path] = set()
    duplicate_hashes: dict[Path, str] = {}
    # Identify the keeper using the full eligible scan before selecting requested paths.
    if duplicate_action != "keep":
        report = find_duplicates(scanned.files)
        errors.extend(report.errors)
        for group in report.groups:
            ordered = sorted(group.files, key=lambda p: (str(p.relative_to(root)).casefold(), str(p.relative_to(root))))
            duplicate_members.update(ordered[1:])
            duplicate_hashes.update((path, group.sha256) for path in ordered)
    selected = None
    if only_paths is not None:
        selected = {fs.validate_path(root, Path(p)) for p in only_paths}
        if duplicate_action == "quarantine":
            for path in sorted(selected - duplicate_members, key=lambda p: (str(p).casefold(), str(p))):
                errors.append(f"{path.name}: no longer a verified duplicate extra; skipped. Run a fresh duplicate scan before quarantining.")
            selected.intersection_update(duplicate_members)
    reserved: set[str] = set()
    items: list[PlannedOperation] = []
    files = sorted(scanned.files, key=lambda f: (str(f.path.relative_to(root)).casefold(), str(f.path.relative_to(root))))
    for file in files:
        if selected is not None and file.path not in selected:
            continue
        try:
            fs.validate_path(root, file.path, must_exist=True)
            category, reason = select_destination(file, config)
            conflict = "none"
            if file.path in duplicate_members:
                if duplicate_action == "skip":
                    conflict = "duplicate_skip"
                    reason = "Exact duplicate: keep the first deterministic member and skip this file"
                elif duplicate_action == "quarantine":
                    category = config.duplicates_folder
                    reason = "Exact duplicate: quarantine; the first deterministic member is kept"
            destination = resolve_destination(root, category) / file.path.name
            fs.validate_path(root, destination)
            if conflict != "duplicate_skip":
                destination, conflict = _reserve(destination, reserved)
            identity = replace(file.identity, sha256=duplicate_hashes.get(file.path, file.identity.sha256))
            items.append(PlannedOperation(file.path, destination, mode, reason, identity, conflict, category))
        except (OSError, ValueError) as exc:
            errors.append(f"{file.path.name}: {exc}")
    return OperationPlan(root, tuple(items), tuple(errors))


def execute_plan(plan: OperationPlan, history: HistoryStore, progress: Callable | None = None,
                 cancel: threading.Event | None = None) -> BatchResult:
    """Apply an immutable preview. A new collision or identity requires a new preview."""
    if not plan.items:
        return BatchResult(None, (), plan.errors)
    results: list[OperationResult] = []
    errors = list(plan.errors)
    session_id = None
    stopped = False
    total = len(plan.items)
    try:
        root = fs.validate_root(plan.root)
        with fs.root_lock(root):
            session_id = journal_call(history.start_session, plan)
            for index, item in enumerate(plan.items):
                ordinal = index
                intent = False
                transfer_attempted = False
                destination_recorded = False
                try:
                    if cancel and cancel.is_set():
                        reason = "Cancelled before this file was changed"
                        journal_call(history.record_intent, session_id, ordinal, item)
                        journal_call(history.complete_item, session_id, ordinal, Status.SKIPPED, reason)
                        result = OperationResult(item, Status.SKIPPED, reason)
                    elif item.conflict == "duplicate_skip":
                        journal_call(history.record_intent, session_id, ordinal, item)
                        journal_call(history.complete_item, session_id, ordinal, Status.SKIPPED, item.reason)
                        result = OperationResult(item, Status.SKIPPED, item.reason)
                    else:
                        OperationType(item.operation)
                        source = fs.validate_path(root, item.source, must_exist=True)
                        destination = fs.validate_path(root, item.destination)
                        if history.protects(source) or history.protects(destination):
                            raise fs.UnsafePathError("The history database and its sidecars are protected")
                        if source == destination:
                            raise fs.UnsafePathError("Source and destination must differ")
                        actual = fs.identify(source)
                        if not fs.same_identity(actual, item.identity, digest=bool(item.identity.sha256)):
                            raise fs.SourceChangedError("Source changed after preview; create a fresh preview")
                        if _occupied(destination):
                            raise FileExistsError("Destination appeared after preview; create a fresh preview")
                        journal_call(history.record_intent, session_id, ordinal, item, actual)
                        intent = True
                        transfer_attempted = True
                        created = fs.create_transfer(root, source, destination, actual, OperationType(item.operation))
                        journal_call(history.record_destination, session_id, ordinal, created)
                        destination_recorded = True
                        fs.verify(destination, created)
                        if item.operation == OperationType.MOVE:
                            fs.remove_verified(root, source, actual)
                        journal_call(history.complete_item, session_id, ordinal, Status.SUCCEEDED)
                        result = OperationResult(item, Status.SUCCEEDED)
                except JournalFailure as exc:
                    result = OperationResult(item, Status.PENDING if intent or transfer_attempted else Status.FAILED, str(exc))
                    errors.append(str(exc) + "; batch stopped, preserve files and inspect recovery")
                    stopped = True
                except Exception as exc:
                    uncertain = False
                    if transfer_attempted and not isinstance(exc, FileExistsError):
                        try:
                            uncertain = os.path.lexists(item.destination)
                        except OSError:
                            uncertain = True
                    if destination_recorded or uncertain:
                        # Never erase a transferred artifact just to make the journal look tidy.
                        result = OperationResult(item, Status.PENDING, str(exc))
                        errors.append("Transfer interrupted with files preserved; batch stopped, inspect recovery")
                        stopped = True
                    else:
                        status = Status.NEEDS_REPLAN if isinstance(exc, (FileNotFoundError, FileExistsError, fs.SourceChangedError)) else Status.FAILED
                        try:
                            if not intent:
                                journal_call(history.record_intent, session_id, ordinal, item)
                            journal_call(history.complete_item, session_id, ordinal, status, str(exc))
                            result = OperationResult(item, status, str(exc))
                        except JournalFailure as journal_error:
                            result = OperationResult(item, Status.PENDING if intent else Status.FAILED, str(journal_error))
                            errors.append(str(journal_error) + "; batch stopped")
                            stopped = True
                results.append(result)
                notify(progress, index + 1, total, result)
                if stopped:
                    for remaining_index, remaining in enumerate(plan.items[index + 1:], index + 2):
                        skipped = OperationResult(remaining, Status.SKIPPED, "Batch stopped before this file was changed")
                        results.append(skipped)
                        notify(progress, remaining_index, total, skipped)
                    break
            if not stopped:
                cancelled = bool(cancel and cancel.is_set())
                partial = bool(errors) or any(r.status in {Status.FAILED, Status.NEEDS_REPLAN, Status.CONFLICT, Status.PENDING} for r in results)
                state = "cancelled" if cancelled else "partial" if partial else "completed"
                journal_call(history.finish_session, session_id, state)
    except Exception as exc:
        errors.append(str(exc))
        # Setup can fail before entering the item loop (invalid root, root lock,
        # or unavailable journal). Every preview row still needs a visible result.
        first_unreported = len(results)
        for index, item in enumerate(plan.items[first_unreported:], first_unreported):
            status = Status.FAILED if index == first_unreported else Status.SKIPPED
            reason = f"Batch setup or continuation failed: {exc}; this file was not changed"
            result = OperationResult(item, status, reason)
            results.append(result)
            notify(progress, index + 1, total, result)
    return BatchResult(session_id, tuple(results), tuple(errors))
