"""Ordered predicates and portable, contained destination directory validation."""
from __future__ import annotations

import fnmatch
import math
import os
from pathlib import Path, PureWindowsPath
import stat
import time

from .models import AppConfig, FileInfo, OrganizationRule


INTERNAL_DIRECTORY_NAMES = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".smartsort",
    ".smartsort-state", ".smartsort-staging",
})
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "clock$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def is_linklike(metadata: os.stat_result) -> bool:
    """Junctions and other Windows reparse points are links for our safety policy."""
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def assert_safe_directory_chain(path: Path) -> None:
    """Reject linked/reparse/non-directory existing components, without creating any."""
    path = path.absolute()
    for component in (*reversed(path.parents), path):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if is_linklike(metadata):
            raise ValueError(f"Directory contains a symlink or reparse point: {component}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Directory component is not a directory: {component}")


def validate_category(category: str) -> str:
    """Require a portable relative path; reject invalid input instead of rewriting it."""
    if not isinstance(category, str) or not category:
        raise ValueError("Destination must be a nonempty relative directory path")
    if "\\" in category or PureWindowsPath(category).drive or category.startswith("/"):
        raise ValueError(f"Destination must be relative and use forward slashes: {category!r}")
    for component in category.split("/"):
        if component in {"", ".", ".."}:
            raise ValueError(f"Destination contains an empty or traversal component: {category!r}")
        if component.endswith((".", " ")) or any(
            ord(character) < 32 or character in '<>:"|?*' for character in component
        ):
            raise ValueError(f"Destination contains an invalid portable directory name: {category!r}")
        folded = component.casefold()
        if folded in INTERNAL_DIRECTORY_NAMES or folded.startswith(".smartsort"):
            raise ValueError(f"Destination uses an internal directory name: {category!r}")
        if folded.split(".", 1)[0] in _WINDOWS_RESERVED:
            raise ValueError(f"Destination uses a reserved Windows name: {category!r}")
    return category


def resolve_destination(root: Path, category: str) -> Path:
    validate_category(category)
    root = Path(root).expanduser().absolute()
    assert_safe_directory_chain(root)
    if not root.is_dir():
        raise ValueError(f"Organization root is not an existing directory: {root}")
    resolved_root = root.resolve(strict=True)
    destination = resolved_root.joinpath(*category.split("/"))
    assert_safe_directory_chain(destination)
    resolved = destination.resolve(strict=False)
    if not resolved.is_relative_to(resolved_root) or resolved == resolved_root:
        raise ValueError(f"Destination escapes the organization root: {category!r}")
    return resolved


def destination_dirs(root: Path, config: AppConfig) -> tuple[Path, ...]:
    categories = dict.fromkeys([
        *(rule.destination for rule in config.rules), config.fallback, config.duplicates_folder,
    ])
    return tuple(resolve_destination(root, category) for category in categories)


def _matches(info: FileInfo, rule: OrganizationRule, now: float) -> bool:
    if rule.extensions and not any(
        info.path.name.casefold().endswith(extension) for extension in rule.extensions
    ):
        return False
    if rule.pattern is not None and not fnmatch.fnmatchcase(info.path.name, rule.pattern):
        return False
    if rule.min_size is not None and info.identity.size < rule.min_size:
        return False
    if rule.max_size is not None and info.identity.size > rule.max_size:
        return False
    for prefix, timestamp in (("modified", info.modified), ("created", info.created)):
        before = getattr(rule, f"{prefix}_before")
        after = getattr(rule, f"{prefix}_after")
        older = getattr(rule, f"{prefix}_older_than_days")
        if any(value is not None for value in (before, after, older)):
            # POSIX ctime is metadata-change time, so unsupported birth time stays None.
            if timestamp is None:
                return False
            if before is not None and not timestamp < before:
                return False
            if after is not None and not timestamp > after:
                return False
            if older is not None and not timestamp < now - older * 86400:
                return False
    return True


def select_destination(
    info: FileInfo, config: AppConfig, now: float | None = None,
) -> tuple[str, str]:
    now = time.time() if now is None else now
    if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
        raise ValueError("Rule evaluation time must be a finite timestamp")
    for rule in config.rules:
        if _matches(info, rule, now):
            return rule.destination, rule.name
    return config.fallback, "Fallback (no matching rule)"
