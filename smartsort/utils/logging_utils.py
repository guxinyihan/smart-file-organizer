"""Explicit rotating logs with link, reparse-point, and hard-link safeguards."""
from __future__ import annotations

import errno
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import stat
import sys
import threading

from smartsort.core.filesystem import absolute, check_ancestors, is_link


class LoggingSafetyError(ValueError):
    pass


_configuration_lock = threading.RLock()


def _regular(path: Path, *, missing_ok: bool = True) -> os.stat_result | None:
    try:
        check_ancestors(path)
        metadata = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise LoggingSafetyError(f"Log file disappeared: {path}")
    except (OSError, ValueError) as exc:
        raise LoggingSafetyError(f"Unsafe log path: {path}: {exc}") from exc
    if is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise LoggingSafetyError(f"Log target must be a regular file without links or reparse points: {path}")
    if metadata.st_nlink != 1:
        raise LoggingSafetyError(f"Hard-link aliases are not allowed for log files: {path}")
    return metadata


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_file(left, right) and (left.st_size, left.st_mtime_ns) == (right.st_size, right.st_mtime_ns)


class _SafeRotatingFileHandler(RotatingFileHandler):
    def __init__(self, target: Path):
        super().__init__(target, maxBytes=1_000_000, backupCount=2, encoding="utf-8", delay=True)

    @property
    def target(self) -> Path:
        return Path(self.baseFilename)

    def _check_family(self) -> None:
        _regular(self.target)
        for number in range(1, self.backupCount + 1):
            _regular(Path(f"{self.baseFilename}.{number}"))

    def _check_stream(self) -> None:
        if self.stream is None:
            return
        current = _regular(self.target, missing_ok=False)
        opened = os.fstat(self.stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or is_link(opened) or opened.st_nlink != 1
                or not _same_file(opened, current)):
            raise LoggingSafetyError("Active log target changed or acquired a hard-link alias; logging stopped")

    def _open(self):
        self._check_family()
        before = _regular(self.target)
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if before is None:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(self.target, flags, 0o600)
        try:
            after = _regular(self.target, missing_ok=False)
            opened = os.fstat(fd)
            if (opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode) or is_link(opened)
                    or not _same_file(opened, after) or before is not None and not _same_file(before, opened)):
                raise LoggingSafetyError("Log target changed while opening; no log record was appended")
            return os.fdopen(fd, "a", encoding=self.encoding, errors=self.errors)
        except Exception:
            os.close(fd)
            raise

    def emit(self, record) -> None:
        if self._closed:
            raise LoggingSafetyError("This log handler was closed after the storage directory changed")
        self._check_family()
        if self.stream is None:
            self.stream = self._open()
        self._check_stream()
        if self.shouldRollover(record):
            self.doRollover()
        self._check_family()
        self._check_stream()
        logging.FileHandler.emit(self, record)
        self._check_family()
        self._check_stream()

    def handleError(self, record) -> None:
        exception = sys.exc_info()[1]
        raise LoggingSafetyError(f"Unable to write SmartSort log: {exception}") from exception

    @staticmethod
    def _rotate_file(source: Path, destination: Path) -> None:
        """Transfer backups exclusively; a new destination is never overwritten."""
        before = _regular(source, missing_ok=False)
        if _regular(destination) is not None:
            raise LoggingSafetyError("A rotated log destination appeared unexpectedly; rotation stopped")
        try:
            os.link(source, destination, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.ENOSYS, errno.EINVAL}:
                raise LoggingSafetyError(f"Unable to create rotated log exclusively: {exc}") from exc
            _SafeRotatingFileHandler._copy_backup(source, destination, before)
            return
        # Exactly two links are expected during this transfer; a further alias fails closed.
        check_ancestors(source)
        check_ancestors(destination)
        original, created = source.lstat(), destination.lstat()
        if (is_link(original) or is_link(created) or original.st_nlink != 2 or created.st_nlink != 2
                or not _same_snapshot(original, before) or not _same_file(original, created)):
            raise LoggingSafetyError("Log changed during rotation; both files were preserved")
        source.unlink()
        _regular(destination, missing_ok=False)

    @staticmethod
    def _copy_backup(source: Path, destination: Path, before: os.stat_result) -> None:
        """Fallback for filesystems without hard links; failures preserve both paths."""
        _regular(source, missing_ok=False)
        if _regular(destination) is not None:
            raise LoggingSafetyError("Rotated destination is occupied")
        read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        read_fd = os.open(source, read_flags)
        write_fd = None
        try:
            if not _same_snapshot(os.fstat(read_fd), before) or os.fstat(read_fd).st_nlink != 1:
                raise LoggingSafetyError("Source log changed during rotation")
            write_fd = os.open(destination, write_flags, 0o600)
            if not _same_file(os.fstat(write_fd), _regular(destination, missing_ok=False)):
                raise LoggingSafetyError("Rotated log changed during creation")
            while chunk := os.read(read_fd, 1024 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(write_fd, view)
                    if written <= 0:
                        raise OSError("Rotated log write made no progress")
                    view = view[written:]
            os.fsync(write_fd)
            if not _same_snapshot(_regular(source, missing_ok=False), before) or not _same_snapshot(os.fstat(read_fd), before):
                raise LoggingSafetyError("Source log changed during rotation; both files were preserved")
            if not _same_file(os.fstat(write_fd), _regular(destination, missing_ok=False)):
                raise LoggingSafetyError("Rotated log changed during rotation; both files were preserved")
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
        if not _same_snapshot(_regular(source, missing_ok=False), before):
            raise LoggingSafetyError("Source log changed before rotation completed")
        _regular(destination, missing_ok=False)
        source.unlink()

    def doRollover(self) -> None:
        self._check_family()
        self._check_stream()
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        for number in range(self.backupCount, 0, -1):
            destination = Path(f"{self.baseFilename}.{number}")
            if number == self.backupCount and _regular(destination) is not None:
                _regular(destination, missing_ok=False)
                destination.unlink()
            source = self.target if number == 1 else Path(f"{self.baseFilename}.{number - 1}")
            if _regular(source) is not None:
                self._rotate_file(source, destination)
        self._check_family()
        self.stream = self._open()


def configure_logging(state_dir: Path) -> logging.Logger:
    """Configure one current target; importing this module creates no files."""
    state_dir = absolute(Path(state_dir))
    target = state_dir / "smartsort.log"
    with _configuration_lock:
        for path in (target, Path(str(target) + ".1"), Path(str(target) + ".2")):
            _regular(path)
        state_dir.mkdir(parents=True, exist_ok=True)
        check_ancestors(state_dir)
        if not state_dir.is_dir():
            raise LoggingSafetyError("Log storage must be a regular directory")
        logger = logging.getLogger("smartsort")
        current = next((handler for handler in logger.handlers
                        if isinstance(handler, _SafeRotatingFileHandler)
                        and handler.target == target and not handler._closed), None)
        if current is not None:
            current._check_family()
            current._check_stream()
        else:
            current = _SafeRotatingFileHandler(target)
            try:
                current.stream = current._open()
                current.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            except Exception:
                current.close()
                raise
        for old in tuple(logger.handlers):
            if isinstance(old, logging.FileHandler) and old is not current:
                logger.removeHandler(old)
                old.close()
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if current not in logger.handlers:
            logger.addHandler(current)
        return logger
