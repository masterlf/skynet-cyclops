"""Strict, minimal, atomically replaced status projection."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from .errors import ProjectionError

MAX_PROJECTION_BYTES = 256 * 1024
_TOP_LEVEL = {"schema_version", "projection_version", "supervisor", "missions", "incidents", "cost"}


def validate_projection(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL:
        raise ProjectionError("status projection schema is invalid")
    if value.get("schema_version") != 1 or value.get("projection_version") != 1:
        raise ProjectionError("status projection version is unsupported")
    supervisor = value.get("supervisor")
    allowed_supervisor = {"mode", "state", "heartbeat_at", "tick_seq", "post_gap"}
    if not isinstance(supervisor, dict) or set(supervisor) != allowed_supervisor:
        raise ProjectionError("status supervisor schema is invalid")
    if supervisor.get("mode") != "observe" or not isinstance(supervisor.get("post_gap"), bool):
        raise ProjectionError("status supervisor mode is invalid")
    if not isinstance(supervisor.get("tick_seq"), int) or isinstance(
        supervisor.get("tick_seq"), bool
    ):
        raise ProjectionError("status tick sequence is invalid")
    if not isinstance(value.get("missions"), list) or len(value["missions"]) > 64:
        raise ProjectionError("status missions are invalid")
    if not isinstance(value.get("incidents"), list) or len(value["incidents"]) > 256:
        raise ProjectionError("status incidents are invalid")
    cost = value.get("cost")
    if not isinstance(cost, dict) or set(cost) != {"classification"}:
        raise ProjectionError("status cost schema is invalid")
    if cost["classification"] not in {"actual", "estimated", "unknown"}:
        raise ProjectionError("status cost classification is invalid")
    return value


def write_projection(path: str | os.PathLike[str], payload: object) -> None:
    validated = validate_projection(payload)
    encoded = (
        json.dumps(validated, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_PROJECTION_BYTES:
        raise ProjectionError("status projection is too large")
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if target.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise ProjectionError("status projection must be a regular file")
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
    except ProjectionError:
        raise
    except OSError as exc:
        raise ProjectionError("status projection could not be written") from exc


def read_projection(path: str | os.PathLike[str]) -> dict[str, Any]:
    target = Path(path)
    try:
        info = target.lstat()
        if target.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise ProjectionError("status projection must be a regular file")
        if info.st_size > MAX_PROJECTION_BYTES:
            raise ProjectionError("status projection is too large")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ProjectionError("status projection permissions are unsafe")
        with target.open("rb") as handle:
            raw = handle.read(MAX_PROJECTION_BYTES + 1)
        if len(raw) > MAX_PROJECTION_BYTES:
            raise ProjectionError("status projection is too large")
        data = json.loads(raw)
    except ProjectionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectionError("status projection is unavailable") from exc
    return validate_projection(data)
