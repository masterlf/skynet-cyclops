"""Bounded read-only adapter for reviewed Hermes cron result protocols."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import stat
import subprocess  # Fixed argv, shell=False, bounded pipes.  # nosec B404
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .adapter import sanitize_environment
from .errors import ValidationError

_RUN_LIMIT = 32
_MAX_STDOUT = 70 * 1024
_MAX_STDERR = 64 * 1024
_MAX_RESPONSE = 64 * 1024
_RUN_FIELDS = frozenset(
    {
        "execution_id",
        "job_id",
        "status",
        "claimed_at",
        "started_at",
        "finished_at",
        "result_available",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "protocol",
        "execution_id",
        "job_id",
        "status",
        "claimed_at",
        "started_at",
        "finished_at",
        "final_response",
        "final_response_sha256",
        "final_response_bytes",
    }
)
_STATUSES = frozenset({"claimed", "running", "completed", "failed", "unknown"})
_OUTCOMES = frozenset({"delivered", "failed", "not_configured", "suppressed"})
_SAFE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")


@dataclass(frozen=True, slots=True)
class CronRun:
    execution_id: str
    job_id: str
    status: str
    claimed_at: float
    started_at: float | None
    finished_at: float | None
    result_available: bool


@dataclass(frozen=True, slots=True)
class CronResult:
    execution_id: str
    job_id: str
    status: str
    claimed_at: float
    started_at: float
    finished_at: float
    final_response: str
    final_response_sha256: str
    final_response_bytes: int
    delivery_outcome: str | None


@dataclass(frozen=True, slots=True)
class CronCollection:
    runs: tuple[CronRun, ...]
    results: tuple[CronResult, ...]
    complete: bool


def _duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("Hermes result JSON contains a duplicate key")
        result[key] = value
    return result


def _constant(_value: str) -> object:
    raise ValidationError("Hermes result JSON contains a non-finite number")


def _json_document(raw: str) -> object:
    if any(ord(character) < 32 and character not in "\r\n\t" for character in raw):
        raise ValidationError("Hermes result JSON contains control characters")
    try:
        decoder = json.JSONDecoder(object_pairs_hook=_duplicates, parse_constant=_constant)
        value, end = decoder.raw_decode(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("Hermes result JSON is malformed") from exc
    if raw[end:].strip():
        raise ValidationError("Hermes result output contains multiple documents")
    return value


def _identifier(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value[0] not in _SAFE_ID_CHARS
        or any(character not in _SAFE_ID_CHARS for character in value)
    ):
        raise ValidationError(f"Hermes result {field} is invalid")
    return value


def _timestamp(value: object, field: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValidationError(f"Hermes result {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValidationError(f"Hermes result {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError(f"Hermes result {field} is invalid")
    return parsed.timestamp()


def _canonical_home(path: Path) -> Path:
    candidate = Path(path)
    owner = os.getuid() if hasattr(os, "getuid") else None
    try:
        info = candidate.lstat()
        if (
            not candidate.is_absolute()
            or candidate.name != ".hermes"
            or candidate.resolve(strict=True) != candidate
            or candidate.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or (owner is not None and info.st_uid != owner)
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValidationError("Hermes result home is unsafe")
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("Hermes result home is unavailable") from exc
    return candidate


class HermesCronResultAdapter:
    """Collect an exact job's bounded run index and available terminal results."""

    def __init__(
        self,
        binary: str = "hermes",
        *,
        hermes_home: Path,
        timeout_seconds: int = 10,
        collection_timeout_seconds: int = 30,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if (
            not isinstance(binary, str)
            or not binary
            or len(binary) > 4096
            or any(ord(character) < 32 for character in binary)
        ):
            raise ValidationError("Hermes result binary is invalid")
        if not 1 <= timeout_seconds <= 10 or not 1 <= collection_timeout_seconds <= 30:
            raise ValidationError("Hermes result adapter bounds are invalid")
        if os.path.sep in binary:
            candidate = Path(binary)
            try:
                info = candidate.lstat()
                if (
                    candidate.is_symlink()
                    or not stat.S_ISREG(info.st_mode)
                    or not os.access(candidate, os.X_OK)
                ):
                    raise ValidationError("Hermes result binary is unsafe")
            except ValidationError:
                raise
            except OSError as exc:
                raise ValidationError("Hermes result binary is unavailable") from exc
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.collection_timeout_seconds = collection_timeout_seconds
        self.environment = sanitize_environment(environment)
        self.environment["HERMES_HOME"] = str(_canonical_home(hermes_home))

    def _run(self, arguments: tuple[str, ...], *, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValidationError("Hermes result collection deadline exceeded")
        try:
            process = subprocess.Popen(  # noqa: S603
                [self.binary, *arguments],
                shell=False,  # Fixed adapter-owned argv.  # nosec B603
                env=self.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ValidationError("Hermes result command is unavailable") from exc
        if process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise ValidationError("Hermes result command pipes are unavailable")
        stdout_pipe = process.stdout
        stderr_pipe = process.stderr
        selector = selectors.DefaultSelector()
        selector.register(stdout_pipe, selectors.EVENT_READ, ("stdout", _MAX_STDOUT))
        selector.register(stderr_pipe, selectors.EVENT_READ, ("stderr", _MAX_STDERR))
        buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        command_deadline = min(deadline, time.monotonic() + self.timeout_seconds)
        try:
            while selector.get_map():
                wait = command_deadline - time.monotonic()
                if wait <= 0:
                    process.kill()
                    raise ValidationError("Hermes result collection timed out")
                events = selector.select(wait)
                if not events:
                    process.kill()
                    raise ValidationError("Hermes result collection timed out")
                for key, _mask in events:
                    stream_name, maximum = key.data
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[stream_name].extend(chunk)
                    if len(buffers[stream_name]) > maximum:
                        process.kill()
                        raise ValidationError("Hermes result command exceeded its output bound")
            returncode = process.wait(timeout=max(0.01, command_deadline - time.monotonic()))
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        try:
            stdout = buffers["stdout"].decode("utf-8")
            buffers["stderr"].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("Hermes result command returned invalid UTF-8") from exc
        if returncode != 0:
            raise ValidationError("Hermes result command failed")
        return stdout

    def parse_runs(self, raw: str, *, expected_job_id: str) -> tuple[CronRun, ...]:
        job_id = _identifier(expected_job_id, "job id")
        value = _json_document(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"protocol", "job_id", "limit", "runs"}
            or value.get("protocol") != "hermes-cron-runs/v1"
            or value.get("job_id") != job_id
            or value.get("limit") != _RUN_LIMIT
            or not isinstance(value.get("runs"), list)
            or len(value["runs"]) > _RUN_LIMIT
        ):
            raise ValidationError("Hermes runs envelope is invalid")
        runs: list[CronRun] = []
        identities: set[str] = set()
        for item in value["runs"]:
            if not isinstance(item, dict) or set(item) != _RUN_FIELDS:
                raise ValidationError("Hermes run schema is invalid")
            execution_id = _identifier(item["execution_id"], "execution id")
            status = item["status"]
            claimed = _timestamp(item["claimed_at"], "claimed_at")
            started = _timestamp(item["started_at"], "started_at", optional=True)
            finished = _timestamp(item["finished_at"], "finished_at", optional=True)
            available = item["result_available"]
            if claimed is None or (
                execution_id in identities
                or item["job_id"] != job_id
                or status not in _STATUSES
                or type(available) is not bool
                or available is not (status == "completed")
                or (started is not None and started < claimed)
                or (finished is not None and (started is None or finished < started))
                or (status in {"completed", "failed"} and finished is None)
                or (status in {"claimed", "running"} and finished is not None)
            ):
                raise ValidationError("Hermes run lifecycle is invalid")
            identities.add(execution_id)
            runs.append(
                CronRun(execution_id, job_id, str(status), claimed, started, finished, available)
            )
        ordering = [(run.claimed_at, run.execution_id) for run in runs]
        if ordering != sorted(ordering, reverse=True):
            raise ValidationError("Hermes runs are not newest first")
        return tuple(runs)

    def parse_result(
        self, raw: str, *, expected_job_id: str, expected_execution_id: str
    ) -> CronResult:
        job_id = _identifier(expected_job_id, "job id")
        execution_id = _identifier(expected_execution_id, "execution id")
        value = _json_document(raw)
        keys = set(value) if isinstance(value, dict) else set()
        if not isinstance(value, dict) or keys not in {
            _RESULT_FIELDS,
            _RESULT_FIELDS | {"delivery_outcome"},
        }:
            raise ValidationError("Hermes result schema is invalid")
        claimed = _timestamp(value["claimed_at"], "claimed_at")
        started = _timestamp(value["started_at"], "started_at")
        finished = _timestamp(value["finished_at"], "finished_at")
        response = value["final_response"]
        byte_count = value["final_response_bytes"]
        digest = value["final_response_sha256"]
        outcome = value.get("delivery_outcome")
        if (
            value["protocol"] != "hermes-cron-result/v1"
            or value["execution_id"] != execution_id
            or value["job_id"] != job_id
            or value["status"] != "completed"
            or claimed is None
            or started is None
            or finished is None
            or not claimed <= started <= finished
            or not isinstance(response, str)
            or not 1 <= len(response.encode("utf-8")) <= _MAX_RESPONSE
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count != len(response.encode("utf-8"))
            or not isinstance(digest, str)
            or len(digest) != 64
            or digest != hashlib.sha256(response.encode("utf-8")).hexdigest()
            or (outcome is not None and outcome not in _OUTCOMES)
        ):
            raise ValidationError("Hermes result artifact is invalid")
        return CronResult(
            execution_id,
            job_id,
            "completed",
            claimed,
            started,
            finished,
            response,
            digest,
            byte_count,
            outcome if isinstance(outcome, str) else None,
        )

    def collect(self, job_id: str, *, lease_acquired_at: float | None = None) -> CronCollection:
        exact_job_id = _identifier(job_id, "job id")
        deadline = time.monotonic() + self.collection_timeout_seconds
        raw_runs = self._run(
            ("cron", "runs", exact_job_id, "--limit", str(_RUN_LIMIT), "--json"),
            deadline=deadline,
        )
        runs = self.parse_runs(raw_runs, expected_job_id=exact_job_id)
        results: list[CronResult] = []
        for run in runs:
            if not run.result_available:
                continue
            raw_result = self._run(
                ("cron", "result", run.execution_id, "--json"), deadline=deadline
            )
            result = self.parse_result(
                raw_result,
                expected_job_id=exact_job_id,
                expected_execution_id=run.execution_id,
            )
            if (
                result.claimed_at != run.claimed_at
                or result.started_at != run.started_at
                or result.finished_at != run.finished_at
            ):
                raise ValidationError("Hermes result lifecycle does not match its run")
            results.append(result)
        complete = len(runs) < _RUN_LIMIT
        if not complete and lease_acquired_at is not None and runs:
            complete = runs[-1].claimed_at <= lease_acquired_at
        return CronCollection(runs, tuple(results), complete)
