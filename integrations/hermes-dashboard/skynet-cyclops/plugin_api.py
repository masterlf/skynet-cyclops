"""Authenticated host-mounted, read-only status route for Hermes Dashboard."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from skynet_cyclops.errors import ProjectionError
from skynet_cyclops.projection import validate_projection

MAX_STATUS_BYTES = 256 * 1024
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TOP = {"schema_version", "projection_version", "supervisor", "missions", "incidents", "cost"}


class StatusUnavailable(Exception):
    """Generic public error; details and paths are intentionally omitted."""


def _safe_string(value: object) -> bool:
    return isinstance(value, str) and _SAFE.fullmatch(value) is not None


def _validate_status(value: object) -> dict[str, Any]:
    try:
        return validate_projection(value)
    except ProjectionError as exc:
        raise StatusUnavailable("status unavailable") from exc


def _validate_mission(value: object) -> None:
    fields = {"id", "manifest_sha256", "outcome", "next_phase", "phases", "workers"}
    if not isinstance(value, dict) or set(value) != fields:
        raise StatusUnavailable("status unavailable")
    if not _safe_string(value.get("id")) or not _safe_string(value.get("outcome")):
        raise StatusUnavailable("status unavailable")
    digest = value.get("manifest_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
        raise StatusUnavailable("status unavailable")
    if value.get("next_phase") is not None and not _safe_string(value.get("next_phase")):
        raise StatusUnavailable("status unavailable")
    phases = value.get("phases")
    workers = value.get("workers")
    if not isinstance(phases, list) or len(phases) > 64:
        raise StatusUnavailable("status unavailable")
    if not isinstance(workers, list) or len(workers) > 256:
        raise StatusUnavailable("status unavailable")
    for phase in phases:
        if not isinstance(phase, dict) or not {"key", "state", "evidence_present"}.issubset(phase):
            raise StatusUnavailable("status unavailable")
        if set(phase) - {"key", "state", "task_id", "assignee", "evidence_present", "retry_count"}:
            raise StatusUnavailable("status unavailable")
        if not _safe_string(phase.get("key")) or not _safe_string(phase.get("state")):
            raise StatusUnavailable("status unavailable")
        evidence = phase.get("evidence_present")
        if (
            not isinstance(evidence, (list, tuple))
            or len(evidence) > 32
            or not all(_safe_string(item) for item in evidence)
        ):
            raise StatusUnavailable("status unavailable")
    for worker in workers:
        if not isinstance(worker, dict) or set(worker) != {
            "task_id",
            "run_id",
            "assignee",
            "status",
            "heartbeat_age_seconds",
            "retry_count",
        }:
            raise StatusUnavailable("status unavailable")
        if not all(
            _safe_string(worker.get(key)) for key in ("task_id", "run_id", "assignee", "status")
        ):
            raise StatusUnavailable("status unavailable")
        age = worker.get("heartbeat_age_seconds")
        if age is not None and (isinstance(age, bool) or not isinstance(age, int) or age < 0):
            raise StatusUnavailable("status unavailable")


def _validate_incident(value: object) -> None:
    fields = {"id", "phase_key", "kind", "severity", "age_ticks", "observed_ticks", "disposition"}
    if not isinstance(value, dict) or set(value) != fields:
        raise StatusUnavailable("status unavailable")
    if not all(
        _safe_string(value.get(key))
        for key in ("id", "phase_key", "kind", "severity", "disposition")
    ):
        raise StatusUnavailable("status unavailable")
    for key in ("age_ticks", "observed_ticks"):
        if (
            isinstance(value.get(key), bool)
            or not isinstance(value.get(key), int)
            or value[key] < 0
        ):
            raise StatusUnavailable("status unavailable")


def read_status(path: str | os.PathLike[str]) -> dict[str, Any]:
    candidate = Path(path)
    try:
        info = candidate.lstat()
        if candidate.is_symlink() or not stat.S_ISREG(info.st_mode):
            raise StatusUnavailable("status unavailable")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StatusUnavailable("status unavailable")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise StatusUnavailable("status unavailable")
        if info.st_size > MAX_STATUS_BYTES:
            raise StatusUnavailable("status unavailable")
        with candidate.open("rb") as handle:
            raw = handle.read(MAX_STATUS_BYTES + 1)
        if len(raw) > MAX_STATUS_BYTES:
            raise StatusUnavailable("status unavailable")
        return _validate_status(json.loads(raw))
    except StatusUnavailable:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StatusUnavailable("status unavailable") from exc


def _configured_status_path() -> Path:
    configured = os.environ.get("SKYNET_CYCLOPS_STATUS_PATH")
    if configured is not None:
        if not configured or len(configured) > 4096 or "\x00" in configured:
            raise StatusUnavailable("status unavailable")
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "skynet-cyclops" / "status.json"


router = APIRouter()


@router.get("/status", response_class=JSONResponse)
def get_status() -> JSONResponse:
    """Return the strict projection; the mounting Hermes host authenticates the request."""
    try:
        return JSONResponse(
            content=read_status(_configured_status_path()),
            headers={"Cache-Control": "no-store"},
        )
    except StatusUnavailable:
        raise HTTPException(status_code=503, detail="status unavailable") from None
