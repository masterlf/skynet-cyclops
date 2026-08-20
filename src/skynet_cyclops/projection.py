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


def _safe_identifier(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    return value[0].isalnum() and all(
        character.isalnum() or character in "._:-" for character in value
    )


def _bounded_int(value: object, minimum: int = 0, maximum: int = 10**9) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= maximum


def _validate_phase(value: object) -> None:
    fields = {"key", "state", "task_id", "assignee", "evidence_present", "retry_count"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ProjectionError("status phase schema is invalid")
    if not _safe_identifier(value["key"]) or value["state"] not in {
        "pending",
        "ready",
        "running",
        "review",
        "blocked",
        "failed",
        "done",
        "unknown",
    }:
        raise ProjectionError("status phase identity is invalid")
    if value["task_id"] is not None and not _safe_identifier(value["task_id"]):
        raise ProjectionError("status phase task is invalid")
    if not _safe_identifier(value["assignee"]) or not _bounded_int(value["retry_count"], 0, 100):
        raise ProjectionError("status phase metadata is invalid")
    evidence = value["evidence_present"]
    if (
        not isinstance(evidence, list)
        or len(evidence) > 32
        or not all(_safe_identifier(item) for item in evidence)
        or evidence != sorted(set(evidence))
    ):
        raise ProjectionError("status phase evidence is invalid")


def _validate_worker(value: object) -> None:
    fields = {"task_id", "run_id", "assignee", "status", "heartbeat_age_seconds", "retry_count"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ProjectionError("status worker schema is invalid")
    if not all(_safe_identifier(value[key]) for key in ("task_id", "run_id", "assignee", "status")):
        raise ProjectionError("status worker identity is invalid")
    age = value["heartbeat_age_seconds"]
    if age is not None and not _bounded_int(age):
        raise ProjectionError("status worker heartbeat is invalid")
    if not _bounded_int(value["retry_count"], 0, 100):
        raise ProjectionError("status worker retry count is invalid")


def _validate_mission(value: object) -> None:
    fields = {"id", "manifest_sha256", "outcome", "next_phase", "phases", "workers"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ProjectionError("status mission schema is invalid")
    if not _safe_identifier(value["id"]) or value["outcome"] not in {
        "running",
        "blocked",
        "failed",
        "done",
        "unknown",
    }:
        raise ProjectionError("status mission identity is invalid")
    digest = value["manifest_sha256"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(c not in "0123456789abcdef" for c in digest)
    ):
        raise ProjectionError("status manifest digest is invalid")
    if value["next_phase"] is not None and not _safe_identifier(value["next_phase"]):
        raise ProjectionError("status next phase is invalid")
    phases = value["phases"]
    workers = value["workers"]
    if not isinstance(phases, list) or len(phases) > 64:
        raise ProjectionError("status phases are invalid")
    if not isinstance(workers, list) or len(workers) > 256:
        raise ProjectionError("status workers are invalid")
    for phase in phases:
        _validate_phase(phase)
    for worker in workers:
        _validate_worker(worker)


def _validate_incident(value: object) -> None:
    fields = {"id", "phase_key", "kind", "severity", "age_ticks", "observed_ticks", "disposition"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ProjectionError("status incident schema is invalid")
    if not all(_safe_identifier(value[key]) for key in ("id", "phase_key", "kind")):
        raise ProjectionError("status incident identity is invalid")
    if value["severity"] not in {"warning", "critical"} or value["disposition"] not in {
        "observing",
        "active",
    }:
        raise ProjectionError("status incident state is invalid")
    if not _bounded_int(value["age_ticks"], 1) or not _bounded_int(value["observed_ticks"], 1):
        raise ProjectionError("status incident counters are invalid")


def _validate_incident_v2(value: object) -> None:
    fields = {
        "id",
        "generation",
        "mission_id",
        "phase_key",
        "kind",
        "subject_task_id",
        "subject_run_id",
        "severity",
        "age_ticks",
        "observed_ticks",
        "disposition",
        "lifecycle",
        "terminal_reason",
        "reason_code",
        "human_question_code",
        "attempt_count",
        "next_attempt_at",
        "manager_state",
        "notification_state",
        "acknowledged_at",
        "terminal_at",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ProjectionError("status incident v2 schema is invalid")
    if not all(_safe_identifier(value[key]) for key in ("id", "mission_id", "phase_key", "kind")):
        raise ProjectionError("status incident v2 identity is invalid")
    for key in (
        "subject_task_id",
        "subject_run_id",
        "terminal_reason",
        "reason_code",
        "human_question_code",
    ):
        if value[key] is not None and not _safe_identifier(value[key]):
            raise ProjectionError("status incident v2 optional identity is invalid")
    if (
        value["severity"] not in {"warning", "critical"}
        or value["disposition"] not in {"observing", "active", "resolved"}
        or value["lifecycle"]
        not in {"detected", "wake_sent", "claimed", "resolved", "human_required", "dead_letter"}
        or value["manager_state"] not in {"idle", "leased", "ack_valid", "retry_wait", "failed"}
        or value["notification_state"]
        not in {"none", "pending", "leased", "sent", "failed", "acknowledged"}
    ):
        raise ProjectionError("status incident v2 state is invalid")
    for key, minimum, maximum in (
        ("generation", 1, 10**9),
        ("age_ticks", 1, 10**9),
        ("observed_ticks", 1, 10**9),
        ("attempt_count", 0, 2),
    ):
        if not _bounded_int(value[key], minimum, maximum):
            raise ProjectionError("status incident v2 counter is invalid")
    for key in ("next_attempt_at", "acknowledged_at", "terminal_at"):
        item = value[key]
        if item is not None and (
            not isinstance(item, (int, float)) or isinstance(item, bool) or item < 0
        ):
            raise ProjectionError("status incident v2 timestamp is invalid")


def validate_projection(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _TOP_LEVEL:
        raise ProjectionError("status projection schema is invalid")
    version = value.get("schema_version")
    if version not in {1, 2} or value.get("projection_version") != version:
        raise ProjectionError("status projection version is unsupported")
    supervisor = value.get("supervisor")
    fields = {"mode", "state", "heartbeat_at", "tick_seq", "post_gap"}
    if version == 2:
        fields |= {"compatibility_state", "wake_enabled"}
    if not isinstance(supervisor, dict) or set(supervisor) != fields:
        raise ProjectionError("status supervisor schema is invalid")
    heartbeat = supervisor["heartbeat_at"]
    if (
        supervisor["mode"] != "observe"
        or supervisor["state"] not in {"ok", "degraded", "critical"}
        or not isinstance(supervisor["post_gap"], bool)
        or not isinstance(heartbeat, (int, float))
        or isinstance(heartbeat, bool)
        or heartbeat < 0
        or not _bounded_int(supervisor["tick_seq"])
    ):
        raise ProjectionError("status supervisor values are invalid")
    if version == 2 and (
        supervisor["compatibility_state"] not in {"supported", "unsupported", "unchecked"}
        or not isinstance(supervisor["wake_enabled"], bool)
    ):
        raise ProjectionError("status manager compatibility is invalid")
    missions = value.get("missions")
    incidents = value.get("incidents")
    if not isinstance(missions, list) or len(missions) > 64:
        raise ProjectionError("status missions are invalid")
    if not isinstance(incidents, list) or len(incidents) > 256:
        raise ProjectionError("status incidents are invalid")
    for mission in missions:
        _validate_mission(mission)
    for incident in incidents:
        _validate_incident_v2(incident) if version == 2 else _validate_incident(incident)
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
        parent_info = target.parent.lstat()
        if target.parent.is_symlink() or not stat.S_ISDIR(parent_info.st_mode):
            raise ProjectionError("status projection directory is unsafe")
        if hasattr(os, "getuid") and parent_info.st_uid != os.getuid():
            raise ProjectionError("status projection directory ownership is unsafe")
        if stat.S_IMODE(parent_info.st_mode) & 0o022:
            raise ProjectionError("status projection directory permissions are unsafe")
        if target.exists() or target.is_symlink():
            info = target.lstat()
            if target.is_symlink() or not stat.S_ISREG(info.st_mode):
                raise ProjectionError("status projection must be a regular file")
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ProjectionError("status projection ownership is unsafe")
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ProjectionError("status projection permissions are unsafe")
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
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ProjectionError("status projection ownership is unsafe")
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
