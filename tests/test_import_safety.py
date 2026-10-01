"""Every application module can be embedded without running a workflow."""
import os
from pathlib import Path
import subprocess
import sys


def test_all_package_modules_import_without_state_or_argument_parsing(tmp_path):
    environment = os.environ.copy()
    environment["SMARTSORT_DATA_DIR"] = str(tmp_path / "absent-state")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    script = """
import importlib, pkgutil, sys
sys.argv = ['embedding-host', '--unrelated-argument']
import smartsort
for module in pkgutil.walk_packages(smartsort.__path__, smartsort.__name__ + '.'):
    importlib.import_module(module.name)
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=environment,
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""
    assert list(tmp_path.iterdir()) == []
