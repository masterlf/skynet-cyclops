"""Strict XDG-friendly runtime configuration."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import ValidationError

MAX_CONFIG_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class Config:
    schema_version: int
    manifest_path: Path
    ledger_path: Path
    status_path: Path
    hermes_binary: str
    incident_debounce_ticks: int


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "skynet-cyclops" / "config.yaml"


def default_ledger_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "skynet-cyclops" / "ledger.db"


def default_status_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "skynet-cyclops" / "status.json"


def load_config(path: str | os.PathLike[str]) -> Config:
    candidate = Path(path).expanduser()
    try:
        info = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ValidationError("configuration must be a regular file")
        if info.st_size > MAX_CONFIG_BYTES:
            raise ValidationError("configuration is too large")
        raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    except ValidationError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValidationError("configuration is unavailable") from exc
    allowed = {
        "schema_version",
        "manifest_path",
        "ledger_path",
        "status_path",
        "hermes_binary",
        "incident_debounce_ticks",
    }
    if not isinstance(raw, dict) or set(raw) != allowed:
        raise ValidationError("configuration schema is invalid")
    if raw["schema_version"] != 1:
        raise ValidationError("configuration version is unsupported")
    paths = []
    for key in ("manifest_path", "ledger_path", "status_path"):
        value = raw[key]
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise ValidationError(f"configuration {key} is invalid")
        paths.append(Path(value).expanduser())
    binary = raw["hermes_binary"]
    debounce = raw["incident_debounce_ticks"]
    if not isinstance(binary, str) or not binary or len(binary) > 4096 or "\x00" in binary:
        raise ValidationError("configuration hermes_binary is invalid")
    if isinstance(debounce, bool) or not isinstance(debounce, int) or not 1 <= debounce <= 10:
        raise ValidationError("configuration incident_debounce_ticks is invalid")
    return Config(1, paths[0], paths[1], paths[2], binary, debounce)
