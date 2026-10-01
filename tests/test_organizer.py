from dataclasses import replace
import errno
from pathlib import Path
import threading

import pytest

from smartsort.core import filesystem as fs
from smartsort.core.history import HistoryStore
from smartsort.core.models import AppConfig, OperationPlan, OperationType, OrganizationRule, ScanOptions, Status
from smartsort.core.organizer import execute_plan, plan_organization


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    return root, AppConfig((OrganizationRule("Text", "Documents", extensions=(".txt",)),)), HistoryStore(tmp_path / "state" / "history.sqlite")


def test_preview_is_pure_unknown_nested_batch_collisions(setup):
    root, config, history = setup
    (root / "a").mkdir()
    (root / "b").mkdir()
    (root / "a" / "report.txt").write_text("first")
    (root / "b" / "report.txt").write_text("second")
    (root / "unknown.zzz").write_text("unknown")
    (root / "Documents").mkdir()
    (root / "Documents" / "report.txt").write_text("existing")
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    plan = plan_organization(root, config, ScanOptions(recursive=True))
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before
    assert not history.path.exists()
    assert [p.destination.name for p in plan.items] == ["report (1).txt", "report (2).txt", "unknown.zzz"]
    assert plan.items[-1].category == "Others"
    assert plan == plan_organization(root, config, ScanOptions(recursive=True))
    result = execute_plan(plan, history)
    assert result.counts()["succeeded"] == 3
    assert (root / "Documents" / "report.txt").read_text() == "existing"


def test_copy_creates_separate_file_and_undo_preserves_source(setup):
    root, config, history = setup
    source = root / "a.txt"
    source.write_text("hello")
    plan = plan_organization(root, config, mode=OperationType.COPY)
    result = execute_plan(plan, history)
    assert result.counts()["succeeded"] == 1
    assert not source.samefile(plan.items[0].destination)
    undo = history.undo_session(result.session_id)
    assert undo.counts()["undone"] == 1
    assert source.read_text() == "hello"
    assert not plan.items[0].destination.exists()


@pytest.mark.parametrize("change", ["missing", "modified", "replaced"])
def test_source_changes_require_new_preview(setup, change):
    root, config, history = setup
    source = root / "a.txt"
    source.write_text("original")
    plan = plan_organization(root, config)
    if change == "missing":
        source.unlink()
    elif change == "modified":
        source.write_text("new contents")
    else:
        replacement = root / "replacement"
        replacement.write_text("original")
        source.unlink()
        replacement.rename(source)
    result = execute_plan(plan, history)
    assert result.results[0].status == Status.NEEDS_REPLAN
    assert not plan.items[0].destination.exists()


def test_new_destination_collision_never_overwrites(setup):
    root, config, history = setup
    (root / "a.txt").write_text("source")
    plan = plan_organization(root, config)
    destination = plan.items[0].destination
    destination.parent.mkdir()
    destination.write_text("intervening")
    result = execute_plan(plan, history)
    assert result.results[0].status == Status.NEEDS_REPLAN
    assert destination.read_text() == "intervening"
    assert (root / "a.txt").read_text() == "source"


def test_partial_pre_action_failure_continues(setup, monkeypatch):
    root, config, history = setup
    for name in ("a.txt", "b.txt"):
        (root / name).write_text(name)
    plan = plan_organization(root, config)
    transfer = fs.create_transfer
    def failing_transfer(root, source, destination, identity, operation):
        if source.name == "a.txt":
            raise PermissionError("denied")
        return transfer(root, source, destination, identity, operation)
    monkeypatch.setattr(fs, "create_transfer", failing_transfer)
    result = execute_plan(plan, history)
    assert [r.status for r in result.results] == [Status.FAILED, Status.SUCCEEDED]
    assert (root / "a.txt").exists()
    assert not (root / "b.txt").exists()
    assert history.get_session(result.session_id)["status"] == "partial"


