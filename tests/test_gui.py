"""Real Tk widgets and disposable filesystem workflows; no personal folders."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import tkinter as tk

import pytest

from smartsort.config.manager import load_config
from smartsort.core.history import HistoryStore
from smartsort.gui import app as desktop


@pytest.fixture
def gui(tmp_path, monkeypatch):
    try:
        window = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")
    window.withdraw()
    errors = []
    monkeypatch.setattr(desktop.messagebox, "showerror", lambda title, text, **kwargs: errors.append(text))
    monkeypatch.setattr(desktop.messagebox, "showwarning", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *args, **kwargs: True)
    selected = tmp_path / "selected"
    selected.mkdir()
    application = desktop.SmartSortApp(window, state_dir=tmp_path / "state")
    application.folder_var.set(str(selected))
    application.test_errors = errors
    yield application, window, selected
    if window.winfo_exists():
        application.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                window.update()
                if not window.winfo_exists():
                    break
            except tk.TclError:
                break
            time.sleep(0.01)


def wait_for(window, condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        window.update()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("Desktop workflow did not complete before the test deadline")


def preview(gui):
    application, window, _ = gui
    application.preview_button.invoke()
    wait_for(window, lambda: application.worker is None and not application.rendering)
    assert application.current_plan is not None, application.test_errors
    return application.current_plan


def organize(gui):
    application, window, _ = gui
    application.organize_button.invoke()
    wait_for(window, lambda: application.worker is None)
    assert application.last_result is not None, application.test_errors
    return application.last_result


def test_gui_import_is_safe_and_does_not_parse_arguments(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.argv=['embedding','--not-a-command']; import smartsort.gui.app; print('imported')"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "imported"
    assert list(tmp_path.iterdir()) == []


def test_gui_startup_has_five_functional_views_and_creates_no_runtime_state(gui):
    application, _, selected = gui
    assert len(application.notebook.tabs()) == 5
    assert application.watch is None
    assert application.current_plan is None
    assert not application.state_dir.exists()
    assert list(selected.iterdir()) == []
    assert str(application.organize_button["state"]) == "disabled"


def test_gui_default_size_keeps_status_summary_and_history_details_visible(gui):
    application, window, _ = gui
    window.geometry("1080x760")
    window.deiconify()
    window.update()

    def visible_inside(widget, container):
        assert widget.winfo_ismapped()
        assert widget.winfo_height() > 1
        assert widget.winfo_rooty() >= container.winfo_rooty()
        assert widget.winfo_rooty() + widget.winfo_height() <= container.winfo_rooty() + container.winfo_height()
        assert widget.winfo_rootx() >= container.winfo_rootx()
        assert widget.winfo_rootx() + widget.winfo_width() <= container.winfo_rootx() + container.winfo_width()

    visible_inside(application.status_label, window)
    for tab, footer in (
        (application.organize_tab, application.summary_label),
        (application.duplicates_tab, application.duplicate_summary_label),
        (application.history_tab, application.history_details),
    ):
        application.notebook.select(tab)
        window.update()
        visible_inside(footer, tab)
        visible_inside(application.status_label, window)
    window.withdraw()


def test_gui_rejects_storage_equal_to_selected_root(gui):
    application, window, selected = gui
    application.state_dir = selected
    application.preview_button.invoke()
    assert application.current_plan is None
    assert "storage must differ" in application.test_errors[-1]
    assert list(selected.iterdir()) == []


def test_gui_preview_is_pure_and_worker_never_reads_tk_variables(gui, monkeypatch):
    application, _, selected = gui
    source = selected / "notes.txt"
    source.write_text("class notes", encoding="utf-8")
    main_thread = threading.get_ident()
    for variable in (application.folder_var, application.mode_var, application.recursive_var,
                     application.hidden_var, application.include_var, application.exclude_var,
                     application.excluded_dirs_var, application.duplicate_action_var):
        original_get = variable.get
        def checked_get(original=original_get):
            assert threading.get_ident() == main_thread, "Tk variables must be read on the UI thread"
            return original()
        monkeypatch.setattr(variable, "get", checked_get)
    plan = preview(gui)
    assert len(plan.items) == 1
    assert source.read_text(encoding="utf-8") == "class notes"
    assert not application.state_dir.exists()
    assert not (selected / "Documents").exists()
    assert application.plan_tree.item("0", "values")[0] == "notes.txt"


def test_gui_large_table_rendering_yields_to_ui_callbacks(gui):
    application, window, selected = gui
    for index in range(450):
        (selected / f"notes-{index:04d}.txt").write_text("notes", encoding="utf-8")
    plan = desktop.plan_organization(*application._snapshot())
    application._show_plan(plan, application._fingerprint(), "organize")
    assert application.rendering
    assert len(application.plan_tree.get_children()) == 200
    ticks = []
    window.after(0, lambda: ticks.append(application.rendering))
    wait_for(window, lambda: not application.rendering)
    assert ticks == [True]
    assert len(application.plan_tree.get_children()) == 450
    assert str(application.organize_button["state"]) == "normal"
    assert not application.state_dir.exists()


def test_gui_executes_the_same_approved_preview_with_real_history(gui, monkeypatch):
    application, _, selected = gui
    source = selected / "notes.txt"
    source.write_text("class notes", encoding="utf-8")
    approved = preview(gui)
    original_execute = desktop.execute_plan
    consumed = []
    def record_plan(plan, *args, **kwargs):
        consumed.append(plan)
        return original_execute(plan, *args, **kwargs)
    monkeypatch.setattr(desktop, "execute_plan", record_plan)
    result = organize(gui)
    assert consumed == [approved]
    assert consumed[0] is approved
    assert result.counts()["succeeded"] == 1
    assert not source.exists()
    assert approved.items[0].destination.read_text(encoding="utf-8") == "class notes"
    assert HistoryStore(application.state_dir / "history.sqlite3").statistics()["success_files"] == 1
    assert application.current_plan is None
    assert not application.test_errors


def test_gui_declined_confirmation_does_not_execute(gui, monkeypatch):
    application, _, selected = gui
    source = selected / "notes.txt"
    source.write_text("retain", encoding="utf-8")
    preview(gui)
    monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *args, **kwargs: False)
    application.organize_button.invoke()
    assert application.worker is None
    assert source.exists()
    assert not application.state_dir.exists()


def test_gui_setting_changes_invalidate_cached_plan(gui):
    application, _, selected = gui
    (selected / "notes.txt").write_text("retain", encoding="utf-8")
    preview(gui)
    application.mode_var.set("copy")
    assert application.current_plan is None
    assert not application.plan_tree.get_children()
    application.organize()
    assert "Preview" in application.test_errors[-1]
    assert not application.state_dir.exists()


def test_gui_rules_edit_validate_apply_and_save_are_real(gui, monkeypatch):
    application, _, selected = gui
    document = {"version": 1, "rules": [{"name": "Notes", "extensions": [".txt"], "destination": "Study/Notes"}], "fallback": "Other"}
    application._set_rules_document(document)
    application.validate_rules()
    assert "Valid" in application.rule_status.get()
    application.apply_rules()
    (selected / "notes.txt").write_text("study", encoding="utf-8")
    plan = preview(gui)
    assert plan.items[0].destination == selected / "Study" / "Notes" / "notes.txt"
    output = selected.parent / "saved-rules.json"
    monkeypatch.setattr(desktop.filedialog, "asksaveasfilename", lambda **kwargs: str(output))
    application.save_rules()
    assert load_config(output).rules[0].destination == "Study/Notes"
    assert application.current_plan is None
    application._set_rules_document({"version": 1, "rules": [{"destination": "../escape"}]})
    application.apply_rules()
    assert application.test_errors
    assert application.config.rules[0].destination == "Study/Notes"


def test_gui_rules_reject_duplicate_json_keys(gui):
    application, _, _ = gui
    application.rules_text.delete("1.0", "end")
    application.rules_text.insert("1.0", '{"version":1,"version":1,"rules":[]}')
    application.validate_rules()
    assert "Duplicate JSON key" in application.test_errors[-1]


def test_gui_loaded_rule_file_inside_selected_folder_is_excluded(gui, monkeypatch):
    application, _, selected = gui
    configuration = selected / "custom-rules.json"
    configuration.write_text(json.dumps({"version": 1, "rules": [], "fallback": "Others"}), encoding="utf-8")
    source = selected / "notes.txt"
    source.write_text("retain configuration", encoding="utf-8")
    monkeypatch.setattr(desktop.filedialog, "askopenfilename", lambda **kwargs: str(configuration))
    application.load_rules()
    application.apply_rules()
    plan = preview(gui)
    assert [item.source for item in plan.items] == [source]
    assert configuration.exists()


def test_gui_folder_and_storage_pickers_update_actual_state(gui, monkeypatch):
    application, _, selected = gui
    other = selected.parent / "other"
    other.mkdir()
    state = selected.parent / "different-state"
    state.mkdir()
    choices = iter((str(other), str(state)))
    monkeypatch.setattr(desktop.filedialog, "askdirectory", lambda **kwargs: next(choices))
    application.choose_folder()
    assert application.folder_var.get() == str(other)
    application.choose_state_dir()
    assert application.state_dir == state
    assert list(state.iterdir()) == []


def test_gui_duplicate_scan_and_selected_extra_quarantine_preserve_keeper(gui):
    application, window, selected = gui
    keeper = selected / "a.txt"
    extra = selected / "b.md"
    keeper.write_text("identical", encoding="utf-8")
    extra.write_text("identical", encoding="utf-8")
    application.scan_duplicates()
    wait_for(window, lambda: application.worker is None)
    assert len(application.duplicate_report.groups) == 1
    assert len(application.duplicate_paths) == 1
    item = next(iter(application.duplicate_paths))
    assert application.duplicate_paths[item] == extra
    application.duplicate_tree.selection_set(item)
    application.quarantine_button.invoke()
    wait_for(window, lambda: application.worker is None)
    assert application.plan_kind == "quarantine"
    assert len(application.current_plan.items) == 1
    assert application.current_plan.items[0].destination == selected / "Duplicates" / extra.name
    result = organize(gui)
    assert result.counts()["succeeded"] == 1
    assert keeper.exists()
    assert not extra.exists()
    assert (selected / "Duplicates" / extra.name).read_text(encoding="utf-8") == "identical"
    assert HistoryStore(application.state_dir / "history.sqlite3").statistics()["duplicate_groups"] == 1


def test_gui_history_detail_undo_statistics_and_logs_use_recorded_results(gui):
    application, window, selected = gui
    source = selected / "notes.txt"
    source.write_text("history", encoding="utf-8")
    preview(gui)
    result = organize(gui)
    application.refresh_history()
    wait_for(window, lambda: application.worker is None)
    application.history_tree.selection_set(result.session_id)
    application.view_session()
    wait_for(window, lambda: application.worker is None)
    assert result.session_id in application.history_details.get("1.0", "end")
    application.undo_session()
    wait_for(window, lambda: application.worker is None)
    assert source.read_text(encoding="utf-8") == "history"
    assert application.last_result.counts()["undone"] == 1
    application.history_tree.selection_set(result.session_id)
    application.recover_session()
    wait_for(window, lambda: application.worker is None)
    assert "Recovery inspected" in application.status.get()
    assert '"read_only": true' in application.history_details.get("1.0", "end")
    application.refresh_statistics()
    wait_for(window, lambda: application.worker is None)
    assert "Successful files: 1" in application.statistics_var.get()
    application.refresh_logs()
    wait_for(window, lambda: application.worker is None)
    assert "Desktop session" in application.log_text.get("1.0", "end")
    assert not application.test_errors


def test_gui_worker_error_restores_controls_and_reports_failure(gui, monkeypatch):
    application, window, selected = gui
    (selected / "notes.txt").write_text("retain", encoding="utf-8")
    def fail(*args, **kwargs):
        raise PermissionError("test scan denied")
    monkeypatch.setattr(desktop, "plan_organization", fail)
    application.preview()
    wait_for(window, lambda: application.worker is None)
    assert application.test_errors == ["test scan denied"]
    assert str(application.preview_button["state"]) == "normal"
    assert not application.state_dir.exists()


def test_gui_cancel_event_reaches_executor_and_preserves_unstarted_files(gui, monkeypatch):
    application, window, selected = gui
    source = selected / "notes.txt"
    source.write_text("retain", encoding="utf-8")
    preview(gui)
    entered = threading.Event()
    proceed = threading.Event()
    original_execute = desktop.execute_plan
    def delayed_execute(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return original_execute(*args, **kwargs)
    monkeypatch.setattr(desktop, "execute_plan", delayed_execute)
    application.organize()
    wait_for(window, entered.is_set)
    application.cancel_button.invoke()
    proceed.set()
    wait_for(window, lambda: application.worker is None)
    assert application.last_result.counts()["skipped"] == 1
    assert source.exists()
    assert not (selected / "Documents" / source.name).exists()


def test_gui_reading_absent_history_statistics_and_logs_has_no_side_effects(gui):
    application, window, _ = gui
    for action in (application.refresh_history, application.refresh_statistics, application.refresh_logs):
        action()
        wait_for(window, lambda: application.worker is None)
    assert not application.state_dir.exists()


def test_gui_monitoring_requires_explicit_approval(gui, monkeypatch):
    application, _, _ = gui
    monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *args, **kwargs: False)
    application.start_watch()
    assert application.worker is None
    assert application.watch is None
    assert not application.state_dir.exists()


@pytest.mark.parametrize("early_error", [False, True])
def test_gui_close_stops_monitor_started_before_ui_adoption(tmp_path, monkeypatch, early_error):
    from smartsort.services import watcher
    try:
        window = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")
    window.withdraw()
    main_thread = threading.get_ident()
    reached_final_check = threading.Event()
    release_worker = threading.Event()
    destroyed = threading.Event()
    monitors = []

    class Monitor:
        def __init__(self, *args, **kwargs):
            self.alive = False
            self.stopped = False
            self.on_error = kwargs["on_error"]
            monitors.append(self)
        def start(self):
            self.alive = True
            if early_error:
                self.on_error("Startup monitor error")
        def stop(self):
            self.stopped = True
            self.alive = False
        def join(self, timeout=None):
            pass
        def is_alive(self):
            return self.alive

    class Logger:
        def info(self, message):
            assert message == "Desktop monitoring enabled"
            reached_final_check.set()
            assert release_worker.wait(5)
        def error(self, *args):
            pass

    original_destroy = window.destroy
    def record_destroy():
        assert threading.get_ident() == main_thread
        destroyed.set()
        original_destroy()
    monkeypatch.setattr(window, "destroy", record_destroy)
    monkeypatch.setattr(watcher, "WatchService", Monitor)
    monkeypatch.setattr(desktop, "configure_logging", lambda *args: Logger())
    monkeypatch.setattr(desktop.messagebox, "askyesno", lambda *args, **kwargs: True)
    selected = tmp_path / "selected"
    selected.mkdir()
    application = desktop.SmartSortApp(window, state_dir=tmp_path / "state")
    application.folder_var.set(str(selected))
    try:
        application.start_watch()
        wait_for(window, reached_final_check.is_set)
        if early_error:
            wait_for(window, lambda: "Startup monitor error" in application.watch_status.get())
        assert application.watch is None  # The queued startup result is not adopted yet.
        assert monitors[0].is_alive()
        application.close()
        assert monitors[0].stopped
        release_worker.set()
        deadline = time.monotonic() + 5
        while not destroyed.is_set() and time.monotonic() < deadline:
            window.update()
            time.sleep(0.01)
        assert destroyed.is_set()
        assert not monitors[0].is_alive()
        assert not application.worker.is_alive()
    finally:
        release_worker.set()
        for service in monitors:
            service.stop()
        if application.worker is not None:
            application.worker.join(timeout=5)
        if not destroyed.is_set():
            original_destroy()


def test_gui_startup_failure_stops_monitor_and_releases_lifecycle_ownership(gui, monkeypatch):
    from smartsort.services import watcher
    application, window, _ = gui
    monitors = []

    class Monitor:
        def __init__(self, *args, **kwargs):
            self.alive = False
            self.joined = False
            monitors.append(self)
        def start(self):
            self.alive = True
            raise RuntimeError("Monitor startup failed after activation")
        def stop(self):
            self.alive = False
        def join(self, timeout=None):
            self.joined = True
        def is_alive(self):
            return self.alive

    monkeypatch.setattr(watcher, "WatchService", Monitor)
    application.start_watch()
    wait_for(window, lambda: application.worker is None)
    assert application.test_errors == ["Monitor startup failed after activation"]
    assert monitors[0].joined and not monitors[0].is_alive()
    assert application.watch is None
    assert application._starting_watch is None
    assert str(application.watch_start_button["state"]) == "normal"


def test_gui_real_monitoring_starts_processes_new_stable_file_and_stops(gui):
    pytest.importorskip("watchdog", reason="Real monitoring smoke requires the optional watch extra")
    application, window, selected = gui
    existing = selected / "existing.txt"
    existing.write_text("keep existing at watch startup", encoding="utf-8")
    application.stable_var.set("0.1")
    application.poll_var.set("0.05")
    application.start_watch()
    wait_for(window, lambda: application.worker is None)
    assert application.watch is not None, application.test_errors
    source = selected / "new.txt"
    source.write_text("new stable content", encoding="utf-8")
    destination = selected / "Documents" / source.name
    wait_for(window, lambda: destination.exists() and not source.exists())
    assert existing.exists()
    assert destination.read_text(encoding="utf-8") == "new stable content"
    application.watch_stop_button.invoke()
    wait_for(window, lambda: application.watch is None)
    assert not application.test_errors


@pytest.mark.parametrize("invalid", ["0", "-1", "nan", "inf", "invalid"])
def test_gui_monitoring_rejects_invalid_intervals_without_side_effects(gui, invalid):
    application, _, _ = gui
    application.stable_var.set(invalid)
    application.start_watch()
    assert application.test_errors
    assert application.watch is None
    assert not application.state_dir.exists()
