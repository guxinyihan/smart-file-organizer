from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from smartsort.config.manager import (
    ConfigError, config_to_document, default_config_document, default_state_dir,
    load_config, save_config, validate_config,
)
from smartsort.core.models import FileIdentity, FileInfo
from smartsort.core.rules import destination_dirs, resolve_destination, select_destination


def config(*rules, **options):
    return validate_config({"version": 1, "rules": list(rules), **options})


def info(name="report.pdf", size=10, modified=0, created=None):
    return FileInfo(Path(name), FileIdentity(size, 0, 0, 0), modified, created)


def test_packaged_upstream_defaults_independent_of_cwd(tmp_path, monkeypatch):
    upstream = Path(__file__).parents[1] / "tests" / "fixtures" / "upstream_extensions.json"
    mapping = json.loads(upstream.read_text())
    monkeypatch.chdir(tmp_path)
    settings = load_config()
    for extension, destination in mapping.items():
        assert select_destination(info("file" + extension), settings)[0] == destination
    assert select_destination(info("unknown.unmapped"), settings)[0] == "Others"
    assert list(tmp_path.iterdir()) == []


def test_legacy_flat_mapping_and_first_rule_priority():
    legacy = validate_config({".PDF": "Documents", ".txt": "Documents"})
    assert legacy.rules[0].extensions == (".pdf", ".txt")
    settings = config(
        {"name": "Research", "extensions": ["PDF"], "pattern": "research-*", "destination": "Documents/Research"},
        {"name": "All PDFs", "extensions": [".pdf"], "destination": "Documents"},
    )
    assert select_destination(info("research-paper.PDF"), settings) == ("Documents/Research", "Research")
    assert select_destination(info("other.pdf"), settings) == ("Documents", "All PDFs")


def test_size_date_and_creation_predicates():
    timestamp = datetime(2024, 6, 1, tzinfo=timezone.utc).timestamp()
    settings = config({
        "destination": "Archive", "min_size": 10, "max_size": 20,
        "modified_after": "2024-01-01", "modified_before": "2025-01-01T00:00:00Z",
        "modified_older_than_days": 2, "created_before": "2025-01-01",
    })
    assert select_destination(info(size=10, modified=timestamp, created=timestamp), settings, timestamp + 3 * 86400)[0] == "Archive"
    for sample in (
        info(size=9, modified=timestamp, created=timestamp),
        info(size=21, modified=timestamp, created=timestamp),
        info(size=10, modified=timestamp, created=None),
        info(size=10, modified=0, created=timestamp),
    ):
        assert select_destination(sample, settings, timestamp + 3 * 86400)[0] == "Others"
    assert select_destination(info(size=10, modified=timestamp, created=timestamp), settings, timestamp + 86400)[0] == "Others"
    assert validate_config(config_to_document(settings)) == settings


def test_creation_after_and_older_than_days():
    settings = config({"destination": "Old", "created_after": "1970-01-01", "created_older_than_days": 1})
    assert select_destination(info(created=100), settings, 200000)[0] == "Old"
    assert select_destination(info(created=None), settings, 200000)[0] == "Others"
    assert select_destination(info(created=190000), settings, 200000)[0] == "Others"


@pytest.mark.parametrize("document", [
    [], {"version": 2, "rules": []}, {"version": True, "rules": []},
    {"version": 1, "rules": {}}, {"version": 1, "rules": [], "bogus": 1},
    {1: "Documents"}, {".pdf": "../outside"}, {"version": 1, "rules": [None]},
])
def test_invalid_config_document(document):
    with pytest.raises(ConfigError):
        validate_config(document)


@pytest.mark.parametrize("predicate", [
    {"unknown": 1}, {"extensions": ".txt"}, {"extensions": [None]},
    {"extensions": ["../txt"]}, {"pattern": "dir/*.txt"}, {"pattern": 1},
    {"min_size": True}, {"max_size": -1}, {"min_size": 20, "max_size": 10},
    {"modified_before": 123}, {"created_before": "invalid"},
    {"modified_older_than_days": float("nan")}, {"created_older_than_days": float("inf")},
    {"modified_older_than_days": True}, {"created_older_than_days": -1},
    {"created_older_than_days": 10 ** 1000}, {1: 2},
    {"modified_before": "2024-01-01", "modified_after": "2025-01-01"},
])
def test_invalid_predicates(predicate):
    with pytest.raises(ConfigError):
        config({"destination": "Documents", **predicate})