def test_cross_filesystem_move_falls_back_to_exclusive_copy(setup, monkeypatch):
    root, config, history = setup
    (root / "a.txt").write_bytes(b"cross filesystem")
    plan = plan_organization(root, config)
    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")
    monkeypatch.setattr(fs.os, "link", cross_device)
    result = execute_plan(plan, history)
    assert result.counts()["succeeded"] == 1
    assert plan.items[0].destination.read_bytes() == b"cross filesystem"
    undo = history.undo_session(result.session_id)
    assert undo.counts()["undone"] == 1
    assert (root / "a.txt").read_bytes() == b"cross filesystem"


def test_cancellation_and_observer_failure_do_not_damage_files(setup):
    root, config, history = setup
    for name in ("a.txt", "b.txt"):
        (root / name).write_text(name)
    event = threading.Event()
    def progress(index, total, result):
        event.set()
        raise RuntimeError("UI was closed")
    result = execute_plan(plan_organization(root, config), history, progress, event)
    assert [r.status for r in result.results] == [Status.SUCCEEDED, Status.SKIPPED]
    assert (root / "b.txt").read_text() == "b.txt"


def test_duplicate_keeper_is_chosen_before_only_paths(setup):
    root, config, history = setup
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text("identical")
    plan = plan_organization(root, config, duplicate_action="quarantine", only_paths=(root / "b.txt", root / "c.txt"))
    assert len(plan.items) == 2
    assert all(i.category == "Duplicates" for i in plan.items)
    assert not (root / "Duplicates").exists()
    result = execute_plan(plan, history)
    assert result.counts()["succeeded"] == 2
    assert (root / "a.txt").exists()
    assert history.undo_session(result.session_id).counts()["undone"] == 2


def test_stale_quarantine_selection_does_not_fall_back_to_ordinary_sorting(setup):
    root, config, history = setup
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text("same")
    # The user selected extras from an earlier report, then edited one extra.
    selected = (root / "b.txt", root / "c.txt")
    (root / "b.txt").write_text("now unique")
    plan = plan_organization(root, config, duplicate_action="quarantine", only_paths=selected)
    assert [item.source.name for item in plan.items] == ["c.txt"]
    assert all(item.category == "Duplicates" for item in plan.items)
    assert any("b.txt" in warning and "fresh duplicate scan" in warning for warning in plan.errors)
    batch = execute_plan(plan, history)
    assert batch.counts()["succeeded"] == 1
    assert (root / "b.txt").read_text() == "now unique"
    assert not (root / "Documents" / "b.txt").exists()


@pytest.mark.parametrize("stale_kind", ["unique", "missing", "now_keeper"])
def test_no_verified_quarantine_extras_produces_warning_and_no_actions(setup, stale_kind):
    root, config, history = setup
    (root / "a.txt").write_text("same")
    (root / "b.txt").write_text("same")
    if stale_kind == "unique":
        (root / "b.txt").write_text("different")
    elif stale_kind == "missing":
        (root / "b.txt").unlink()
    else:
        # A former extra becomes the sole remaining keeper after its peer changed.
        (root / "a.txt").write_text("different")
    plan = plan_organization(root, config, duplicate_action="quarantine", only_paths=(root / "b.txt",))
    assert not plan.items
    assert any("fresh duplicate scan" in warning for warning in plan.errors)
    result = execute_plan(plan, history)
    assert result.session_id is None and result.errors
    assert not history.path.exists()
    assert not (root / ".smartsort.lock").exists()
    assert not (root / "Documents").exists()
    assert not (root / "Duplicates").exists()


def test_quarantine_preview_binds_content_even_if_size_and_mtime_are_restored(setup):
    import os
    root, config, history = setup
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("aaaa")
    source = root / "b.txt"
    metadata = source.stat()
    plan = plan_organization(root, config, duplicate_action="quarantine", only_paths=(source,))
    assert len(plan.items[0].identity.sha256) == 64
    source.write_text("bbbb")
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    assert source.stat().st_size == metadata.st_size
    assert source.stat().st_mtime_ns == metadata.st_mtime_ns
    result = execute_plan(plan, history)
    assert result.results[0].status == Status.NEEDS_REPLAN
    assert source.read_text() == "bbbb"
    assert not plan.items[0].destination.exists()


