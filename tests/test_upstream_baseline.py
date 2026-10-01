"""Characterize the unsafe original script, never the SmartSort implementation.

The snapshots are copied from upstream commit
64faf6652918fc5f01b830849f7b88c43e43d944. Every subprocess, config,
log, move, copy and undo stays under pytest's disposable ``tmp_path``.
Passing tests here mean an upstream defect was reproduced, not that it is safe.
"""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


class UpstreamSandbox:
    def __init__(self, root):
        self.root = root.resolve()
        self.tool = self.root / "upstream-tool"
        self.target = self.root / "selected"
        self.cwd = self.root / "working-directory"
        for directory in (self.tool, self.target, self.cwd):
            directory.mkdir()
        self.script = self.tool / "FileOrganizer.py"
        shutil.copyfile(FIXTURES / "upstream_script.txt", self.script)
        shutil.copyfile(FIXTURES / "upstream_extensions.json", self.config)

    @property
    def config(self):
        return self.tool / "extension.json"

    @property
    def undo_record(self):
        return self.tool / "undo.json"

    def rules(self, mapping):
        self.config.write_text(json.dumps(mapping), encoding="utf-8")

    def file(self, relative, content="original"):
        path = self.target / relative
        assert path.resolve().is_relative_to(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run(self, *arguments, input_text="", target=None, runner=None):
        selected = (target or self.target).resolve()
        assert selected.is_relative_to(self.root)
        entry = runner or self.script
        assert entry.resolve().is_relative_to(self.root)
        return subprocess.run(
            [sys.executable, str(entry), str(selected), *arguments],
            cwd=self.cwd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )


@pytest.fixture
def upstream(tmp_path):
    return UpstreamSandbox(tmp_path)


def test_upstream_characterization_dry_run_creates_persistent_log(upstream):
    source = upstream.file("notes.txt")
    result = upstream.run("--dry-mode")
    assert result.returncode == 0
    assert "Will move" in result.stderr
    assert source.exists()
    assert not (upstream.target / "Documents").exists()
    assert not upstream.undo_record.exists()
    assert (upstream.tool / "organizer.log").read_text(encoding="utf-8")


def test_upstream_characterization_unknown_first_file_is_omitted_and_config_written_to_cwd(upstream):
    source = upstream.file("sample.unknownbaseline")
    original_config = upstream.config.read_bytes()
    result = upstream.run(input_text="Others\n")
    assert result.returncode == 0
    assert "Unknown Extension Found" in result.stderr
    assert "No File Found" in result.stderr
    assert source.exists()
    assert not (upstream.target / "Others").exists()
    assert upstream.config.read_bytes() == original_config
    written = json.loads((upstream.cwd / "extension.json").read_text(encoding="utf-8"))
    assert written[".unknownbaseline"] == "Others"
    # The next process still loads the unmodified script-relative config.
    second = upstream.run(input_text="Others\n")
    assert "Unknown Extension Found" in second.stderr
    assert source.exists()


@pytest.mark.parametrize("parent", [".git", "node_modules", ".hidden", "Documents"])
def test_upstream_characterization_excluded_and_hidden_parents_are_not_pruned(upstream, parent):
    source = upstream.file(f"{parent}/nested/payload.txt")
    result = upstream.run("--dry-mode")
    assert result.returncode == 0
    assert str(source) in result.stderr
    assert "Will move" in result.stderr


def test_upstream_characterization_preview_does_not_reserve_batch_names(upstream):
    upstream.file("first/report.pdf", "one")
    upstream.file("second/report.pdf", "two")
    result = upstream.run("--dry-mode")
    destination = upstream.target / "Documents" / "report.pdf"
    assert result.returncode == 0
    assert result.stderr.count(f"-> {destination}") == 2
    assert not destination.exists()


def test_upstream_characterization_nested_destination_fails_entire_run(upstream):
    upstream.rules({".txt": "Documents/Nested"})
    source = upstream.file("notes.txt")
    result = upstream.run()
    assert result.returncode == 1
    assert source.exists()
    assert not (upstream.target / "Documents").exists()
    assert not upstream.undo_record.exists()


def test_upstream_characterization_partial_failure_leaves_success_without_undo(upstream):
    first = upstream.file("01-succeeds.txt")
    second = upstream.file("02-fails.txt")
    third = upstream.file("03-never-attempted.txt")
    # Deterministic fault injection avoids OS/user-specific permission behavior.
    # The original move implementation is used for every other file.
    runner = upstream.tool / "fault_runner.py"
    runner.write_text(
        "import pathlib, runpy, shutil\n"
        "original_rglob = pathlib.Path.rglob\n"
        "pathlib.Path.rglob = lambda self, pattern: iter(sorted(original_rglob(self, pattern)))\n"
        "original_move = shutil.move\n"
        "def failing_move(source, destination):\n"
        "    if pathlib.Path(source).name == '02-fails.txt':\n"
        "        raise PermissionError('injected baseline operation failure')\n"
        "    return original_move(source, destination)\n"
        "shutil.move = failing_move\n"
        "runpy.run_path(str(pathlib.Path(__file__).with_name('FileOrganizer.py')), run_name='__main__')\n",
        encoding="utf-8",
    )
    result = upstream.run(runner=runner)
    assert result.returncode == 1
    assert "injected baseline operation failure" in result.stderr
    assert not first.exists()
    assert (upstream.target / "Documents" / first.name).exists()
    assert second.exists()
    assert third.exists()
    assert not upstream.undo_record.exists()


def test_upstream_characterization_move_undo_overwrites_occupied_source(upstream):
    source = upstream.file("report.txt", "organized content")
    assert upstream.run().returncode == 0
    assert upstream.undo_record.exists()
    source.write_text("replacement content", encoding="utf-8")
    result = upstream.run("--undo")
    assert result.returncode == 0
    assert source.read_text(encoding="utf-8") == "organized content"
    assert not (upstream.target / "Documents" / source.name).exists()


def test_upstream_characterization_copy_undo_deletes_changed_copy(upstream):
    source = upstream.file("report.txt", "source content")
    assert upstream.run("--copy").returncode == 0
    destination = upstream.target / "Documents" / source.name
    destination.write_text("new user changes", encoding="utf-8")
    result = upstream.run("--undo")
    assert result.returncode == 0
    assert source.read_text(encoding="utf-8") == "source content"
    assert not destination.exists()


@pytest.mark.parametrize("absolute", [False, True], ids=["parent-traversal", "absolute"])
def test_upstream_characterization_rule_can_escape_selected_root_inside_sandbox(upstream, absolute):
    outside_selected = upstream.root / "escaped-category"
    assert outside_selected.is_relative_to(upstream.root)
    assert not outside_selected.is_relative_to(upstream.target)
    category = str(outside_selected) if absolute else "../escaped-category"
    upstream.rules({".txt": category})
    source = upstream.file("report.txt")
    result = upstream.run()
    assert result.returncode == 0
    assert not source.exists()
    assert (outside_selected / source.name).exists()


def test_upstream_characterization_missing_target_reports_failure(upstream):
    result = upstream.run(target=upstream.root / "does-not-exist")
    assert result.returncode == 1
    assert "Organization Stopped" in result.stderr


def test_upstream_characterization_missing_config_reports_failure(upstream):
    upstream.config.unlink()
    result = upstream.run("--dry-mode")
    assert result.returncode == 1
    assert "extension.json" in result.stderr


def test_upstream_characterization_invalid_config_reports_failure(upstream):
    upstream.config.write_text("{invalid", encoding="utf-8")
    result = upstream.run("--dry-mode")
    assert result.returncode == 1
    assert "ERROR" in result.stderr


def test_upstream_characterization_import_parses_cli_and_creates_log(upstream):
    runner = upstream.tool / "import_runner.py"
    runner.write_text(
        "import importlib.util, pathlib, sys\n"
        "sys.argv = ['embedding-program', '--help']\n"
        "spec = importlib.util.spec_from_file_location('upstream_example', pathlib.Path(__file__).with_name('FileOrganizer.py'))\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print('IMPORT_COMPLETED')\n",
        encoding="utf-8",
    )
    result = upstream.run(runner=runner)
    assert result.returncode == 0
    assert "--dry-mode" in result.stdout
    assert "IMPORT_COMPLETED" not in result.stdout
    assert (upstream.tool / "organizer.log").exists()


def test_upstream_snapshots_have_documented_immutable_hashes():
    expected = {
        "upstream_script.txt": "0830d61bbaa647ef7fd223d8ccb6689a174d2cec635959106a92a1b5e4d446ae",
        "upstream_extensions.json": "1180637d2bdff8d875c34713fe046f2beb7b2e09b9556d4f1e394c6723b903c7",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest
