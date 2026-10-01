"""Explicit event monitoring with a testable stability window and serial execution."""
from dataclasses import replace
import math
import logging
import sqlite3
from pathlib import Path
from queue import Empty, Queue
import threading
import time

from smartsort.core.models import OperationType, ScanOptions, Status
from smartsort.core.scanner import scan
from smartsort.core.rules import destination_dirs
from smartsort.core.organizer import execute_plan, plan_organization

TEMPORARY_EXTENSIONS = frozenset((".crdownload", ".part", ".tmp", ".download", ".partial"))


class StabilityTracker:
    def __init__(self, stable_seconds: float):
        if not math.isfinite(stable_seconds) or stable_seconds <= 0:
            raise ValueError("stable_seconds must be a positive finite number")
        self.stable_seconds = stable_seconds
        self._observations: dict[Path, tuple[tuple[int, int], float]] = {}

    def observe(self, path: Path, signature: tuple[int, int], now: float) -> bool:
        previous = self._observations.get(path)
        if previous is None or previous[0] != signature:
            self._observations[path] = (signature, now)
            return False
        return now - previous[1] >= self.stable_seconds

    def forget(self, path: Path) -> None:
        self._observations.pop(path, None)


class WatchService:
    def __init__(self, root: Path, config, options: ScanOptions, history,
                 mode=OperationType.MOVE, stable_seconds=5.0, poll_seconds=1.0,
                 on_result=None, on_error=None):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Watch root must be a directory")
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a positive finite number")
        self.config, self.history, self.mode = config, history, OperationType(mode)
        destinations = options.destination_dirs + destination_dirs(self.root, config)
        self.options = replace(options, destination_dirs=destinations)
        self.tracker = StabilityTracker(stable_seconds)
        self.poll_seconds = poll_seconds
        self.on_result, self.on_error = on_result, on_error
        self._events: Queue[Path] = Queue()
        self._pending: set[Path] = set()
        self._attempts: dict[Path, int] = {}
        self._stop = threading.Event()
        self._thread = None
        self._observer = None
        initial = scan(self.root, self.options)
        if initial.errors:
            raise RuntimeError("Watch scan failed: " + "; ".join(initial.errors))
        self._existing = {item.path for item in initial.files}
        self.error: str | None = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _report_error(self, message: str) -> None:
        logging.getLogger("smartsort").error("Watch: %s", message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                logging.getLogger("smartsort").exception("Watch error observer failed")

    def _eligible_path(self, path: Path) -> bool:
        try:
            relative = path.absolute().relative_to(self.root)
        except ValueError:
            return False
        if not self.options.recursive and len(relative.parts) != 1:
            return False
        if path.suffix.lower() in TEMPORARY_EXTENSIONS:
            return False
        if any(part.startswith(".smartsort") for part in relative.parts):
            return False
        for directory in self.options.destination_dirs:
            if path.absolute().is_relative_to(directory.resolve()):
                return False
        return True

    def notify(self, path: Path, *, created: bool = True) -> None:
        path = Path(path).absolute()
        if self._eligible_path(path) and (created or path not in self._existing):
            self._events.put(path)

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Watch service has already been started")
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as error:
            raise RuntimeError("Watch mode requires the optional extra: pip install '.[watch]'") from error
        service = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    service.notify(Path(event.src_path))

            def on_modified(self, event):
                if not event.is_directory:
                    service.notify(Path(event.src_path), created=False)

            def on_moved(self, event):
                if not event.is_directory:
                    service.notify(Path(event.dest_path))

        self._observer = Observer()
        self._observer.schedule(Handler(), str(self.root), recursive=self.options.recursive)
        self._observer.start()
        self._thread = threading.Thread(target=self._run, name="smartsort-watch", daemon=True)
        self._thread.start()

    def _tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        while True:
            try:
                self._pending.add(self._events.get_nowait())
            except Empty:
                break
        if not self._pending:
            return
        scanned = scan(self.root, self.options)
        if scanned.errors:
            raise RuntimeError("Watch scan failed: " + "; ".join(scanned.errors))
        available = {item.path: item for item in scanned.files}
        ready = []
        for path in sorted(self._pending):
            item = available.get(path)
            if item is None:
                self._pending.discard(path)
                self.tracker.forget(path)
                continue
            signature = (item.identity.size, item.identity.mtime_ns)
            if self.tracker.observe(path, signature, now):
                ready.append(path)
        if not ready or self._stop.is_set():
            return
        plan = plan_organization(self.root, self.config, self.options, self.mode, only_paths=tuple(ready))
        result = execute_plan(plan, self.history, cancel=self._stop)
        if self.on_result:
            try:
                self.on_result(result)
            except Exception:
                logging.getLogger("smartsort").exception("Watch result observer failed")
        if result.errors or any(item.status == Status.PENDING for item in result.results):
            self.error = "; ".join(result.errors) or "Unresolved journal item; inspect history before restarting watch"
            self._report_error(self.error)
            self.stop()
            return
        for operation in result.results:
            path = operation.item.source
            if operation.status in (Status.SUCCEEDED, Status.SKIPPED):
                self._pending.discard(path)
                self._existing.add(path)
                self.tracker.forget(path)
                self._attempts.pop(path, None)
            else:
                self._attempts[path] = self._attempts.get(path, 0) + 1
                self.tracker.forget(path)
                if self._attempts[path] >= 3:
                    self._pending.discard(path)
        # No selected operation may mean the file was excluded or disappeared.
        if not plan.items:
            for path in ready:
                self._pending.discard(path)
                self.tracker.forget(path)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self.poll_seconds):
                try:
                    self._tick()
                except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
                    self.error = str(error)
                    self._report_error(self.error)
                    self.stop()
        finally:
            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=5)

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()

    def join(self, timeout=None) -> None:
        if self._thread:
            self._thread.join(timeout)