def test_duplicate_hashes_are_bound_to_keeper_and_extra_previews(setup):
    root, config, history = setup
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("same")
    plan = plan_organization(root, config, duplicate_action="quarantine")
    assert len(plan.items) == 2
    assert all(len(item.identity.sha256) == 64 for item in plan.items)
    assert plan.items[0].identity.sha256 == plan.items[1].identity.sha256


def test_duplicate_skip_is_previewed_and_not_changed(setup):
    root, config, history = setup
    (root / "a.txt").write_text("same")
    (root / "b.txt").write_text("same")
    plan = plan_organization(root, config, duplicate_action="skip")
    assert [i.conflict for i in plan.items] == ["none", "duplicate_skip"]
    result = execute_plan(plan, history)
    assert [r.status for r in result.results] == [Status.SUCCEEDED, Status.SKIPPED]
    assert (root / "b.txt").read_text() == "same"


def test_forged_plan_cannot_escape_root_or_target_metadata(setup):
    root, config, history = setup
    (root / "a.txt").write_text("safe")
    plan = plan_organization(root, config)
    for destination in (root.parent / "outside.txt", root / ".git" / "config", root / "NUL.txt"):
        forged = OperationPlan(root, (replace(plan.items[0], destination=destination),))
        result = execute_plan(forged, history)
        assert result.results[0].status == Status.FAILED
        assert (root / "a.txt").read_text() == "safe"
        assert not destination.exists()


def test_shared_root_lock_blocks_execution_and_undo(setup):
    root, config, history = setup
    (root / "a.txt").write_text("safe")
    plan = plan_organization(root, config)
    with fs.root_lock(root):
        result = execute_plan(plan, history)
    assert result.errors and result.session_id is None
    assert result.results[0].status == Status.FAILED
    result = execute_plan(plan, history)
    with fs.root_lock(root):
        undo = history.undo_session(result.session_id)
    assert undo.errors
    assert plan.items[0].destination.exists()


def test_invalid_root_reports_every_plan_item_without_transfers(setup):
    root, config, history = setup
    for name in ("a.txt", "b.txt"):
        (root / name).write_text(name)
    plan = plan_organization(root, config)
    result = execute_plan(replace(plan, root=root / "missing"), history)
    assert result.session_id is None and result.errors
    assert [r.status for r in result.results] == [Status.FAILED, Status.SKIPPED]
    assert all(item.source.exists() and not item.destination.exists() for item in plan.items)
    assert not history.path.exists()


def test_history_database_inside_root_is_protected_even_in_a_plan(setup):
    root, config, _ = setup
    history = HistoryStore(root / "history.sqlite")
    history.record_duplicate_report(1)
    (root / "a.txt").write_text("safe")
    plan = plan_organization(root, config)
    result = execute_plan(plan, history)
    protected = next(r for r in result.results if r.item.source == history.path)
    assert protected.status == Status.FAILED
    assert history.path.exists()
    assert history.statistics()["duplicate_groups"] == 1


def test_windows_junction_added_after_preview_is_rejected(setup):
    import os
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows-specific junction test")
    root, config, history = setup
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "a.txt").write_text("safe")
    plan = plan_organization(root, config)
    junction = root / "Documents"
    linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    if linked.returncode:
        pytest.skip("Junction creation unavailable")
    try:
        result = execute_plan(plan, history)
        assert result.results[0].status == Status.FAILED
        assert not (outside / "a.txt").exists()
        assert (root / "a.txt").exists()
    finally:
        os.rmdir(junction)


def test_reparse_parent_added_after_preview_is_rejected(setup):
    root, config, history = setup
    outside = root.parent / "outside"
    outside.mkdir()
    (root / "a.txt").write_text("safe")
    plan = plan_organization(root, config)
    try:
        (root / "Documents").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating a symlink requires privileges on this platform")
    result = execute_plan(plan, history)
    assert result.results[0].status == Status.FAILED
    assert not (outside / "a.txt").exists()
    assert (root / "a.txt").exists()
