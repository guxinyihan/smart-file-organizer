import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, cwd=None):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run([sys.executable, "-m", "smartsort", *map(str, args)], cwd=cwd or ROOT,
                          env=environment, text=True, input="", capture_output=True, timeout=30)


def test_legacy_import_is_safe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["unrelated", "--bad-argument"])
    spec = importlib.util.spec_from_file_location("legacy_organizer", ROOT / "FileOrganizer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert list(tmp_path.iterdir()) == []


def test_preview_has_no_persistent_side_effects(tmp_path):
    source, state = tmp_path / "inbox", tmp_path / "state"
    source.mkdir()
    (source / "first.unknown").write_text("unknown")
    (source / "report.pdf").write_text("pdf")
    result = run_cli("preview", source, "--state-dir", state, "--json")
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert len(plan["items"]) == 2
    assert not state.exists()
    assert sorted(p.name for p in source.iterdir()) == ["first.unknown", "report.pdf"]


def test_noninteractive_execution_requires_explicit_approval(tmp_path):
    (tmp_path / "note.txt").write_text("test")
    result = run_cli("organize", tmp_path, "--state-dir", tmp_path / "state")
    assert result.returncode == 1
    assert "--yes" in result.stderr
    assert (tmp_path / "note.txt").exists()
    assert not (tmp_path / "state").exists()


def test_complete_cli_copy_history_undo_workflow(tmp_path):
    root, state = tmp_path / "inbox", tmp_path / "state"
    root.mkdir()
    (root / "note.txt").write_text("test")
    result = run_cli("organize", root, "--copy", "--yes", "--state-dir", state)
    assert result.returncode == 0, result.stderr
    assert (root / "Documents" / "note.txt").read_text() == "test"
    sessions = json.loads(run_cli("history", "--state-dir", state).stdout)
    assert len(sessions) == 1
    result = run_cli("undo", sessions[0]["id"], "--yes", "--state-dir", state)
    assert result.returncode == 0, result.stderr
    assert not (root / "Documents" / "note.txt").exists()
    assert (root / "note.txt").read_text() == "test"


def test_help_and_rules_validation():
    assert run_cli("--help").returncode == 0
    assert run_cli("rules", "validate").returncode == 0


def test_invalid_json_returns_useful_error(tmp_path):
    config = tmp_path / "bad.json"
    config.write_text("{broken")
    result = run_cli("rules", "validate", "--config", config)
    assert result.returncode == 1
    assert "SmartSort:" in result.stderr


def test_history_reads_do_not_create_database(tmp_path):
    state = tmp_path / "absent"
    assert run_cli("history", "--state-dir", state).returncode == 0
    assert run_cli("stats", "--state-dir", state).returncode == 0
    assert not state.exists()


def test_json_execution_is_one_document(tmp_path):
    root, state = tmp_path / "inbox", tmp_path / "state"
    root.mkdir()
    (root / "note.txt").write_text("test")
    result = run_cli("organize", root, "--yes", "--json", "--state-dir", state)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["results"][0]["status"] == "succeeded"


def test_loaded_configuration_is_not_sorted(tmp_path):
    config = tmp_path / "rules.json"
    config.write_text(json.dumps({".txt": "Documents"}))
    (tmp_path / "note.txt").write_text("test")
    result = run_cli("preview", tmp_path, "--config", config, "--json", "--state-dir", tmp_path / "state")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [Path(item["source"]).name for item in payload["items"]] == ["note.txt"]


def test_runtime_storage_cannot_equal_selected_root(tmp_path):
    (tmp_path / "smartsort.log").write_text("existing activity")
    result = run_cli("preview", tmp_path, "--state-dir", tmp_path)
    assert result.returncode == 1
    assert "storage must differ" in result.stderr
    assert [p.name for p in tmp_path.iterdir()] == ["smartsort.log"]
