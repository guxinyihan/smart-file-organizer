"""Deterministic directory traversal with early pruning and per-path errors."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from smartsort.config.manager import default_state_dir, normalize_extension
from .models import FileIdentity, FileInfo, ScanOptions, ScanResult
from .rules import INTERNAL_DIRECTORY_NAMES, assert_safe_directory_chain, is_linklike


def _hidden(name: str, metadata: os.stat_result) -> bool:
    return name.startswith(".") or bool(
        getattr(metadata, "st_file_attributes", 0)
        & (getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2) | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4))
    )


def _extension_matches(path: Path, extensions: tuple[str, ...]) -> bool:
    name = path.name.casefold()
    return any(
        (not path.suffix if extension == "" else name.endswith(extension))
        for extension in extensions
    )


def _birth_time(metadata: os.stat_result) -> float | None:
    # st_ctime on Unix measures metadata changes, not creation. Windows documents
    # st_ctime as creation in 3.11; newer Python supplies st_birthtime explicitly.
    birth = getattr(metadata, "st_birthtime", None)
    return birth if birth is not None else metadata.st_ctime if os.name == "nt" else None


def scan(root: Path, options: ScanOptions = ScanOptions()) -> ScanResult:
    root = Path(root).expanduser().absolute()
    found: list[FileInfo] = []
    errors: list[str] = []
    try:
        assert_safe_directory_chain(root)
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Scan root is not a directory")
        if not isinstance(options, ScanOptions):
            raise ValueError("Options must be ScanOptions")
        includes = tuple(normalize_extension(value) for value in options.include_extensions)
        excludes = tuple(normalize_extension(value) for value in options.exclude_extensions)
        excluded_names: set[str] = set()
        excluded_paths: set[Path] = set()
        for value in options.excluded_dirs:
            if not isinstance(value, str) or not value:
                raise ValueError("Excluded directories must be nonempty names or relative paths")
            normalized = value.replace("\\", "/")
            path = Path(normalized)
            if path.is_absolute():
                excluded_paths.add(path.absolute())
            elif "/" in normalized:
                if ".." in path.parts:
                    raise ValueError("Excluded relative directories cannot contain '..'")
                excluded_paths.add(root / path)
            else:
                excluded_names.add(value.casefold())
        destinations = tuple(Path(path).expanduser().absolute() for path in options.destination_dirs)
        excluded_file_paths = {
            Path(path).expanduser().resolve(strict=False) for path in options.excluded_files
        }
        state = default_state_dir().resolve(strict=False)
        if root == state or root.is_relative_to(state):
            raise ValueError("SmartSort's state directory cannot be scanned")
    except (OSError, ValueError, RuntimeError) as error:
        return ScanResult((), (f"{root}: {error}",))

    visited: set[tuple[int, int]] = set()

    def pruned(path: Path) -> bool:
        folded = path.name.casefold()
        return (
            folded in INTERNAL_DIRECTORY_NAMES
            or folded.startswith(".smartsort")
            or folded in excluded_names
            or path in excluded_paths
            or path == state
            or any(path == destination or path.is_relative_to(destination) for destination in destinations)
        )

    def visit(directory: Path) -> None:
        try:
            assert_safe_directory_chain(directory)
            metadata = directory.lstat()
            if is_linklike(metadata) or not stat.S_ISDIR(metadata.st_mode):
                errors.append(f"{directory}: Directory changed into a link or non-directory; skipped")
                return
            key = (metadata.st_dev, metadata.st_ino)
            if metadata.st_ino and key in visited:
                return
            if metadata.st_ino:
                visited.add(key)
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        except (OSError, ValueError) as error:
            errors.append(f"{directory}: {error}")
            return
        for entry in entries:
            path = directory / entry.name
            if path in excluded_file_paths:
                continue
            try:
                assert_safe_directory_chain(path.parent)
                # Windows DirEntry.stat may report device/inode as zero. Path.lstat
                # supplies identities consistent with later execution revalidation.
                metadata = path.lstat()
                if is_linklike(metadata):
                    continue
                if not options.include_hidden and _hidden(entry.name, metadata):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if options.recursive and not pruned(path):
                        visit(path)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                if path.is_relative_to(state) or path.name.casefold().startswith(".smartsort"):
                    continue
                if includes and not _extension_matches(path, includes):
                    continue
                if excludes and _extension_matches(path, excludes):
                    continue
                found.append(FileInfo(
                    path=path,
                    identity=FileIdentity(metadata.st_size, metadata.st_mtime_ns, metadata.st_dev, metadata.st_ino),
                    modified=metadata.st_mtime,
                    created=_birth_time(metadata),
                ))
            except (OSError, ValueError) as error:
                errors.append(f"{path}: {error}")

    visit(root)
    found.sort(key=lambda info: (info.path.relative_to(root).as_posix().casefold(), info.path.relative_to(root).as_posix()))
    return ScanResult(tuple(found), tuple(errors))
