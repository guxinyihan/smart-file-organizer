"""Validated version-1 rules and explicit, atomic configuration persistence."""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import sys
import tempfile

from smartsort.core.models import AppConfig, OrganizationRule
from smartsort.core.rules import assert_safe_directory_chain, is_linklike, validate_category


class ConfigError(ValueError):
    """An actionable configuration validation/load error."""


def default_state_dir() -> Path:
    """Query the user data location without creating files or directories."""
    override = os.environ.get("SMARTSORT_DATA_DIR")
    if override:
        return Path(override).expanduser().absolute()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "SmartSort"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SmartSort"
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "smartsort"


def normalize_extension(value: str) -> str:
    if not isinstance(value, str):
        raise ConfigError("Extensions must be strings")
    value = value.strip().casefold()
    if value == "":
        return ""
    if any(character.isspace() or character in '/\\:*?<>|"' or ord(character) < 32 for character in value):
        raise ConfigError(f"Invalid extension: {value!r}")
    if not value.startswith("."):
        value = "." + value
    if value == "." or value.endswith(".") or ".." in value:
        raise ConfigError(f"Invalid extension: {value!r}")
    return value


def _category(value: object, label: str) -> str:
    try:
        return validate_category(value)
    except ValueError as error:
        raise ConfigError(f"{label}: {error}") from error


def _date(value: object, label: str) -> float:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be an ISO-8601 date or datetime string")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        timestamp = parsed.timestamp()
    except (ValueError, OverflowError, OSError) as error:
        raise ConfigError(f"{label} is not a supported ISO-8601 date") from error
    if not math.isfinite(timestamp):
        raise ConfigError(f"{label} must be a finite date")
    return timestamp


def _rule(document: object, index: int) -> OrganizationRule:
    label = f"Rule {index + 1}"
    if not isinstance(document, dict):
        raise ConfigError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in document):
        raise ConfigError(f"{label} keys must be strings")
    supported = {field.name for field in fields(OrganizationRule)}
    if set(document) - supported:
        raise ConfigError(f"{label} has unknown fields: {', '.join(sorted(set(document) - supported))}")
    name = document.get("name", label)
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"{label} name must be a nonempty string")
    values: dict = {"name": name, "destination": _category(document.get("destination"), label)}
    extensions = document.get("extensions", [])
    if not isinstance(extensions, list) or any(not isinstance(value, str) or not value.strip() for value in extensions):
        raise ConfigError(f"{label} extensions must be a list of nonempty strings")
    values["extensions"] = tuple(dict.fromkeys(normalize_extension(value) for value in extensions))
    if "pattern" in document:
        pattern = document["pattern"]
        if not isinstance(pattern, str) or not pattern or any(character in pattern for character in "/\\\x00"):
            raise ConfigError(f"{label} pattern must be a nonempty filename glob (no directory separators)")
        values["pattern"] = pattern
    for field in ("min_size", "max_size"):
        if field in document:
            value = document[field]
            if type(value) is not int or value < 0:
                raise ConfigError(f"{label} {field} must be a nonnegative integer (bytes)")
            values[field] = value
    if values.get("min_size", 0) > values.get("max_size", math.inf):
        raise ConfigError(f"{label} min_size cannot exceed max_size")
    for prefix in ("modified", "created"):
        for boundary in ("before", "after"):
            field = f"{prefix}_{boundary}"
            if field in document:
                values[field] = _date(document[field], f"{label} {field}")
        if values.get(f"{prefix}_after", -math.inf) > values.get(f"{prefix}_before", math.inf):
            raise ConfigError(f"{label} {prefix}_after cannot exceed {prefix}_before")
        field = f"{prefix}_older_than_days"
        if field in document:
            value = document[field]
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise ConfigError(f"{label} {field} must be a finite nonnegative number")
            values[field] = float(value)
    return OrganizationRule(**values)


def validate_config(document: dict) -> AppConfig:
    if not isinstance(document, dict):
        raise ConfigError("Configuration must be a JSON object")
    if any(not isinstance(key, str) for key in document):
        raise ConfigError("Configuration keys must be strings")
    if "version" not in document and "rules" not in document:
        if any(not isinstance(key, str) or not key.startswith(".") for key in document):
            raise ConfigError("Legacy extension mappings must use extension keys beginning with '.'")
        grouped: dict[str, list[str]] = {}
        for extension, category in document.items():
            destination = _category(category, f"Extension {extension}")
            normalized = normalize_extension(extension)
            grouped.setdefault(destination, []).append(normalized)
        return AppConfig(tuple(
            OrganizationRule(f"{category} extensions", category, tuple(dict.fromkeys(extensions)))
            for category, extensions in grouped.items()
        ))
    supported = {"version", "rules", "fallback", "duplicates_folder"}
    if set(document) - supported:
        raise ConfigError(f"Unknown configuration fields: {', '.join(sorted(set(document) - supported))}")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ConfigError("Configuration version must be 1")
    rules = document.get("rules")
    if not isinstance(rules, list):
        raise ConfigError("Configuration rules must be a list")
    return AppConfig(
        rules=tuple(_rule(rule, index) for index, rule in enumerate(rules)),
        fallback=_category(document.get("fallback", "Others"), "Fallback"),
        duplicates_folder=_category(document.get("duplicates_folder", "Duplicates"), "Duplicates folder"),
    )


def config_to_document(config: AppConfig) -> dict:
    rules = []
    for rule in config.rules:
        document = {"name": rule.name, "destination": rule.destination}
        for field in fields(OrganizationRule):
            if field.name in {"name", "destination"}:
                continue
            value = getattr(rule, field.name)
            if value is None or value == ():
                continue
            if field.name == "extensions":
                value = list(value)
            elif field.name.endswith(("_before", "_after")):
                value = datetime.fromtimestamp(value, timezone.utc).isoformat()
            document[field.name] = value
        rules.append(document)
    return {
        "version": 1, "rules": rules, "fallback": config.fallback,
        "duplicates_folder": config.duplicates_folder,
    }


def default_config_document() -> dict:
    # Packaged data is independent of the working directory and is never mutated.
    document = json.loads(files("smartsort").joinpath("data", "extensions.json").read_text(encoding="utf-8"))
    return config_to_document(validate_config(document))


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    document = {}
    for key, value in pairs:
        if key in document:
            raise ConfigError(f"Duplicate JSON key: {key}")
        document[key] = value
    return document


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        return validate_config(default_config_document())
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"Cannot load configuration {path}: {error}") from error
    return validate_config(document)


def save_config(path: Path, document: dict) -> None:
    config = validate_config(document)
    path = Path(path).expanduser().absolute()
    assert_safe_directory_chain(path.parent)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if is_linklike(metadata) or not path.is_file():
            raise ConfigError(f"Configuration target must be a regular file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".smartsort-config-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(config_to_document(config), output, indent=2, ensure_ascii=False, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        # Replace within the same directory; readers see the old or the complete new JSON.
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
