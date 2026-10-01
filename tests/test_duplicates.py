import hashlib
from pathlib import Path

from smartsort.core import duplicates
from smartsort.core.duplicates import find_duplicates
from smartsort.core.scanner import scan


def test_different_names_with_identical_contents(tmp_path):
    for name in ("report.pdf", "renamed.bin"):
        (tmp_path / name).write_bytes(b"same content")
    (tmp_path / "different.pdf").write_bytes(b"changed data")
    result = find_duplicates(scan(tmp_path).files)
    assert len(result.groups) == 1
    assert result.groups[0].files == (tmp_path / "renamed.bin", tmp_path / "report.pdf")
    assert result.groups[0].sha256 == hashlib.sha256(b"same content").hexdigest()
    assert result.groups[0].size == 12
    assert result.hashed_files == 3
    assert not result.errors


def test_hashes_only_shared_size_groups_even_for_large_files(tmp_path, monkeypatch):
    (tmp_path / "large.unique").write_bytes(b"x" * (4 * 1024 * 1024))
    (tmp_path / "a.txt").write_bytes(b"duplicate")
    (tmp_path / "b.bin").write_bytes(b"duplicate")
    paths = []
    original = duplicates._sha256
    def record(info):
        paths.append(info.path.name)
        return original(info)
    monkeypatch.setattr(duplicates, "_sha256", record)
    result = find_duplicates(scan(tmp_path).files)
    assert paths == ["a.txt", "b.bin"]
    assert result.hashed_files == 2 and len(result.groups) == 1


def test_unreadable_candidate_does_not_abort_other_hashes(tmp_path, monkeypatch):
    for name in ("a.txt", "blocked.txt", "c.txt"):
        (tmp_path / name).write_bytes(b"same")
    original = Path.open
    def inaccessible(path, *args, **kwargs):
        if path.name == "blocked.txt":
            raise PermissionError("simulated access denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", inaccessible)
    result = find_duplicates(scan(tmp_path).files)
    assert len(result.groups) == 1
    assert result.groups[0].files == (tmp_path / "a.txt", tmp_path / "c.txt")
    assert len(result.errors) == 1 and "blocked.txt" in result.errors[0]
    assert result.hashed_files == 2


def test_changed_or_missing_candidate_is_reported(tmp_path):
    (tmp_path / "a.txt").write_text("same")
    (tmp_path / "b.txt").write_text("same")
    candidates = scan(tmp_path).files
    (tmp_path / "a.txt").write_text("changed")
    result = find_duplicates(candidates)
    assert not result.groups and "changed since" in result.errors[0]
    (tmp_path / "a.txt").unlink()
    result = find_duplicates(candidates)
    assert not result.groups and result.errors


def test_duplicate_input_path_is_not_its_own_duplicate(tmp_path):
    (tmp_path / "a.txt").write_text("same")
    item = scan(tmp_path).files[0]
    result = find_duplicates((item, item))
    assert not result.groups and result.hashed_files == 0


def test_hashing_stream_detects_content_changes(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_bytes(b"same")
    candidates = scan(tmp_path).files
    original = Path.open
    class ChangingReader:
        def __init__(self, stream, path):
            self.stream = stream
            self.path = path
            self.changed = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, size):
            chunk = self.stream.read(size)
            if not self.changed:
                self.changed = True
                with original(self.path, "ab") as output:
                    output.write(b"changed")
            return chunk
    def change(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return ChangingReader(stream, path) if path.name == "a.txt" else stream
    monkeypatch.setattr(Path, "open", change)
    result = find_duplicates(candidates)
    assert not result.groups
    assert "changed while hashing" in result.errors[0]
