"""Durable SQLite intent journal and conservative, identity-checked undo."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from . import filesystem as fs
from .models import BatchResult, FileIdentity, OperationPlan, OperationResult, OperationType, PlannedOperation, Status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(identity: FileIdentity | None) -> str | None:
    return json.dumps(asdict(identity)) if identity else None


def _decode(value: str | dict | None) -> FileIdentity | None:
    return FileIdentity(**(json.loads(value) if isinstance(value, str) else value)) if value else None


class JournalFailure(RuntimeError):
    """A journal write failed: stop without deleting files to conceal the gap."""


def journal_call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except Exception as exc:
        raise JournalFailure(f"Journal update failed ({method.__name__}): {exc}") from exc


def notify(progress: Callable | None, index: int, total: int, result: OperationResult) -> None:
    if progress:
        # UI callbacks are observers and must not interrupt a durable transition.
        try:
            progress(index, total, result)
        except Exception:
            pass


class HistoryStore:
    def __init__(self, path: Path):
        self.path = fs.absolute(Path(path))

    def protects(self, path: Path) -> bool:
        """The journal and its sidecars may never be sorted or undone as user files."""
        path = fs.absolute(path)
        protected = (self.path, *(Path(str(self.path) + suffix) for suffix in ("-journal", "-wal", "-shm")))
        for entry in protected:
            if path == entry:
                return True
            try:
                if path.exists() and entry.exists() and path.samefile(entry):
                    return True
            except OSError:
                pass
        return False

    def _connect(self, *, write: bool = False) -> sqlite3.Connection | None:
        fs.check_ancestors(self.path)
        if not write and not self.path.exists():
            return None
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fs.check_ancestors(self.path)
        connection = sqlite3.connect(self.path if write else self.path.as_uri() + "?mode=ro", uri=not write, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if write:
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL,
                    root TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    session_id TEXT NOT NULL REFERENCES sessions(id), ordinal INTEGER NOT NULL,
                    source TEXT NOT NULL, destination TEXT NOT NULL, operation TEXT NOT NULL,
                    reason TEXT NOT NULL, category TEXT NOT NULL, conflict TEXT NOT NULL,
                    source_identity TEXT NOT NULL, destination_identity TEXT,
                    undo_identity TEXT, status TEXT NOT NULL, stage TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', applied_at TEXT,
                    PRIMARY KEY (session_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS duplicate_reports (
                    id INTEGER PRIMARY KEY, recorded_at TEXT NOT NULL, groups_count INTEGER NOT NULL
                );
            """)
        return connection

    def _write(self, statement: str, values: tuple = ()) -> None:
        connection = self._connect(write=True)
        try:
            with connection:
                connection.execute(statement, values)
        finally:
            connection.close()

    def start_session(self, plan: OperationPlan) -> str:
        root = fs.validate_root(plan.root)
        operations = {OperationType(i.operation).value for i in plan.items}
        mode = next(iter(operations)) if len(operations) == 1 else "mixed"
        session_id = uuid4().hex
        self._write("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (session_id, _now(), str(root), mode, "running"))
        return session_id

    def record_intent(self, session_id: str, ordinal: int, item: PlannedOperation, identity: FileIdentity | None = None) -> None:
        self._write("""INSERT INTO items
            (session_id,ordinal,source,destination,operation,reason,category,conflict,source_identity,status,stage)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (session_id, ordinal, str(item.source), str(item.destination), OperationType(item.operation).value,
             item.reason, item.category, item.conflict, _encode(identity or item.identity), Status.PENDING.value, "intent"))

    def record_destination(self, session_id: str, ordinal: int, identity: FileIdentity) -> None:
        self._write("UPDATE items SET destination_identity=?, stage='destination_ready' WHERE session_id=? AND ordinal=?",
                    (_encode(identity), session_id, ordinal))

    def complete_item(self, session_id: str, ordinal: int, status: Status, error: str = "") -> None:
        self._write("""UPDATE items SET status=?, stage=?, error=?,
            applied_at=CASE WHEN ?='succeeded' THEN ? ELSE applied_at END
            WHERE session_id=? AND ordinal=?""",
            (status.value, status.value, error, status.value, _now(), session_id, ordinal))

    def finish_session(self, session_id: str, status: str) -> None:
        self._write("UPDATE sessions SET status=? WHERE id=?", (status, session_id))

    def begin_undo(self, session_id: str, ordinal: int) -> None:
        self._write("UPDATE items SET status='pending', stage='undo_intent', error='' WHERE session_id=? AND ordinal=?", (session_id, ordinal))

    def record_restore(self, session_id: str, ordinal: int, identity: FileIdentity) -> None:
        self._write("UPDATE items SET undo_identity=?, stage='restore_ready' WHERE session_id=? AND ordinal=?", (_encode(identity), session_id, ordinal))

    def record_duplicate_report(self, count: int) -> None:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("Duplicate group count must be a nonnegative integer")
        self._write("INSERT INTO duplicate_reports(recorded_at,groups_count) VALUES (?,?)", (_now(), count))

    def list_sessions(self, limit: int = 50) -> list[dict]:
        if limit <= 0:
            return []
        connection = self._connect()
        if connection is None:
            return []
        try:
            rows = connection.execute("""SELECT s.*, COUNT(CASE WHEN i.applied_at IS NOT NULL THEN 1 END) AS files,
                COALESCE(SUM(CASE WHEN i.applied_at IS NOT NULL
                    THEN CAST(json_extract(i.source_identity, '$.size') AS INTEGER) ELSE 0 END),0) AS bytes
                FROM sessions s LEFT JOIN items i ON i.session_id=s.id GROUP BY s.id
                ORDER BY s.started_at DESC, s.id DESC LIMIT ?""", (min(int(limit), 10000),)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_session(self, session_id: str) -> dict:
        connection = self._connect()
        if connection is None:
            raise KeyError(f"Unknown session: {session_id}")
        try:
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown session: {session_id}")
            result = dict(row)
            result["items"] = []
            for row in connection.execute("SELECT * FROM items WHERE session_id=? ORDER BY ordinal", (session_id,)):
                item = dict(row)
                for key in ("source_identity", "destination_identity", "undo_identity"):
                    item[key] = json.loads(item[key]) if item[key] else None
                result["items"].append(item)
            result["files"] = sum(i["applied_at"] is not None for i in result["items"])
            result["bytes"] = sum(i["source_identity"]["size"] for i in result["items"] if i["applied_at"] is not None)
            return result
        finally:
            connection.close()

    def statistics(self) -> dict:
        result = {"session_count": 0, "success_files": 0, "success_bytes": 0,
                  "by_type": {}, "by_category": {}, "by_operation": {}, "duplicate_groups": 0}
        connection = self._connect()
        if connection is None:
            return result
        try:
            result["session_count"] = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            result["duplicate_groups"] = connection.execute("SELECT COALESCE(SUM(groups_count),0) FROM duplicate_reports").fetchone()[0]
            for row in connection.execute("SELECT source,source_identity,category,operation FROM items WHERE applied_at IS NOT NULL"):
                size = json.loads(row["source_identity"])["size"]
                result["success_files"] += 1
                result["success_bytes"] += size
                for name, key in (("by_type", Path(row["source"]).suffix.lower() or "(no extension)"),
                                  ("by_category", row["category"] or "Uncategorized"), ("by_operation", row["operation"])):
                    entry = result[name].setdefault(key, {"files": 0, "bytes": 0})
                    entry["files"] += 1
                    entry["bytes"] += size
            return result
        finally:
            connection.close()

    @staticmethod
    def _planned(row: dict) -> PlannedOperation:
        identity = _decode(row["source_identity"])
        if identity is None:
            raise ValueError("Journal source identity is missing")
        return PlannedOperation(Path(row["source"]), Path(row["destination"]), OperationType(row["operation"]),
                                row["reason"], identity, row["conflict"], row["category"])

    def undo_session(self, session_id: str, progress: Callable | None = None) -> BatchResult:
        session = self.get_session(session_id)
        results: list[OperationResult] = []
        errors: list[str] = []
        rows = list(reversed(session["items"]))
        try:
            with fs.root_lock(Path(session["root"])):
                for index, row in enumerate(rows, 1):
                    item = self._planned(row)
                    ordinal = row["ordinal"]
                    if row["status"] != Status.SUCCEEDED.value:
                        result = OperationResult(item, Status.SKIPPED, "Item was not successfully applied, or already undone; pending items require recovery")
                    else:
                        changed = False
                        begun = False
                        try:
                            root = fs.validate_root(Path(session["root"]))
                            fs.validate_path(root, item.source)
                            fs.validate_path(root, item.destination, must_exist=True)
                            if self.protects(item.source) or self.protects(item.destination):
                                raise fs.UnsafePathError("The history database and its sidecars are protected")
                            if item.source == item.destination:
                                raise fs.UnsafePathError("Journal source and destination must differ")
                            expected = _decode(row["destination_identity"])
                            if expected is None or not expected.sha256:
                                raise fs.SourceChangedError("Destination lacks a full recorded identity")
                            fs.verify(item.destination, expected)
                            if item.operation == OperationType.MOVE and item.source.exists():
                                raise FileExistsError("Original source path is occupied")
                            journal_call(self.begin_undo, session_id, ordinal)
                            begun = True
                            if item.operation == OperationType.MOVE:
                                restored = fs.create_transfer(root, item.destination, item.source, expected, OperationType.MOVE)
                                changed = True
                                journal_call(self.record_restore, session_id, ordinal, restored)
                                fs.verify(item.source, restored)
                            fs.remove_verified(root, item.destination, expected)
                            changed = True
                            journal_call(self.complete_item, session_id, ordinal, Status.UNDONE)
                            result = OperationResult(item, Status.UNDONE)
                        except JournalFailure as exc:
                            result = OperationResult(item, Status.PENDING, str(exc))
                            errors.append(str(exc) + "; stopped, preserve files and inspect recovery")
                            results.append(result)
                            notify(progress, index, len(rows), result)
                            break
                        except Exception as exc:
                            # A restoration may have happened even if transfer raised afterwards.
                            if begun and item.operation == OperationType.MOVE and item.source.exists():
                                changed = True
                            result = OperationResult(item, Status.PENDING if changed else Status.CONFLICT, str(exc))
                            if changed:
                                errors.append("Undo stopped with files preserved; inspect recovery")
                                results.append(result)
                                notify(progress, index, len(rows), result)
                                break
                            if begun:
                                # Keep the successful operation retryable after a safe, pre-action undo failure.
                                journal_call(self.complete_item, session_id, ordinal, Status.SUCCEEDED, f"Undo conflict: {exc}")
                    results.append(result)
                    notify(progress, index, len(rows), result)
                if not errors:
                    updated = self.get_session(session_id)
                    applied = [r for r in updated["items"] if r["applied_at"]]
                    state = "undone" if applied and all(r["status"] == Status.UNDONE.value for r in applied) else "undo_partial"
                    journal_call(self.finish_session, session_id, state)
        except Exception as exc:
            errors.append(str(exc))
        return BatchResult(session_id, tuple(results), tuple(errors))

    def recover_session(self, session_id: str) -> dict:
        """Inspect incomplete journal entries without moving, deleting, or updating anything."""
        session = self.get_session(session_id)
        root = fs.validate_root(Path(session["root"]))
        report = {"session_id": session_id, "status": session["status"], "read_only": True, "items": [], "manual_intervention": False}
        for row in session["items"]:
            if row["status"] != Status.PENDING.value:
                continue
            item_report = {"ordinal": row["ordinal"], "stage": row["stage"], "state": "manual_intervention", "recommendation": "Preserve all files and reconcile manually"}
            try:
                item = self._planned(row)
                fs.validate_path(root, item.source)
                fs.validate_path(root, item.destination)
                if self.protects(item.source) or self.protects(item.destination):
                    raise fs.UnsafePathError("The history database and its sidecars are protected")
                source_exists, destination_exists = item.source.exists(), item.destination.exists()
                item_report.update(source_exists=source_exists, destination_exists=destination_exists)
                expected = _decode(row["destination_identity"])
                if expected and destination_exists:
                    fs.verify(item.destination, expected)
                    item_report["destination_matches"] = True
                if row["stage"] == "intent" and source_exists and not destination_exists:
                    fs.verify(item.source, _decode(row["source_identity"]))
                    item_report.update(state="not_applied", recommendation="Create a fresh preview before retrying; no files were changed by recovery")
                elif row["stage"] == "destination_ready" and expected and destination_exists:
                    state = "move_completed_unrecorded" if item.operation == OperationType.MOVE and not source_exists else "transfer_preserved"
                    item_report.update(state=state, recommendation="Verified destination is preserved; journal completion requires explicit reconciliation")
                elif row["stage"] == "restore_ready" and row["undo_identity"] and source_exists:
                    fs.verify(item.source, _decode(row["undo_identity"]))
                    item_report.update(state="restore_preserved", recommendation="Verified restored source is preserved; inspect remaining destination manually")
                elif row["stage"] == "undo_intent" and item.operation == OperationType.COPY and not destination_exists:
                    item_report.update(state="copy_undo_unrecorded", recommendation="Destination is absent; no further deletion is necessary")
            except Exception as exc:
                item_report["error"] = str(exc)
            report["items"].append(item_report)
        report["manual_intervention"] = bool(report["items"]) or session["status"] == "running"
        if session["status"] == "running" and not report["items"]:
            report["recommendation"] = "Session metadata completion is missing; entries have no pending file transitions. Inspect before reconciling metadata."
        return report
