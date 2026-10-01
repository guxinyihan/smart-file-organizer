from dataclasses import replace
import json
from pathlib import Path

import pytest

from smartsort.core.history import HistoryStore
from smartsort.core.models import AppConfig, OperationType, OrganizationRule, Status
from smartsort.core.organizer import execute_plan, plan_organization


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    return root, AppConfig((OrganizationRule("Text", "Documents", extensions=(".txt",)),)), HistoryStore(tmp_path / "state" / "history.sqlite")


def applied(setup, mode=OperationType.MOVE, count=1):
    root, config, history = setup
    for index in range(count):
        (root / f"{index}.txt").write_text("content")
    plan = plan_organization(root, config, mode=mode)
    batch = execute_plan(plan, history)
    assert batch.counts()["succeeded"] == count
    return plan, batch


def test_read_only_queries_do_not_create_state(setup):
    root, config, history = setup
    assert history.list_sessions() == []
    assert history.statistics()["session_count"] == 0
    with pytest.raises(KeyError):
        history.get_session("absent")
    assert not history.path.parent.exists()


def test_history_persists_and_multiple_sessions_can_be_undone(setup):
    root, config, history = setup
    first_plan, first = applied(setup)
    (root / "another.txt").write_text("second")
    second_plan = plan_organization(root, config)
    second = execute_plan(second_plan, history)
    reopened = HistoryStore(history.path)
    assert len(reopened.list_sessions()) == 2
    assert reopened.undo_session(second.session_id).counts()["undone"] == 1
    assert reopened.undo_session(first.session_id).counts()["undone"] == 1
    assert reopened.undo_session(first.session_id).counts()["skipped"] == 1
    assert (root / "0.txt").read_text() == "content"
    assert (root / "another.txt").read_text() == "second"


def test_occupied_source_prevents_move_undo(setup):
    plan, batch = applied(setup)
    plan.items[0].source.write_text("new occupant")
    result = setup[2].undo_session(batch.session_id)
    assert result.results[0].status == Status.CONFLICT
    assert plan.items[0].source.read_text() == "new occupant"
    assert plan.items[0].destination.read_text() == "content"
    plan.items[0].source.unlink()
    assert setup[2].undo_session(batch.session_id).counts()["undone"] == 1


@pytest.mark.parametrize("mode", [OperationType.MOVE, OperationType.COPY])
def test_changed_destination_prevents_undo(setup, mode):
    plan, batch = applied(setup, mode)
    plan.items[0].destination.write_text("user edits")
    result = setup[2].undo_session(batch.session_id)
    assert result.results[0].status == Status.CONFLICT
    assert plan.items[0].destination.read_text() == "user edits"


def test_same_content_replacement_is_not_deleted_by_copy_undo(setup):
    plan, batch = applied(setup, OperationType.COPY)
    destination = plan.items[0].destination
    replacement = setup[0] / "replacement"
    replacement.write_bytes(destination.read_bytes())
    # Create replacement before unlink to prevent inode reuse.
    destination.unlink()
    replacement.rename(destination)
    result = setup[2].undo_session(batch.session_id)
    assert result.results[0].status == Status.CONFLICT
    assert destination.read_text() == "content"


@pytest.mark.parametrize("failing_method", ["start_session", "record_intent"])
def test_journal_failure_before_action_leaves_source_unchanged(setup, monkeypatch, failing_method):
    root, config, history = setup
    (root / "a.txt").write_text("original")
    plan = plan_organization(root, config)
    def fail(*args, **kwargs):
        raise RuntimeError("disk unavailable")
    monkeypatch.setattr(history, failing_method, fail)
    result = execute_plan(plan, history)
    assert result.errors
    assert (root / "a.txt").read_text() == "original"
    assert not plan.items[0].destination.exists()


def test_start_session_failure_reports_every_item_without_transfers(setup, monkeypatch):
    root, config, history = setup
    for name in ("a.txt", "b.txt", "c.txt"):
        (root / name).write_text(name)
    plan = plan_organization(root, config)
    observed = []
    def fail(*args, **kwargs):
        raise RuntimeError("journal cannot be opened")
    monkeypatch.setattr(history, "start_session", fail)
    result = execute_plan(plan, history, progress=lambda index, total, item: observed.append((index, total, item.status)))
    assert result.session_id is None
    assert result.errors
    assert [r.status for r in result.results] == [Status.FAILED, Status.SKIPPED, Status.SKIPPED]
    assert [r.item for r in result.results] == list(plan.items)
    assert [index for index, total, status in observed] == [1, 2, 3]
    assert all(total == 3 for index, total, status in observed)
    for item in plan.items:
        assert item.source.read_text() == item.source.name
        assert not item.destination.exists()
    assert not history.path.exists()


