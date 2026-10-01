from pathlib import Path
import math

import pytest

from smartsort.services.watcher import StabilityTracker, TEMPORARY_EXTENSIONS
from smartsort.services.watcher import WatchService
from smartsort.config.manager import load_config
from smartsort.core.history import HistoryStore
from smartsort.core.models import ScanOptions


def test_stability_resets_when_file_changes():
    tracker = StabilityTracker(5)
    path = Path("download.txt")
    assert not tracker.observe(path, (10, 1), 0)
    assert not tracker.observe(path, (10, 1), 4)
    assert not tracker.observe(path, (20, 2), 5)
    assert not tracker.observe(path, (20, 2), 9)
    assert tracker.observe(path, (20, 2), 10)
    tracker.forget(path)
    assert not tracker.observe(path, (20, 2), 11)


@pytest.mark.parametrize("seconds", [0, -1, math.inf, math.nan])
def test_invalid_stability_window(seconds):
    with pytest.raises(ValueError):
        StabilityTracker(seconds)


def test_common_partial_downloads_are_excluded():
    assert {".crdownload", ".part", ".tmp"} <= TEMPORARY_EXTENSIONS


def test_watch_waits_for_stability_and_ignores_existing_and_self_events(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    existing = root / "existing.txt"
    existing.write_text("untouched")
    history = HistoryStore(tmp_path / "state" / "history.sqlite3")
    service = WatchService(root, load_config(), ScanOptions(), history, stable_seconds=5)
    assert not history.path.exists()
    service.notify(existing, created=False)
    service._tick(0)
    assert not service._pending
    new = root / "new.txt"
    new.write_text("first")
    service.notify(new)
    service._tick(0)
    service._tick(4)
    assert new.exists() and not history.path.exists()
    new.write_text("still growing")
    service._tick(5)
    service._tick(9)
    assert new.exists()
    service._tick(10)
    destination = root / "Documents" / "new.txt"
    assert destination.read_text() == "still growing"
    assert existing.read_text() == "untouched"
    service.notify(destination)
    service.notify(root / "partial.crdownload")
    service._tick(20)
    assert len(history.list_sessions()) == 1


def test_watch_observer_callback_failure_does_not_interrupt_file_outcomes(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    def fail(result):
        raise RuntimeError("closed display")
    service = WatchService(root, load_config(), ScanOptions(), HistoryStore(tmp_path / "history.sqlite3"),
                           stable_seconds=1, on_result=fail)
    source = root / "note.txt"
    source.write_text("content")
    service.notify(source)
    service._tick(0)
    service._tick(1)
    assert (root / "Documents" / "note.txt").read_text() == "content"
    assert not service._pending
