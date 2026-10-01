import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartsort.core import scanner
from smartsort.core.models import ScanOptions
from smartsort.core.scanner import scan


def write(root, relative, contents="file"):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    return target


def relative(result, root):
    return [info.path.relative_to(root).as_posix() for info in result.files]


def test_top_level_recursive_and_stable_order(tmp_path):
    write(tmp_path, "z.txt")
    write(tmp_path, "A.txt")
    write(tmp_path, "nested/report.pdf")
    assert relative(scan(tmp_path), tmp_path) == ["A.txt", "z.txt"]
    result = scan(tmp_path, ScanOptions(recursive=True))
    assert relative(result, tmp_path) == ["A.txt", "nested/report.pdf", "z.txt"]
    assert not result.errors
    assert result.files[0].identity.size == 4


def test_prunes_excluded_hidden_internal_destination_and_state_trees(tmp_path, monkeypatch):
    for path in (
        "keep/report.txt", "exclude/deeper/ignored.txt", "keep/exclude/ignored.txt",
        ".hidden/child/ignored.txt", ".git/objects/ignored.txt", ".venv/lib/ignored.txt",
        "node_modules/pkg/ignored.txt", "Documents/PDF/ignored.pdf", "runtime/ignored.db",
        ".secret.txt", ".smartsort.lock",
    ):
        write(tmp_path, path)
    monkeypatch.setenv("SMARTSORT_DATA_DIR", str(tmp_path / "runtime"))
    visited = []
    original = scanner.os.scandir
    def record_scandir(path):
        visited.append(Path(path).relative_to(tmp_path).as_posix())
        return original(path)
    monkeypatch.setattr(scanner.os, "scandir", record_scandir)
    result = scan(tmp_path, ScanOptions(
        recursive=True, excluded_dirs=("exclude",),
        destination_dirs=(tmp_path / "Documents" / "PDF",),
    ))
    assert relative(result, tmp_path) == ["keep/report.txt"]
    assert set(visited) == {".", "Documents", "keep"}
    assert not result.errors


def test_include_hidden_keeps_internal_and_state_pruning(tmp_path, monkeypatch):
    write(tmp_path, ".hidden/deeper/visible.txt")
    write(tmp_path, ".visible.txt")
    write(tmp_path, ".git/ignored.txt")
    write(tmp_path, "state/history.db")
    monkeypatch.setenv("SMARTSORT_DATA_DIR", str(tmp_path / "state"))
    assert relative(scan(tmp_path, ScanOptions(recursive=True, include_hidden=True)), tmp_path) == [
        ".hidden/deeper/visible.txt", ".visible.txt",
    ]


def test_extension_filters_normalize_case_and_dots(tmp_path):
    for name in ("a.TXT", "b.pdf", "c.tar.gz", "extensionless", ".env"):
        write(tmp_path, name)
    options = ScanOptions(include_hidden=True, include_extensions=(" TXT ", "tar.gz", ".env"))
    assert relative(scan(tmp_path, options), tmp_path) == [".env", "a.TXT", "c.tar.gz"]
    options = ScanOptions(include_extensions=("txt", "pdf"), exclude_extensions=(".TXT",))
    assert relative(scan(tmp_path, options), tmp_path) == ["b.pdf"]
    assert relative(scan(tmp_path, ScanOptions(include_extensions=("",))), tmp_path) == ["extensionless"]


def test_relative_excluded_path_only_prunes_that_subtree(tmp_path):
    write(tmp_path, "a/cache/ignored.txt")
    write(tmp_path, "b/cache/kept.txt")
    result = scan(tmp_path, ScanOptions(recursive=True, excluded_dirs=("a/cache",)))
    assert relative(result, tmp_path) == ["b/cache/kept.txt"]


