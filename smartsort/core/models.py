"""Shared immutable records: planning does not mutate the filesystem."""
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class OperationType(StrEnum):
    MOVE = "move"
    COPY = "copy"


class Status(StrEnum):
    PLANNED = "planned"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    NEEDS_REPLAN = "needs_replan"
    UNDONE = "undone"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int
    device: int
    inode: int
    sha256: str = ""


@dataclass(frozen=True)
class FileInfo:
    path: Path
    identity: FileIdentity
    modified: float
    created: float | None = None


@dataclass(frozen=True)
class ScanOptions:
    recursive: bool = False
    include_hidden: bool = False
    excluded_dirs: tuple[str, ...] = ()
    include_extensions: tuple[str, ...] = ()
    exclude_extensions: tuple[str, ...] = ()
    destination_dirs: tuple[Path, ...] = ()
    excluded_files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ScanResult:
    files: tuple[FileInfo, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class OrganizationRule:
    name: str
    destination: str
    extensions: tuple[str, ...] = ()
    pattern: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    modified_before: float | None = None
    modified_after: float | None = None
    modified_older_than_days: float | None = None
    created_before: float | None = None
    created_after: float | None = None
    created_older_than_days: float | None = None


@dataclass(frozen=True)
class AppConfig:
    rules: tuple[OrganizationRule, ...]
    fallback: str = "Others"
    duplicates_folder: str = "Duplicates"


@dataclass(frozen=True)
class PlannedOperation:
    source: Path
    destination: Path
    operation: OperationType
    reason: str
    identity: FileIdentity
    conflict: str = "none"
    category: str = ""


@dataclass(frozen=True)
class OperationPlan:
    root: Path
    items: tuple[PlannedOperation, ...]
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperationResult:
    item: PlannedOperation
    status: Status
    error: str = ""


@dataclass(frozen=True)
class BatchResult:
    session_id: str | None
    results: tuple[OperationResult, ...]
    errors: tuple[str, ...] = ()

    def counts(self) -> dict[str, int]:
        return {status.value: sum(r.status == status for r in self.results) for status in Status}


@dataclass(frozen=True)
class DuplicateGroup:
    size: int
    sha256: str
    files: tuple[Path, ...]


@dataclass(frozen=True)
class DuplicateReport:
    groups: tuple[DuplicateGroup, ...]
    errors: tuple[str, ...] = ()
    hashed_files: int = 0
