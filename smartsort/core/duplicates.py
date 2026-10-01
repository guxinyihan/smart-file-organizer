"""Size-first duplicate reporting; no deletion or other filesystem mutations."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import os
from pathlib import Path
import stat

from .models import DuplicateGroup, DuplicateReport, FileIdentity, FileInfo
from .rules import assert_safe_directory_chain, is_linklike


def _identity(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(metadata.st_size, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino)


def _sha256(info: FileInfo) -> str:
    assert_safe_directory_chain(info.path.parent)
    before = info.path.lstat()
    if is_linklike(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Duplicate candidate is no longer a regular, unlinked file")
    expected = info.identity
    if _identity(before) != FileIdentity(expected.size, expected.mtime_ns, expected.device, expected.inode):
        raise ValueError("Duplicate candidate changed since the scan")
    digest = hashlib.sha256()
    with info.path.open("rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError("Duplicate candidate was replaced before hashing")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        if _identity(os.fstat(stream.fileno())) != _identity(before):
            raise ValueError("Duplicate candidate changed while hashing")
    after = info.path.lstat()
    if is_linklike(after) or _identity(after) != _identity(before):
        raise ValueError("Duplicate candidate was replaced or changed while hashing")
    result = digest.hexdigest()
    if expected.sha256 and expected.sha256 != result:
        raise ValueError("Duplicate candidate content changed since its prior fingerprint")
    return result


def find_duplicates(files: tuple[FileInfo, ...]) -> DuplicateReport:
    candidates: dict[int, list[FileInfo]] = defaultdict(list)
    # A repeated input path is not a duplicate copy of itself.
    unique: dict[Path, FileInfo] = {info.path: info for info in files}
    for info in unique.values():
        candidates[info.identity.size].append(info)
    groups: list[DuplicateGroup] = []
    errors: list[str] = []
    hashed_files = 0
    for size in sorted(candidates):
        if len(candidates[size]) < 2:
            continue
        by_digest: dict[str, list[Path]] = defaultdict(list)
        for info in sorted(candidates[size], key=lambda item: (str(item.path).casefold(), str(item.path))):
            try:
                digest = _sha256(info)
                hashed_files += 1
                by_digest[digest].append(info.path)
            except (OSError, ValueError) as error:
                errors.append(f"{info.path}: {error}")
        for digest, paths in sorted(by_digest.items()):
            if len(paths) > 1:
                groups.append(DuplicateGroup(size, digest, tuple(paths)))
    return DuplicateReport(tuple(groups), tuple(errors), hashed_files)