@pytest.mark.parametrize("category", [
    "", ".", "..", "../outside", "Docs/../../outside", "/tmp/outside",
    "C:/outside", "C:outside", "\\\\server\\share", "Docs\\PDF", "Docs//PDF",
    "Docs/../PDF", ".git", ".venv/files", ".smartsort-state", "CON", "NUL.txt",
    "COM1", "Docs/Bad.", "Docs/Bad ", "Docs/A:B", "Docs/*.pdf",
])
def test_destination_rejection_on_all_platforms(tmp_path, category):
    with pytest.raises(ValueError):
        resolve_destination(tmp_path, category)
    with pytest.raises(ConfigError):
        config({"destination": category})


def test_nested_destinations_resolve_without_creating_folders(tmp_path):
    settings = config({"destination": "Documents/PDF"}, fallback="Others", duplicates_folder="Quarantine")
    assert resolve_destination(tmp_path, "Documents/PDF") == tmp_path / "Documents" / "PDF"
    assert destination_dirs(tmp_path, settings) == (tmp_path / "Documents" / "PDF", tmp_path / "Others", tmp_path / "Quarantine")
    assert list(tmp_path.iterdir()) == []


def test_destination_file_as_parent_is_rejected(tmp_path):
    (tmp_path / "Documents").write_text("occupied")
    with pytest.raises(ValueError):
        resolve_destination(tmp_path, "Documents/PDF")


def test_destination_link_parent_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "Documents"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires unavailable privileges")
    with pytest.raises(ValueError):
        resolve_destination(tmp_path, "Documents/PDF")
    with pytest.raises(ValueError):
        resolve_destination(link, "PDF")


def test_windows_junction_destination_and_root_rejected(tmp_path):
    import os
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows junction test")
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "Documents"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True, text=True,
    )
    if created.returncode:
        pytest.skip("Junction creation unavailable")
    with pytest.raises(ValueError, match="reparse"):
        resolve_destination(root, "Documents/PDF")
    with pytest.raises(ValueError, match="reparse"):
        resolve_destination(link, "PDF")


def test_config_load_save_and_atomic_failure(tmp_path, monkeypatch):
    from smartsort.config import manager
    target = tmp_path / "nested" / "rules.json"
    original = {"version": 1, "rules": [{"destination": "Documents", "extensions": ["txt"]}]}
    save_config(target, original)
    assert load_config(target).rules[0].extensions == (".txt",)
    before = target.read_bytes()
    def fail_replace(*args):
        raise OSError("simulated persistence failure")
    monkeypatch.setattr(manager.os, "replace", fail_replace)
    with pytest.raises(OSError, match="persistence"):
        save_config(target, {"version": 1, "rules": []})
    assert target.read_bytes() == before
    assert list(target.parent.iterdir()) == [target]


def test_invalid_save_has_no_side_effects(tmp_path):
    target = tmp_path / "new" / "rules.json"
    with pytest.raises(ConfigError):
        save_config(target, {"version": 100, "rules": []})
    assert not target.parent.exists()


@pytest.mark.parametrize("payload", ['{"version":', '{"version":1,"version":2,"rules":[]}'])
def test_malformed_and_duplicate_json_keys(tmp_path, payload):
    target = tmp_path / "rules.json"
    target.write_text(payload)
    with pytest.raises(ConfigError):
        load_config(target)


def test_state_location_is_explicit_and_non_creating(tmp_path, monkeypatch):
    target = tmp_path / "state"
    monkeypatch.setenv("SMARTSORT_DATA_DIR", str(target))
    assert default_state_dir() == target
    assert not target.exists()
    assert default_config_document()["version"] == 1
