"""Tkinter desktop workflows using the same services as the command line.

Workers never access Tk widgets or variables. All filesystem plans are displayed
before execution; UI input changes invalidate the cached plan.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from smartsort.config.manager import (
    config_to_document, default_config_document, default_state_dir,
    load_config, save_config, validate_config,
)
from smartsort.core.duplicates import find_duplicates
from smartsort.core.history import HistoryStore
from smartsort.core.models import OperationType, ScanOptions
from smartsort.core.organizer import execute_plan, plan_organization
from smartsort.core.rules import destination_dirs
from smartsort.core.scanner import scan
from smartsort.utils.logging_utils import configure_logging


def comma_values(value: str) -> tuple[str, ...]:
    """Convert desktop filter fields to the core's immutable scan options."""
    return tuple(item.strip() for item in value.split(",") if item.strip())


def relative_label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def result_summary(result) -> str:
    counts = result.counts()
    labels = ("succeeded", "failed", "skipped", "needs_replan", "undone", "conflict", "pending")
    parts = [f"{counts[name]} {name.replace('_', ' ')}" for name in labels if counts.get(name)]
    return "; ".join(parts) or "No file operations"


class SmartSortApp:
    def __init__(self, root: tk.Tk, config_path: Path | None = None, state_dir: Path | None = None):
        self.root = root
        self.config_path = Path(config_path) if config_path else None
        self.editor_path = self.config_path
        self.state_dir = Path(state_dir) if state_dir else default_state_dir()
        self.config = load_config(self.config_path)
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.watch = None
        self.cancel_event = threading.Event()
        self.cancellable = False
        self.current_plan = None
        self.plan_kind = "organize"
        self.duplicate_report = None
        self.duplicate_paths: dict[str, Path] = {}
        self.last_result = None
        self.last_error = ""
        self.closing = False
        self.rendering = False
        self._render_after = None
        self._render_epoch = 0
        self.after_id = None
        self._plan_fingerprint = None
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.after_id = self.root.after(60, self._drain_events)

    def _build(self):
        self.root.title("SmartSort — File organizer")
        self.root.geometry("1080x760")
        self.root.minsize(900, 600)
        self.root.configure(background="#f3f7f8")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f3f7f8", foreground="#26383f")
        style.configure("TFrame", background="#f3f7f8")
        style.configure("TLabel", background="#f3f7f8")
        style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"), foreground="#126d71")
        style.configure("Subtitle.TLabel", foreground="#577079")
        style.configure("TButton", padding=(12, 8))
        style.configure("Accent.TButton", background="#126d71", foreground="white")
        style.map("Accent.TButton", background=[("active", "#0c575c"), ("disabled", "#a5b8bb")])
        style.configure("Treeview", background="white", fieldbackground="white", rowheight=28)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"), padding=(6, 8))
        style.configure("TNotebook.Tab", padding=(16, 10))
        outer = ttk.Frame(self.root, padding=22)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 18))
        ttk.Label(header, text="SmartSort", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="A clear plan. A safer place for every file.", style="Subtitle.TLabel").pack(anchor="w", pady=(4, 0))
        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.organize_tab = ttk.Frame(self.notebook, padding=16)
        self.duplicates_tab = ttk.Frame(self.notebook, padding=16)
        self.rules_tab = ttk.Frame(self.notebook, padding=16)
        self.history_tab = ttk.Frame(self.notebook, padding=16)
        self.settings_tab = ttk.Frame(self.notebook, padding=16)
        for frame, name in ((self.organize_tab, "Organize"), (self.duplicates_tab, "Duplicate files"),
                            (self.rules_tab, "Rules"), (self.history_tab, "History"),
                            (self.settings_tab, "Settings & logs")):
            self.notebook.add(frame, text=name)
        self.status = tk.StringVar(value="Choose a folder, then preview your plan. No folders are monitored at startup.")
        ttk.Label(outer, textvariable=self.status, wraplength=980, style="Subtitle.TLabel").pack(fill="x", pady=(12, 0))
        self._actions = []
        self._inputs = []
        self._build_organize()
        self._build_duplicates()
        self._build_rules()
        self._build_history()
        self._build_settings()
        self._sync_controls()

    def _button(self, parent, text, command, *, accent=False):
        button = ttk.Button(parent, text=text, command=command, style="Accent.TButton" if accent else "TButton")
        self._actions.append(button)
        return button

    def _tree(self, parent, columns, widths):
        container = ttk.Frame(parent)
        tree = ttk.Treeview(container, columns=columns, show="headings", selectmode="extended")
        for column, width in zip(columns, widths):
            tree.heading(column, text=column)
            tree.column(column, width=width, minwidth=70, stretch=True)
        vertical = ttk.Scrollbar(container, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        container.pack(fill="both", expand=True, pady=(10, 0))
        return tree

    def _build_organize(self):
        frame = self.organize_tab
        self.folder_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="move")
        self.recursive_var = tk.BooleanVar(value=False)
        self.hidden_var = tk.BooleanVar(value=False)
        self.include_var = tk.StringVar()
        self.exclude_var = tk.StringVar()
        self.excluded_dirs_var = tk.StringVar()
        self.duplicate_action_var = tk.StringVar(value="keep")
        self.summary_var = tk.StringVar(value="Preview shows the exact plan that Organize will use.")
        folder_row = ttk.Frame(frame)
        folder_row.pack(fill="x")
        ttk.Label(folder_row, text="Folder").pack(side="left", padx=(0, 12))
        entry = ttk.Entry(folder_row, textvariable=self.folder_var)
        entry.pack(side="left", fill="x", expand=True)
        self._inputs.append(entry)
        self._button(folder_row, "Choose folder", self.choose_folder).pack(side="left", padx=(10, 0))
        choices = ttk.Frame(frame)
        choices.pack(fill="x", pady=12)
        for text, value in (("Move", "move"), ("Copy", "copy")):
            control = ttk.Radiobutton(choices, text=text, variable=self.mode_var, value=value)
            control.pack(side="left", padx=(0, 12))
            self._inputs.append(control)
        for text, variable in (("Include subfolders", self.recursive_var), ("Include hidden files", self.hidden_var)):
            control = ttk.Checkbutton(choices, text=text, variable=variable)
            control.pack(side="left", padx=(0, 16))
            self._inputs.append(control)
        ttk.Label(choices, text="Duplicates:").pack(side="left", padx=(8, 6))
        duplicate_choice = ttk.Combobox(choices, textvariable=self.duplicate_action_var, values=("keep", "skip"), state="readonly", width=8)
        duplicate_choice.pack(side="left")
        self._inputs.append(duplicate_choice)
        filters = ttk.Frame(frame)
        filters.pack(fill="x")
        for index, (label, variable) in enumerate((("Include extensions", self.include_var),
                                                ("Exclude extensions", self.exclude_var),
                                                ("Exclude folders", self.excluded_dirs_var))):
            box = ttk.Frame(filters)
            box.grid(row=0, column=index, sticky="ew", padx=(0, 12) if index < 2 else 0)
            filters.columnconfigure(index, weight=1)
            ttk.Label(box, text=label).pack(anchor="w")
            control = ttk.Entry(box, textvariable=variable)
            control.pack(fill="x", pady=(4, 0))
            self._inputs.append(control)
        ttk.Label(frame, text="Comma-separated filters, for example .pdf, .txt or .git, node_modules. Leave extensions blank for all.", style="Subtitle.TLabel").pack(anchor="w", pady=(5, 10))
        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        self.preview_button = self._button(actions, "Preview", self.preview)
        self.preview_button.pack(side="left")
        self.organize_button = self._button(actions, "Organize approved plan", self.organize, accent=True)
        self.organize_button.pack(side="left", padx=10)
        self.cancel_button = ttk.Button(actions, text="Cancel after current file", command=self.cancel)
        self.cancel_button.pack(side="left")
        self.progress = ttk.Progressbar(actions, maximum=100, mode="determinate")
        self.progress.pack(side="right", fill="x", expand=True, padx=(20, 0))
        self.plan_tree = self._tree(frame, ("Source", "Destination", "Reason", "Conflict / result"), (250, 250, 185, 160))
        ttk.Label(frame, textvariable=self.summary_var, wraplength=940).pack(anchor="w", pady=(10, 0))
        for variable in (self.folder_var, self.mode_var, self.recursive_var, self.hidden_var,
                         self.include_var, self.exclude_var, self.excluded_dirs_var, self.duplicate_action_var):
            variable.trace_add("write", self._invalidate)

    def _build_duplicates(self):
        ttk.Label(self.duplicates_tab, text="Find identical content, including files with different names.", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(self.duplicates_tab, text="Uses the folder and filters in Organize. The first file in each group is kept; extra files can be moved to quarantine after a preview.", wraplength=930, style="Subtitle.TLabel").pack(anchor="w", pady=(6, 12))
        actions = ttk.Frame(self.duplicates_tab)
        actions.pack(fill="x")
        self._button(actions, "Scan duplicates", self.scan_duplicates).pack(side="left")
        self.quarantine_button = self._button(actions, "Preview quarantine of selected extras", self.preview_quarantine)
        self.quarantine_button.pack(side="left", padx=10)
        self._button(actions, "Keep all when organizing", lambda: self.set_duplicate_action("keep")).pack(side="left")
        self._button(actions, "Skip extras when organizing", lambda: self.set_duplicate_action("skip")).pack(side="left", padx=(10, 0))
        self.duplicate_tree = self._tree(self.duplicates_tab, ("Group", "File", "Size", "Role"), (70, 550, 120, 120))
        self.duplicate_summary = tk.StringVar(value="No duplicate scan yet. Duplicate files are never automatically deleted.")
        ttk.Label(self.duplicates_tab, textvariable=self.duplicate_summary, wraplength=930).pack(anchor="w", pady=(10, 0))

    def _build_rules(self):
        ttk.Label(self.rules_tab, text="Ordered rules · first match wins", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(self.rules_tab, text="Edit validated JSON. Destinations must be relative categories inside the chosen organization folder.", style="Subtitle.TLabel", wraplength=930).pack(anchor="w", pady=(6, 10))
        actions = ttk.Frame(self.rules_tab)
        actions.pack(fill="x")
        for label, command in (("Load rules", self.load_rules), ("Default rules", self.default_rules),
                               ("Validate", self.validate_rules), ("Apply for this session", self.apply_rules),
                               ("Save rules as…", self.save_rules)):
            self._button(actions, label, command).pack(side="left", padx=(0, 8))
        box = ttk.Frame(self.rules_tab)
        box.pack(fill="both", expand=True, pady=(10, 0))
        self.rules_text = tk.Text(box, wrap="none", font=("Consolas", 10), undo=True, borderwidth=1, relief="solid", padx=10, pady=10)
        vertical = ttk.Scrollbar(box, command=self.rules_text.yview)
        horizontal = ttk.Scrollbar(box, orient="horizontal", command=self.rules_text.xview)
        self.rules_text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.rules_text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self._set_rules_document(config_to_document(self.config))
        self.rule_status = tk.StringVar(value=f"Using {len(self.config.rules)} rules. Unsaved edits are applied only when you choose Apply or Save.")
        ttk.Label(self.rules_tab, textvariable=self.rule_status, wraplength=930).pack(anchor="w", pady=(10, 0))

    def _build_history(self):
        ttk.Label(self.history_tab, text="Previous sessions and safe recovery", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(self.history_tab, text="Undo checks file identity and refuses occupied original paths. Recovery inspects interrupted entries and explains any manual review needed.", wraplength=930, style="Subtitle.TLabel").pack(anchor="w", pady=(6, 12))
        actions = ttk.Frame(self.history_tab)
        actions.pack(fill="x")
        for label, command in (("Refresh history", self.refresh_history), ("View selected session", self.view_session),
                               ("Undo selected session", self.undo_session), ("Inspect recovery", self.recover_session)):
            self._button(actions, label, command).pack(side="left", padx=(0, 8))
        self.history_tree = self._tree(self.history_tab, ("Session", "Created", "Mode", "Status", "Folder"), (230, 170, 70, 130, 300))
        self.history_details = tk.Text(self.history_tab, height=7, wrap="word", font=("Consolas", 9), state="disabled", padx=10, pady=8)
        self.history_details.pack(fill="x", pady=(10, 0))

    def _build_settings(self):
        frame = self.settings_tab
        ttk.Label(frame, text="Storage, monitoring and activity", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        storage = ttk.Frame(frame)
        storage.pack(fill="x", pady=(12, 10))
        self.state_var = tk.StringVar(value=str(self.state_dir))
        ttk.Label(storage, text="History and logs:").pack(side="left", padx=(0, 8))
        ttk.Label(storage, textvariable=self.state_var, wraplength=650).pack(side="left", fill="x", expand=True)
        self._button(storage, "Choose storage folder", self.choose_state_dir).pack(side="right")
        monitoring = ttk.LabelFrame(frame, text="Opt-in folder monitoring", padding=12)
        monitoring.pack(fill="x")
        ttk.Label(monitoring, text="Uses the chosen folder and current rules. Files must stop changing before organization; existing files are not processed when monitoring starts.", wraplength=890, style="Subtitle.TLabel").pack(anchor="w")
        watch_row = ttk.Frame(monitoring)
        watch_row.pack(fill="x", pady=(10, 0))
        self.stable_var = tk.StringVar(value="5")
        self.poll_var = tk.StringVar(value="1")
        for label, variable in (("Stable for (seconds)", self.stable_var), ("Check every (seconds)", self.poll_var)):
            ttk.Label(watch_row, text=label).pack(side="left", padx=(0, 8))
            entry = ttk.Entry(watch_row, textvariable=variable, width=6)
            entry.pack(side="left", padx=(0, 16))
            self._inputs.append(entry)
        self.watch_start_button = self._button(watch_row, "Start monitoring", self.start_watch)
        self.watch_start_button.pack(side="left")
        self.watch_stop_button = ttk.Button(watch_row, text="Stop monitoring", command=self.stop_watch)
        self.watch_stop_button.pack(side="left", padx=8)
        self.watch_status = tk.StringVar(value="Monitoring is off.")
        ttk.Label(monitoring, textvariable=self.watch_status, wraplength=890).pack(anchor="w", pady=(10, 0))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 6))
        self._button(actions, "Refresh statistics", self.refresh_statistics).pack(side="left")
        self._button(actions, "Refresh log", self.refresh_logs).pack(side="left", padx=10)
        self.statistics_var = tk.StringVar(value="Statistics are calculated from recorded completed operations.")
        ttk.Label(frame, textvariable=self.statistics_var, wraplength=930).pack(anchor="w", pady=(0, 8))
        self.log_text = tk.Text(frame, wrap="none", font=("Consolas", 9), state="disabled", height=8, padx=10, pady=10)
        self.log_text.pack(fill="both", expand=True)

    def _sync_controls(self):
        active = self.worker is not None or self.watch is not None or self.closing or self.rendering
        for button in self._actions:
            button.configure(state="disabled" if active else "normal")
        for widget in self._inputs:
            widget.configure(state="disabled" if active else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
        self.rules_text.configure(state="disabled" if active else "normal")
        self.organize_button.configure(state="normal" if not active and self.current_plan is not None and self.current_plan.items else "disabled")
        self.cancel_button.configure(state="normal" if self.worker is not None and self.cancellable and not self.closing else "disabled")
        self.watch_stop_button.configure(state="normal" if self.watch is not None and not self.closing else "disabled")
        self.quarantine_button.configure(state="normal" if not active and self.duplicate_report is not None and self.duplicate_report.groups else "disabled")

    def _idle(self):
        if self.worker is not None or self.watch is not None or self.closing or self.rendering:
            self.status.set("Finish the current task or stop monitoring before starting another workflow.")
            return False
        return True

    def _error(self, error):
        self.last_error = str(error)
        self.status.set(f"Error: {error}")
        messagebox.showerror("SmartSort", str(error), parent=self.root)

    def _invalidate(self, *_):
        self._cancel_render()
        self.current_plan = None
        self._plan_fingerprint = None
        self.duplicate_report = None
        if hasattr(self, "plan_tree"):
            self.plan_tree.delete(*self.plan_tree.get_children())
            self.summary_var.set("Settings changed. Preview again before organizing.")
        if hasattr(self, "duplicate_tree"):
            self.duplicate_tree.delete(*self.duplicate_tree.get_children())
            self.duplicate_paths.clear()
        if hasattr(self, "rules_text"):
            self._sync_controls()

    def _cancel_render(self):
        self._render_epoch += 1
        self.rendering = False
        if self._render_after is not None:
            self.root.after_cancel(self._render_after)
            self._render_after = None

    def _render_rows(self, tree, rows, done):
        """Insert large tables in short batches so the desktop keeps responding."""
        self._cancel_render()
        epoch = self._render_epoch
        iterator = iter(rows)
        tree.delete(*tree.get_children())
        self.rendering = True
        self._sync_controls()
        def insert_batch():
            self._render_after = None
            if self.closing or epoch != self._render_epoch:
                return
            try:
                for _ in range(200):
                    identifier, values = next(iterator)
                    tree.insert("", "end", iid=identifier, values=values)
            except StopIteration:
                self.rendering = False
                done()
                self._sync_controls()
                return
            self._render_after = self.root.after(1, insert_batch)
        insert_batch()

    def _fingerprint(self):
        return (self.folder_var.get(), self.mode_var.get(), self.recursive_var.get(), self.hidden_var.get(),
                self.include_var.get(), self.exclude_var.get(), self.excluded_dirs_var.get(),
                self.duplicate_action_var.get(), self.config, self.state_dir)

    def _snapshot(self):
        if not self.folder_var.get().strip():
            raise ValueError("Choose a folder first.")
        selected = Path(self.folder_var.get()).expanduser().resolve()
        if not selected.is_dir():
            raise ValueError("The selected folder does not exist or is not a directory.")
        if selected == self.state_dir.resolve():
            raise ValueError("History/log storage must differ from the organization folder. Choose separate storage in Settings.")
        options = ScanOptions(
            recursive=self.recursive_var.get(), include_hidden=self.hidden_var.get(),
            excluded_dirs=comma_values(self.excluded_dirs_var.get()),
            include_extensions=comma_values(self.include_var.get()),
            exclude_extensions=comma_values(self.exclude_var.get()),
            destination_dirs=(*destination_dirs(selected, self.config), self.state_dir.resolve()),
            excluded_files=((self.config_path,) if self.config_path else ()) + (self.state_dir / "history.sqlite3",),
        )
        return selected, self.config, options, OperationType(self.mode_var.get()), self.duplicate_action_var.get()

    def _start_task(self, description, work, done, *, cancellable=False):
        if not self._idle():
            return False
        self.cancel_event = threading.Event()
        self.cancellable = cancellable
        self.progress.configure(value=0)
        self.status.set(description)
        def run():
            try:
                self.events.put(("done", done, work()))
            except Exception as error:
                self.events.put(("error", str(error)))
        self.worker = threading.Thread(target=run, name="SmartSort-desktop-worker", daemon=True)
        self._sync_controls()
        self.worker.start()
        return True

    def _drain_events(self):
        self.after_id = None
        try:
            for _ in range(250):
                event = self.events.get_nowait()
                if self.closing:
                    continue
                if event[0] in ("done", "error"):
                    self.worker = None
                    try:
                        if event[0] == "done":
                            event[1](event[2])
                        else:
                            self._error(event[1])
                    except Exception as error:
                        self._error(error)
                    finally:
                        self._sync_controls()
                elif event[0] == "progress":
                    index, total, result = event[1:]
                    self.progress.configure(value=100 * index / max(total, 1))
                    self.status.set(f"{index}/{total}: {result.item.source.name} · {result.status.value}")
                elif event[0] == "watch_result":
                    self.last_result = event[1]
                    self.watch_status.set(f"Monitoring active · latest batch: {result_summary(event[1])}")
                elif event[0] == "watch_error":
                    self.watch_status.set(f"Monitoring error: {event[1]}")
                    self.status.set(f"Monitoring error: {event[1]}. Inspect History before retrying.")
                elif event[0] == "watch_stopped":
                    self.watch = None
                    self.watch_status.set("Monitoring is off.")
                    self.status.set("Monitoring stopped.")
                    self._sync_controls()
        except queue.Empty:
            pass
        if not self.closing:
            self.after_id = self.root.after(60, self._drain_events)

    def choose_folder(self):
        if self._idle():
            path = filedialog.askdirectory(title="Choose a folder to organize", parent=self.root)
            if path:
                self.folder_var.set(path)

    def preview(self):
        if not self._idle():
            return
        try:
            snapshot = self._snapshot()
            fingerprint = self._fingerprint()
            self.current_plan = None
            self._start_task("Building a read-only preview…", lambda: plan_organization(*snapshot),
                             lambda plan: self._show_plan(plan, fingerprint, "organize"))
        except Exception as error:
            self._error(error)

    def _show_plan(self, plan, fingerprint, kind):
        if fingerprint != self._fingerprint():
            self.summary_var.set("Settings changed during preview. Preview again.")
            return
        self.current_plan = plan
        self._plan_fingerprint = fingerprint
        self.plan_kind = kind
        def rows():
            for index, item in enumerate(plan.items):
                yield str(index), (relative_label(item.source, plan.root), relative_label(item.destination, plan.root), item.reason, item.conflict)
        def done():
            self.summary_var.set(f"{len(plan.items)} planned operations · {len(plan.errors)} scan warnings. Review destinations before approval.")
            self.status.set("Preview ready. No files, history, configuration or operation logs were written.")
            if plan.errors:
                messagebox.showwarning("Scan warnings", "\n".join(plan.errors), parent=self.root)
            self.notebook.select(self.organize_tab)
        self._render_rows(self.plan_tree, rows(), done)

    def organize(self):
        if not self._idle():
            return
        if self.current_plan is None or self._plan_fingerprint != self._fingerprint():
            self._error("Preview the current settings before organizing.")
            return
        plan = self.current_plan
        if not plan.items:
            self.status.set("There are no planned operations to execute.")
            return
        title = "Approve quarantine plan" if self.plan_kind == "quarantine" else "Approve organization plan"
        if not messagebox.askyesno(title, f"Execute the displayed plan for {len(plan.items)} files?\n\nExisting files will not be overwritten. The executor rechecks each source and destination.", parent=self.root):
            self.status.set("Plan approval cancelled.")
            return
        state_dir = self.state_dir
        events = self.events
        # _start_task installs the cancellation Event before invoking this worker.
        def work():
            logger = configure_logging(state_dir)
            result = execute_plan(plan, HistoryStore(state_dir / "history.sqlite3"),
                                  progress=lambda index, total, item: events.put(("progress", index, total, item)),
                                  cancel=self.cancel_event)
            logger.info("Desktop session %s: %s", result.session_id, result.counts())
            return result
        self._start_task("Executing the approved plan…", work, self._show_result, cancellable=True)

    def _show_result(self, result):
        self.last_result = result
        self.current_plan = None
        self._plan_fingerprint = None
        self.progress.configure(value=100)
        self.summary_var.set(result_summary(result) + (f" · session {result.session_id}" if result.session_id else ""))
        for index, operation in enumerate(result.results):
            iid = str(index)
            if self.plan_tree.exists(iid):
                values = list(self.plan_tree.item(iid, "values"))
                values[-1] = operation.status.value + (f": {operation.error}" if operation.error else "")
                self.plan_tree.item(iid, values=values)
        self.status.set("Finished. Review the summary and History for any conflicts or failures.")
        if result.errors:
            messagebox.showwarning("Operation warnings", "\n".join(result.errors), parent=self.root)

    def cancel(self):
        self.cancel_event.set()
        self.status.set("Cancellation requested. The current file finishes before the next operation is skipped.")

    def scan_duplicates(self):
        if not self._idle():
            return
        try:
            selected, config, options, _, _ = self._snapshot()
            fingerprint = self._fingerprint()
            state_dir = self.state_dir
            def work():
                scanned = scan(selected, options)
                report = find_duplicates(scanned.files)
                HistoryStore(state_dir / "history.sqlite3").record_duplicate_report(len(report.groups))
                return report, scanned.errors
            self._start_task("Comparing duplicate candidates by size and SHA-256…", work,
                             lambda value: self._show_duplicates(value, selected, fingerprint))
        except Exception as error:
            self._error(error)

    def _show_duplicates(self, value, selected, fingerprint):
        if fingerprint != self._fingerprint():
            self.duplicate_summary.set("Settings changed during scan. Scan again.")
            return
        report, scan_errors = value
        self.duplicate_report = report
        self.duplicate_paths.clear()
        errors = (*scan_errors, *report.errors)
        def rows():
            for group_index, group in enumerate(report.groups, 1):
                for file_index, path in enumerate(group.files):
                    iid = f"{group_index}-{file_index}"
                    if file_index > 0:
                        self.duplicate_paths[iid] = path
                    yield iid, (group_index, relative_label(path, selected), f"{group.size:,} bytes", "Keeper" if file_index == 0 else "Extra copy")
        def done():
            self.duplicate_summary.set(f"{len(report.groups)} duplicate groups · {report.hashed_files} candidate files hashed · {len(errors)} warnings. No files changed.")
            self.status.set("Duplicate report ready. Select extra copies to preview quarantine.")
            if errors:
                messagebox.showwarning("Duplicate scan warnings", "\n".join(errors), parent=self.root)
        self._render_rows(self.duplicate_tree, rows(), done)

    def set_duplicate_action(self, action):
        if self._idle():
            self.duplicate_action_var.set(action)
            self.status.set(f"Organization duplicate policy: {action}. Preview again to apply it.")

    def preview_quarantine(self):
        if not self._idle():
            return
        paths = tuple(self.duplicate_paths[iid] for iid in self.duplicate_tree.selection() if iid in self.duplicate_paths)
        if not paths:
            self._error("Select at least one extra copy. Keeper files cannot be quarantined by this action.")
            return
        try:
            selected, config, options, _, _ = self._snapshot()
            fingerprint = self._fingerprint()
            self._start_task("Building a quarantine preview…", lambda: plan_organization(selected, config, options, OperationType.MOVE, "quarantine", only_paths=paths),
                             lambda plan: self._show_plan(plan, fingerprint, "quarantine"))
        except Exception as error:
            self._error(error)

    def _set_rules_document(self, document):
        self.rules_text.delete("1.0", "end")
        self.rules_text.insert("1.0", json.dumps(document, indent=2, ensure_ascii=False))

    def _rules_document(self):
        def unique_object(pairs):
            values = {}
            for key, value in pairs:
                if key in values:
                    raise ValueError(f"Duplicate JSON key: {key}")
                values[key] = value
            return values
        document = json.loads(self.rules_text.get("1.0", "end"), object_pairs_hook=unique_object)
        return document, validate_config(document)

    def load_rules(self):
        if not self._idle():
            return
        path = filedialog.askopenfilename(title="Load rule configuration", filetypes=(("JSON rules", "*.json"), ("All files", "*")), parent=self.root)
        if path:
            try:
                config = load_config(Path(path))
                self._set_rules_document(config_to_document(config))
                self.editor_path = Path(path)
                self.rule_status.set("Rules loaded into the editor. Apply or Save to use them.")
            except Exception as error:
                self._error(error)

    def default_rules(self):
        if self._idle():
            self._set_rules_document(default_config_document())
            self.editor_path = None
            self.rule_status.set("Defaults loaded into the editor. Apply or Save to use them.")

    def validate_rules(self):
        if self._idle():
            try:
                _, config = self._rules_document()
                self.rule_status.set(f"Valid version-1 configuration · {len(config.rules)} ordered rules.")
            except Exception as error:
                self._error(error)

    def apply_rules(self):
        if self._idle():
            try:
                _, self.config = self._rules_document()
                self.config_path = self.editor_path
                self._invalidate()
                self.rule_status.set(f"Applied {len(self.config.rules)} rules for this desktop session. No file was written.")
            except Exception as error:
                self._error(error)

    def save_rules(self):
        if not self._idle():
            return
        try:
            document, config = self._rules_document()
            filename = filedialog.asksaveasfilename(title="Save validated rules", defaultextension=".json", filetypes=(("JSON rules", "*.json"),), parent=self.root)
            if filename:
                save_config(Path(filename), document)
                self.config = config
                self.config_path = Path(filename)
                self.editor_path = self.config_path
                self._invalidate()
                self.rule_status.set(f"Saved and applied {len(config.rules)} rules.")
        except Exception as error:
            self._error(error)

    def _existing_store(self):
        path = self.state_dir / "history.sqlite3"
        return HistoryStore(path) if path.is_file() else None

    def refresh_history(self):
        if not self._idle():
            return
        try:
            store = self._existing_store()
            self._start_task("Reading previous sessions…", lambda: store.list_sessions() if store else [], self._show_history)
        except Exception as error:
            self._error(error)

    def _show_history(self, sessions):
        self.history_tree.delete(*self.history_tree.get_children())
        for session in sessions:
            identifier = str(session["id"])
            self.history_tree.insert("", "end", iid=identifier, values=(identifier, session.get("started_at", ""), session.get("mode", ""), session.get("status", ""), session.get("root", "")))
        self.status.set(f"{len(sessions)} recorded sessions.")

    def _selected_session(self):
        selected = self.history_tree.selection()
        if len(selected) != 1:
            raise ValueError("Select one history session.")
        store = self._existing_store()
        if store is None:
            raise ValueError("No history database exists in the selected storage folder.")
        return selected[0], store

    def view_session(self):
        if self._idle():
            try:
                identifier, store = self._selected_session()
                self._start_task("Reading session details…", lambda: store.get_session(identifier), self._show_session)
            except Exception as error:
                self._error(error)

    def _show_session(self, session):
        self.history_details.configure(state="normal")
        self.history_details.delete("1.0", "end")
        self.history_details.insert("1.0", json.dumps(session, indent=2, default=str))
        self.history_details.configure(state="disabled")
        self.status.set("Session details loaded.")

    def undo_session(self):
        self._history_action("undo")

    def recover_session(self):
        self._history_action("recover")

    def _history_action(self, action):
        if not self._idle():
            return
        try:
            identifier, store = self._selected_session()
            prompt = ("Undo eligible files with identity checks? Occupied paths and changed files will be reported as conflicts."
                      if action == "undo" else "Inspect the interrupted session without changing files or history? Recommendations explain any manual reconciliation needed.")
            if not messagebox.askyesno(f"Confirm {action}", prompt, parent=self.root):
                return
            state_dir = self.state_dir
            def work():
                logger = configure_logging(state_dir)
                value = store.undo_session(identifier) if action == "undo" else store.recover_session(identifier)
                logger.info("Desktop %s session %s", action, identifier)
                return value, store.list_sessions(), store.get_session(identifier)
            def done(payload):
                value, sessions, detail = payload
                if hasattr(value, "counts"):
                    self.last_result = value
                self._show_history(sessions)
                self._show_session(detail if action == "undo" else {"recovery": value, "session": detail})
                self.status.set(result_summary(value) if hasattr(value, "counts") else ("Recovery inspected. Preserve files and review the recommendations below." if value.get("manual_intervention") else "Recovery inspected. No pending entries need manual review."))
            self._start_task(f"Performing safe {action}…", work, done)
        except Exception as error:
            self._error(error)

    def choose_state_dir(self):
        if self._idle():
            path = filedialog.askdirectory(title="Choose history and log storage", parent=self.root)
            if path:
                self.state_dir = Path(path).resolve()
                self.state_var.set(str(self.state_dir))
                self._invalidate()
                self.history_tree.delete(*self.history_tree.get_children())
                self.statistics_var.set("Storage changed. Refresh statistics to read this folder's history.")
                self.status.set("Storage folder selected. Previous history remains in its original folder.")

    def refresh_statistics(self):
        if not self._idle():
            return
        try:
            store = self._existing_store()
            self._start_task("Reading history statistics…", lambda: store.statistics() if store else {}, self._show_statistics)
        except Exception as error:
            self._error(error)

    def _show_statistics(self, values):
        if not values:
            self.statistics_var.set("No operation history in this storage folder.")
        else:
            self.statistics_var.set(
                f"Successful files: {values.get('success_files', 0):,} · bytes: {values.get('success_bytes', 0):,} · sessions: {values.get('session_count', 0):,} · duplicate groups reported: {values.get('duplicate_groups', 0):,}\n"
                f"Categories: {json.dumps(values.get('by_category', {}), default=str)}\n"
                f"File types: {json.dumps(values.get('by_type', {}), default=str)}"
            )
        self.status.set("Statistics refreshed from recorded results.")

    def refresh_logs(self):
        if not self._idle():
            return
        state_dir = self.state_dir
        def work():
            path = state_dir / "smartsort.log"
            if not path.is_file():
                return "No operation log in this storage folder. Preview does not create one."
            with path.open("rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - 100_000))
                return stream.read().decode("utf-8", errors="replace")
        def done(text):
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.insert("1.0", text)
            self.log_text.configure(state="disabled")
            self.status.set("Latest operation log loaded (up to 100 KB).")
        self._start_task("Reading the operation log…", work, done)

    def start_watch(self):
        if not self._idle():
            return
        try:
            selected, config, options, mode, _ = self._snapshot()
            stable, poll = float(self.stable_var.get()), float(self.poll_var.get())
            if not math.isfinite(stable) or not math.isfinite(poll) or stable <= 0 or poll <= 0:
                raise ValueError("Monitoring intervals must be greater than zero.")
            if not messagebox.askyesno("Enable folder monitoring", f"Monitor {selected}?\n\nNew stable files will be organized with the current {mode.value} rules. Temporary downloads and destination folders are excluded. Stop monitoring before changing settings.", parent=self.root):
                return
            events = self.events
            state_dir = self.state_dir
            def work():
                from smartsort.services.watcher import WatchService
                cancel = self.cancel_event
                logger = configure_logging(state_dir)
                service = WatchService(selected, config, options, HistoryStore(state_dir / "history.sqlite3"), mode=mode,
                                       stable_seconds=stable, poll_seconds=poll,
                                       on_result=lambda result: events.put(("watch_result", result)),
                                       on_error=lambda error: (logger.error("Desktop monitoring: %s", error), events.put(("watch_error", error))))
                if not cancel.is_set():
                    service.start()
                if cancel.is_set():
                    service.stop()
                    service.join()
                else:
                    logger.info("Desktop monitoring enabled")
                return service
            def done(service):
                self.watch = service
                self.watch_status.set(f"Monitoring {selected} · {mode.value} · stable for {stable:g}s")
                self.status.set("Monitoring is explicitly enabled. Use Stop monitoring to end it.")
                def wait_for_stop():
                    service.join()
                    events.put(("watch_stopped",))
                threading.Thread(target=wait_for_stop, name="SmartSort-watch-lifecycle", daemon=True).start()
            self._start_task("Starting the approved folder monitor…", work, done)
        except Exception as error:
            self._error(error)

    def stop_watch(self):
        if self.watch is not None:
            self.watch.stop()
            self.watch_stop_button.configure(state="disabled")
            self.watch_status.set("Stopping monitoring after the current file…")

    def close(self):
        if self.closing:
            return
        self.closing = True
        self._cancel_render()
        self.cancel_event.set()
        if self.watch is not None:
            self.watch.stop()
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.status.set("Finishing the current file before closing…")
        self._sync_controls()
        self._finish_close()

    def _finish_close(self):
        if self.worker is not None and self.worker.is_alive():
            self.root.after(60, self._finish_close)
            return
        if self.watch is not None:
            try:
                self.watch.join(timeout=0)
            except TypeError:
                self.watch.join(0)
            if hasattr(self.watch, "is_alive") and self.watch.is_alive():
                self.root.after(60, self._finish_close)
                return
        self.root.destroy()


def main(config_path: Path | None = None, state_dir: Path | None = None, *, smoke_seconds: float | None = None) -> int:
    root = None
    try:
        root = tk.Tk()
        app = SmartSortApp(root, config_path=config_path, state_dir=state_dir)
        if smoke_seconds is not None:
            root.after(max(1, int(smoke_seconds * 1000)), app.close)
        root.mainloop()
        return 0
    except (tk.TclError, OSError, ValueError) as error:
        if root is not None:
            root.destroy()
        print(f"SmartSort desktop: {error}")
        return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Launch the SmartSort desktop application")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--smoke-seconds", type=float, help="Close automatically after a startup smoke check")
    arguments = parser.parse_args()
    raise SystemExit(main(arguments.config, arguments.state_dir, smoke_seconds=arguments.smoke_seconds))