@pytest.mark.parametrize("failing_method", ["record_destination", "complete_item"])
def test_journal_failure_after_action_stops_and_recovery_is_read_only(setup, monkeypatch, failing_method):
    root, config, history = setup
    for name in ("a.txt", "b.txt"):
        (root / name).write_text(name)
    plan = plan_organization(root, config)
    def fail(*args, **kwargs):
        raise RuntimeError("journal disk full")
    monkeypatch.setattr(history, failing_method, fail)
    batch = execute_plan(plan, history)
    assert batch.errors
    assert [r.status for r in batch.results] == [Status.PENDING, Status.SKIPPED]
    assert plan.items[0].destination.read_text() == "a.txt"
    assert (root / "b.txt").read_text() == "b.txt"
    before = history.path.read_bytes()
    file_snapshot = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = history.recover_session(batch.session_id)
    assert report["read_only"] and report["manual_intervention"]
    assert history.path.read_bytes() == before
    assert all(p.read_bytes() == data for p, data in file_snapshot.items())


def test_undo_journal_failure_after_restore_preserves_both_files(setup, monkeypatch):
    plan, batch = applied(setup)
    history = setup[2]
    def fail(*args, **kwargs):
        raise RuntimeError("restore journal unavailable")
    monkeypatch.setattr(history, "record_restore", fail)
    result = history.undo_session(batch.session_id)
    assert result.errors and result.results[0].status == Status.PENDING
    assert plan.items[0].source.read_text() == "content"
    assert plan.items[0].destination.read_text() == "content"
    assert history.recover_session(batch.session_id)["manual_intervention"]


def test_copy_undo_journal_failure_after_delete_is_visible(setup, monkeypatch):
    plan, batch = applied(setup, OperationType.COPY)
    history = setup[2]
    def fail(*args, **kwargs):
        raise RuntimeError("completion unavailable")
    monkeypatch.setattr(history, "complete_item", fail)
    result = history.undo_session(batch.session_id)
    assert result.errors
    assert plan.items[0].source.read_text() == "content"
    assert not plan.items[0].destination.exists()
    assert history.recover_session(batch.session_id)["items"][0]["state"] == "copy_undo_unrecorded"


def test_forged_history_destination_cannot_delete_external_file(setup):
    plan, batch = applied(setup, OperationType.COPY)
    root, config, history = setup
    outside = root.parent / "outside.txt"
    outside.write_text("valuable")
    history._write("UPDATE items SET destination=? WHERE session_id=?", (str(outside), batch.session_id))
    result = history.undo_session(batch.session_id)
    assert result.results[0].status == Status.CONFLICT
    assert outside.read_text() == "valuable"
    assert plan.items[0].destination.read_text() == "content"


def test_statistics_are_real_cumulative_activity(setup):
    plan, batch = applied(setup, OperationType.COPY, count=2)
    history = setup[2]
    history.record_duplicate_report(3)
    history.record_duplicate_report(0)
    stats = history.statistics()
    assert stats["session_count"] == 1
    assert stats["success_files"] == 2 and stats["success_bytes"] == 14
    assert stats["by_type"][".txt"] == {"files": 2, "bytes": 14}
    assert stats["by_category"]["Documents"]["files"] == 2
    assert stats["by_operation"]["copy"]["files"] == 2
    assert stats["duplicate_groups"] == 3
    history.undo_session(batch.session_id)
    assert history.statistics()["success_files"] == 2
    assert history.list_sessions()[0]["files"] == 2


def test_complete_destination_identity_includes_sha256(setup):
    plan, batch = applied(setup)
    row = setup[2].get_session(batch.session_id)["items"][0]
    assert len(row["source_identity"]["sha256"]) == 64
    assert len(row["destination_identity"]["sha256"]) == 64
    assert row["destination_identity"]["size"] == 7
