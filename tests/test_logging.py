import errno
import importlib
import logging
import os
from pathlib import Path
import subprocess

import pytest

from smartsort.utils import logging_utils
from smartsort.utils.logging_utils import LoggingSafetyError, configure_logging


@pytest.fixture(autouse=True)
def reset_app_logger():
    logger = logging.getLogger("smartsort")
    for handler in tuple(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()
    yield
    for handler in tuple(logger.handlers):
        if isinstance(handler, logging.FileHandler):
            logger.removeHandler(handler)
            handler.close()


def file_handler(logger):
    return next(handler for handler in logger.handlers if isinstance(handler, logging.FileHandler))


def link(source, destination):
    try:
        os.link(source, destination)
    except OSError as exc:
        pytest.skip(f"Hard links unavailable: {exc}")


def test_import_remains_pure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = tuple(tmp_path.iterdir())
    logger = logging.getLogger("smartsort")
    before_handlers = tuple(logger.handlers)
    importlib.reload(logging_utils)
    assert tuple(tmp_path.iterdir()) == before
    assert tuple(logger.handlers) == before_handlers


def test_same_storage_is_idempotent_and_changed_storage_closes_old_handler(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    logger = configure_logging(first)
    logger.info("first storage only")
    old = file_handler(logger)
    assert configure_logging(first) is logger
    assert sum(isinstance(handler, logging.FileHandler) for handler in logger.handlers) == 1
    configure_logging(second).info("second storage only")
    assert old._closed and sum(isinstance(handler, logging.FileHandler) for handler in logger.handlers) == 1
    assert "second storage only" not in (first / "smartsort.log").read_text()
    assert "first storage only" not in (second / "smartsort.log").read_text()


@pytest.mark.parametrize("suffix", ["", ".1", ".2"])
def test_existing_log_or_backup_hard_link_is_rejected_before_mutation(tmp_path, suffix):
    outside = tmp_path / "valuable.txt"
    outside.write_text("valuable original contents")
    state = tmp_path / "state"
    state.mkdir()
    alias = state / f"smartsort.log{suffix}"
    link(outside, alias)
    with pytest.raises(ValueError, match="Hard-link"):
        configure_logging(state)
    assert outside.read_text() == "valuable original contents"
    if suffix:
        assert not (state / "smartsort.log").exists()


@pytest.mark.parametrize("suffix", ["", ".1", ".2"])
def test_existing_log_or_backup_symlink_is_rejected(tmp_path, suffix):
    outside = tmp_path / "valuable.txt"
    outside.write_text("valuable original contents")
    state = tmp_path / "state"
    state.mkdir()
    try:
        (state / f"smartsort.log{suffix}").symlink_to(outside)
    except OSError:
        pytest.skip("Creating a symlink requires privileges")
    with pytest.raises(ValueError, match="Unsafe log|regular file"):
        configure_logging(state)
    assert outside.read_text() == "valuable original contents"


def test_directory_log_target_is_rejected_before_other_creation(tmp_path):
    state = tmp_path / "state"
    (state / "smartsort.log.1").mkdir(parents=True)
    with pytest.raises(ValueError, match="regular file"):
        configure_logging(state)
    assert not (state / "smartsort.log").exists()


def test_alias_added_to_open_log_blocks_next_append(tmp_path):
    state = tmp_path / "state"
    logger = configure_logging(state)
    logger.info("existing safe entry")
    target = state / "smartsort.log"
    link(target, tmp_path / "unexpected-alias.log")
    before = target.read_bytes()
    with pytest.raises(ValueError, match="Hard-link|alias"):
        logger.info("must not append")
    assert target.read_bytes() == before
    assert (tmp_path / "unexpected-alias.log").read_bytes() == before


def test_poisoned_backup_added_after_configuration_blocks_rollover(tmp_path):
    state = tmp_path / "state"
    logger = configure_logging(state)
    handler = file_handler(logger)
    handler.maxBytes = 1
    outside = tmp_path / "valuable.txt"
    outside.write_text("valuable original contents")
    link(outside, state / "smartsort.log.1")
    with pytest.raises(ValueError, match="Hard-link"):
        logger.info("must not rotate or append")
    assert outside.read_text() == "valuable original contents"
    assert (state / "smartsort.log").read_bytes() == b""


def test_destination_appearing_during_rotation_is_never_overwritten(tmp_path, monkeypatch):
    state = tmp_path / "state"
    logger = configure_logging(state)
    logger.info("initial safe record")
    handler = file_handler(logger)
    handler.maxBytes = 1
    outside = tmp_path / "valuable.txt"
    outside.write_text("valuable original contents")
    real_link = os.link
    def intervening_link(source, destination, **kwargs):
        if Path(destination).name == "smartsort.log.1":
            real_link(outside, destination)
        return real_link(source, destination, **kwargs)
    monkeypatch.setattr(logging_utils.os, "link", intervening_link)
    with pytest.raises(ValueError, match="exclusively"):
        logger.info("must not overwrite")
    assert outside.read_text() == "valuable original contents"
    assert "initial safe record" in (state / "smartsort.log").read_text()


@pytest.mark.parametrize("force_copy", [False, True])
def test_normal_rotation_keeps_two_independent_backups(tmp_path, monkeypatch, force_copy):
    logger = configure_logging(tmp_path / "state")
    file_handler(logger).maxBytes = 1
    if force_copy:
        def unavailable(*args, **kwargs):
            raise OSError(errno.EXDEV, "hard link unsupported")
        monkeypatch.setattr(logging_utils.os, "link", unavailable)
    for index in range(5):
        logger.info("record-%s", index)
    state = tmp_path / "state"
    assert "record-4" in (state / "smartsort.log").read_text()
    assert "record-3" in (state / "smartsort.log.1").read_text()
    assert "record-2" in (state / "smartsort.log.2").read_text()
    assert all((state / name).lstat().st_nlink == 1 for name in ("smartsort.log", "smartsort.log.1", "smartsort.log.2"))


def test_windows_junction_ancestor_is_rejected(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows-specific reparse-point test")
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = tmp_path / "state"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    if result.returncode:
        pytest.skip("Junction creation unavailable")
    try:
        with pytest.raises(ValueError, match="Unsafe log"):
            configure_logging(junction)
        assert not (outside / "smartsort.log").exists()
    finally:
        os.rmdir(junction)