def test_explicit_config_history_and_other_files_are_excluded_by_path(tmp_path):
    configuration = write(tmp_path, "rules.json")
    journal = write(tmp_path, "journal/history.sqlite3")
    write(tmp_path, "nested/rules.json")
    write(tmp_path, "ordinary.txt")
    result = scan(tmp_path, ScanOptions(
        recursive=True, excluded_files=(configuration, journal),
    ))
    assert relative(result, tmp_path) == ["nested/rules.json", "ordinary.txt"]
    assert not result.errors
    assert configuration.read_text() == journal.read_text() == "file"


def test_explicit_file_exclusion_resolves_relative_paths_without_writes(tmp_path, monkeypatch):
    write(tmp_path, "rules.json")
    write(tmp_path, "document.txt")
    monkeypatch.chdir(tmp_path)
    result = scan(tmp_path, ScanOptions(excluded_files=(Path("rules.json"),)))
    assert relative(result, tmp_path) == ["document.txt"]
    assert not result.errors


def test_per_directory_failure_keeps_other_results(tmp_path, monkeypatch):
    write(tmp_path, "blocked/a.txt")
    write(tmp_path, "good/b.txt")
    original = scanner.os.scandir
    def inaccessible(path):
        if Path(path).name == "blocked":
            raise PermissionError("simulated access denied")
        return original(path)
    monkeypatch.setattr(scanner.os, "scandir", inaccessible)
    result = scan(tmp_path, ScanOptions(recursive=True))
    assert relative(result, tmp_path) == ["good/b.txt"]
    assert len(result.errors) == 1
    assert "blocked" in result.errors[0] and "denied" in result.errors[0]


def test_symlink_files_directories_cycles_and_root_are_skipped(tmp_path):
    actual = write(tmp_path, "actual/file.txt")
    try:
        (tmp_path / "file-link.txt").symlink_to(actual)
        (tmp_path / "dir-link").symlink_to(actual.parent, target_is_directory=True)
        (actual.parent / "cycle").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires unavailable privileges")
    assert relative(scan(tmp_path, ScanOptions(recursive=True)), tmp_path) == ["actual/file.txt"]
    result = scan(tmp_path / "dir-link")
    assert not result.files and result.errors
    result = scan(tmp_path / "dir-link" / "nested")
    assert not result.files and result.errors


def test_windows_junctions_and_junction_ancestors_are_not_followed(tmp_path):
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows junction test")
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    write(outside, "external.txt")
    write(root, "local.txt")
    junction = root / "linked"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, text=True,
    )
    if created.returncode:
        pytest.skip("Junction creation unavailable")
    result = scan(root, ScanOptions(recursive=True))
    assert relative(result, root) == ["local.txt"]
    assert not result.errors
    assert scan(junction).errors
    assert scan(junction / "nested").errors


def test_missing_or_file_root_errors(tmp_path):
    for root in (tmp_path / "missing", write(tmp_path, "file.txt")):
        result = scan(root)
        assert result.errors and not result.files


def test_state_root_is_not_scanned(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTSORT_DATA_DIR", str(tmp_path))
    write(tmp_path, "history.db")
    result = scan(tmp_path, ScanOptions(include_hidden=True))
    assert not result.files and "state directory" in result.errors[0]


def test_windows_hidden_system_and_reparse_attributes():
    import stat
    from smartsort.core.rules import is_linklike
    assert scanner._hidden("ordinary.txt", SimpleNamespace(st_file_attributes=0x2))
    assert scanner._hidden("ordinary.txt", SimpleNamespace(st_file_attributes=0x4))
    assert not scanner._hidden("ordinary.txt", SimpleNamespace(st_file_attributes=0))
    assert is_linklike(SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400))


def test_unix_ctime_is_not_used_as_birth_time(monkeypatch):
    monkeypatch.setattr(scanner.os, "name", "posix")
    assert scanner._birth_time(SimpleNamespace(st_ctime=100)) is None
    assert scanner._birth_time(SimpleNamespace(st_ctime=100, st_birthtime=50)) == 50
