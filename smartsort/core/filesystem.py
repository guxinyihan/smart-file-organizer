"""Conservative filesystem primitives shared by execution and undo.

The root lock coordinates SmartSort processes. Identity checks also detect changes
made by other programs; no filesystem operation is claimed to be atomic with SQL.
"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import hashlib
import os
from pathlib import Path
import stat
import threading
from typing import Iterator

from .models import FileIdentity, OperationType


class UnsafePathError(ValueError):
    pass


class SourceChangedError(OSError):
    pass


class RootBusyError(OSError):
    pass


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def absolute(path: Path) -> Path:
    path = Path(path)
    if ".." in path.parts:
        raise UnsafePathError("Parent traversal is not allowed")
    return Path(os.path.abspath(path))


def check_ancestors(path: Path) -> None:
    path = absolute(path)
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if is_link(info):
            raise UnsafePathError(f"Links and reparse points are not allowed: {part}")
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise UnsafePathError(f"Parent is not a directory: {part}")


def validate_root(root: Path) -> Path:
    root = absolute(root)
    if root.name.casefold() == ".git" or root.name.casefold().startswith(".smartsort"):
        raise UnsafePathError("Metadata directories cannot be organization roots")
    check_ancestors(root)
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise UnsafePathError("Organization root must be a directory")
    return root


def validate_path(root: Path, path: Path, *, must_exist: bool = False) -> Path:
    root = validate_root(root)
    path = Path(path)
    if not path.is_absolute():
        raise UnsafePathError("Journal and plan paths must be absolute")
    path = absolute(path)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError("Path is outside the organization root") from exc
    if not relative.parts:
        raise UnsafePathError("A file operation cannot target the root")
    if any(p.casefold().startswith(".smartsort") or p.casefold() == ".git" for p in relative.parts):
        raise UnsafePathError("SmartSort metadata and Git internals are protected")
    reserved = {"con", "prn", "aux", "nul", "clock$"} | {f"{prefix}{n}" for prefix in ("com", "lpt") for n in range(1, 10)}
    for component in relative.parts:
        if (component.endswith((".", " ")) or component.casefold().split(".", 1)[0] in reserved
                or any(ord(c) < 32 or c in '<>:"|?*' for c in component)):
            raise UnsafePathError("Unsafe or nonportable file path component")
    check_ancestors(path)
    if must_exist:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or is_link(info):
            raise UnsafePathError("Source must be a regular file")
    return path


def _identity(info: os.stat_result, digest: str = "") -> FileIdentity:
    return FileIdentity(info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, digest)


def same_identity(actual: FileIdentity, expected: FileIdentity, *, digest: bool = False) -> bool:
    basic = (actual.size, actual.mtime_ns, actual.device, actual.inode) == (
        expected.size, expected.mtime_ns, expected.device, expected.inode
    )
    return basic and (not digest or bool(expected.sha256) and actual.sha256 == expected.sha256)


def identify(path: Path, *, hash_content: bool = True) -> FileIdentity:
    check_ancestors(path)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or is_link(before):
        raise UnsafePathError("Only regular files can be identified")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not same_identity(_identity(opened), _identity(before)):
            raise SourceChangedError("File changed while opening")
        digest = hashlib.sha256()
        if hash_content:
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
        after = os.fstat(fd)
        check_ancestors(path)
        current = path.lstat()
        if not same_identity(_identity(after), _identity(before)) or not same_identity(_identity(current), _identity(before)):
            raise SourceChangedError("File changed while reading")
        return _identity(after, digest.hexdigest() if hash_content else "")
    finally:
        os.close(fd)


def verify(path: Path, expected: FileIdentity) -> FileIdentity:
    actual = identify(path, hash_content=bool(expected.sha256))
    if not same_identity(actual, expected, digest=bool(expected.sha256)):
        raise SourceChangedError(f"File changed since it was recorded: {path.name}")
    return actual


def ensure_parent(root: Path, path: Path) -> None:
    path = validate_path(root, path)
    parent = path.parent
    for directory in reversed((parent, *parent.parents)):
        if directory == root or root in directory.parents:
            check_ancestors(directory)
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            check_ancestors(directory)
            if not directory.is_dir():
                raise UnsafePathError("Destination parent is not a directory")


def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_verified(root: Path, path: Path, expected: FileIdentity) -> None:
    path = validate_path(root, path, must_exist=True)
    verify(path, expected)
    path.unlink()
    _sync_directory(path.parent)


def _exclusive_copy(root: Path, source: Path, destination: Path, expected: FileIdentity) -> FileIdentity:
    source = validate_path(root, source, must_exist=True)
    destination = validate_path(root, destination)
    verify(source, expected)
    input_fd = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    output_fd = None
    owned = None
    try:
        if not same_identity(_identity(os.fstat(input_fd)), expected):
            raise SourceChangedError("Source changed while opening")
        validate_path(root, destination)
        output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
        owned = _identity(os.fstat(output_fd))
        digest = hashlib.sha256()
        while chunk := os.read(input_fd, 1024 * 1024):
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(output_fd, view)
                if written <= 0:
                    raise OSError("Destination write made no progress")
                view = view[written:]
        os.fsync(output_fd)
        result = _identity(os.fstat(output_fd), digest.hexdigest())
        if not same_identity(_identity(os.fstat(input_fd)), expected):
            raise SourceChangedError("Source changed during transfer")
        verify(source, expected)
        if expected.sha256 and result.sha256 != expected.sha256:
            raise SourceChangedError("Source content changed during transfer")
        validate_path(root, destination, must_exist=True)
        if not same_identity(identify(destination), result, digest=True):
            raise SourceChangedError("Destination changed during transfer")
        _sync_directory(destination.parent)
        return result
    except Exception:
        # Delete only the file created by this call; never delete a replacement.
        if output_fd is not None and owned is not None:
            try:
                current = destination.lstat()
                opened = os.fstat(output_fd)
                if not is_link(current) and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    os.close(output_fd)
                    output_fd = None
                    validate_path(root, destination, must_exist=True)
                    destination.unlink()
            except (OSError, ValueError):
                pass
        raise
    finally:
        os.close(input_fd)
        if output_fd is not None:
            os.close(output_fd)


def create_transfer(root: Path, source: Path, destination: Path, expected: FileIdentity, operation: OperationType) -> FileIdentity:
    """Create destination exclusively; MOVE source removal belongs to its journal."""
    source = validate_path(root, source, must_exist=True)
    destination = validate_path(root, destination)
    if source == destination:
        raise UnsafePathError("Source and destination must differ")
    verify(source, expected)
    ensure_parent(root, destination)
    if operation == OperationType.MOVE:
        try:
            validate_path(root, destination)
            os.link(source, destination, follow_symlinks=False)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.ENOSYS, errno.EINVAL}:
                raise
        else:
            _sync_directory(destination.parent)
            # A changed source leaves both links for recovery instead of deleting data.
            verify(source, expected)
            result = identify(destination)
            if expected.sha256 and result.sha256 != expected.sha256:
                raise SourceChangedError("Linked destination changed during transfer")
            return result
    return _exclusive_copy(root, source, destination, expected)


@contextmanager
def root_lock(root: Path) -> Iterator[None]:
    """An OS advisory lock plus an in-process lock; released on process exit."""
    root = validate_root(root)
    key = os.path.normcase(str(root))
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise RootBusyError("Another SmartSort operation is using this folder")
    fd = None
    acquired = False
    lock_path = root / ".smartsort.lock"
    try:
        check_ancestors(lock_path)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or is_link(info) or (info.st_dev, info.st_ino) != (lock_path.lstat().st_dev, lock_path.lstat().st_ino):
            raise UnsafePathError("Unsafe lock file")
        if info.st_size == 0:
            os.write(fd, b"\0")
            os.fsync(fd)
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    except (BlockingIOError, PermissionError) as exc:
        raise RootBusyError("Another SmartSort process is using this folder") from exc
    finally:
        if acquired and fd is not None:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        if fd is not None:
            os.close(fd)
        lock.release()
