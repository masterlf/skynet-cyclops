"""Private fail-closed manager activation attestation.

The validator is deliberately side-effect free.  Callers must supply current, typed evidence
collected through supported Hermes surfaces; an attestation is never an evidence source.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .errors import ValidationError

ACTIVATION_PROTOCOL = "cyclops-manager-activation/v1"
SEAM_PROTOCOL = "cyclops-hermes-seam-evidence/v1"
RELEASE = "0.2.1"
MAX_ACTIVATION_BYTES = 16 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_TASK_SCOPE_MARKERS = frozenset(
    {
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_BRANCH",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_DELEGATED_CHILD",
        "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_DELEGATION_PARENT",
    }
)
_JOB_DEFINITION_FIELDS = frozenset(
    {
        "job_id",
        "name",
        "schedule",
        "repeat",
        "deliver",
        "skills",
        "model",
        "provider",
        "base_url",
        "prompt_sha256",
        "script",
        "monitor_script",
        "monitor_url",
        "no_agent",
        "enabled_toolsets",
        "workdir",
        "continuity",
        "context_from",
        "attach_to_session",
    }
)
_RECORD_KEYS = frozenset(
    {
        "protocol",
        "schema_version",
        "state",
        "release",
        "profile",
        "activated_at",
        "deactivated_at",
        "hermes_version",
        "manager_install_spec_sha256",
        "seam_evidence_protocol",
        "seam_evidence_sha256",
        "jobs",
    }
)
_REASONS = Literal[
    "absent",
    "disabled",
    "unsafe_file",
    "malformed",
    "release_drift",
    "profile_drift",
    "hermes_drift",
    "spec_drift",
    "script_drift",
    "job_missing",
    "job_identity_drift",
    "job_definition_drift",
    "prompt_unverifiable",
    "seam_protocol_drift",
    "seam_evidence_drift",
    "supported",
]


@dataclass(frozen=True, slots=True)
class ActivationInputs:
    activation_path: Path
    release: str
    profile: str
    hermes_version: str
    install_spec: Mapping[str, object]
    scripts: Mapping[str, bytes]
    jobs: Mapping[str, Mapping[str, object]]
    seam_evidence: Mapping[str, object]
    evidence_collected_at: str | None = None
    validated_at: str | None = None


@dataclass(frozen=True, slots=True)
class ActivationVerdict:
    reason: _REASONS
    compatibility_state: Literal["unchecked", "unsupported", "supported"]
    wake_enabled: bool

    @property
    def public(self) -> tuple[str, bool]:
        return self.compatibility_state, self.wake_enabled


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _normalized_job(value: Mapping[str, object]) -> dict[str, object] | None:
    if set(value) != _JOB_DEFINITION_FIELDS | {"state", "enabled"}:
        return None
    normalized = {key: value[key] for key in sorted(_JOB_DEFINITION_FIELDS)}
    if (
        not isinstance(normalized["job_id"], str)
        or not _SAFE_ID.fullmatch(str(normalized["job_id"]))
        or not isinstance(normalized["name"], str)
        or not _SAFE_ID.fullmatch(str(normalized["name"]))
        or not isinstance(normalized["prompt_sha256"], str)
        or not _HEX_64.fullmatch(str(normalized["prompt_sha256"]))
        or type(normalized["no_agent"]) is not bool
        or type(normalized["continuity"]) is not bool
        or type(normalized["attach_to_session"]) is not bool
        or not isinstance(normalized["skills"], list)
        or not isinstance(normalized["enabled_toolsets"], list)
        or not isinstance(normalized["context_from"], list)
    ):
        return None
    return normalized


def job_definition_sha256(value: Mapping[str, object]) -> str:
    """Hash exact authorization/execution fields, excluding operational pause state."""
    normalized = _normalized_job(value)
    if normalized is None:
        raise ValidationError("full job definition evidence is invalid")
    return canonical_sha256(normalized)


def _scripts_match_spec(inputs: ActivationInputs) -> bool:
    artifacts = inputs.install_spec.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return False
    expected: dict[str, str] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            return False
        name = artifact.get("name")
        digest = artifact.get("sha256")
        if (
            not isinstance(name, str)
            or not name
            or name in expected
            or not isinstance(digest, str)
            or not _HEX_64.fullmatch(digest)
        ):
            return False
        expected[name] = digest
    return set(inputs.scripts) == set(expected) and all(
        isinstance(inputs.scripts[name], bytes)
        and hashlib.sha256(inputs.scripts[name]).hexdigest() == digest
        for name, digest in expected.items()
    )


def _expected_job(arguments: Mapping[str, object], job_id: str) -> dict[str, object]:
    prompt = arguments.get("prompt")
    if not isinstance(prompt, str):
        raise ValidationError("activation full prompt is unverifiable")
    return {
        "job_id": job_id,
        "name": arguments.get("name"),
        "schedule": arguments.get("schedule"),
        "repeat": "forever" if arguments.get("repeat") == 0 else arguments.get("repeat"),
        "deliver": arguments.get("deliver"),
        "skills": arguments.get("skills"),
        "model": arguments.get("model"),
        "provider": arguments.get("provider"),
        "base_url": arguments.get("base_url"),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "script": arguments.get("script"),
        "monitor_script": arguments.get("monitor_script"),
        "monitor_url": arguments.get("monitor_url"),
        "no_agent": arguments.get("no_agent", False),
        "enabled_toolsets": arguments.get("enabled_toolsets", []),
        "workdir": arguments.get("workdir"),
        "continuity": arguments.get("continuity", False),
        "context_from": arguments.get("context_from", []),
        "attach_to_session": arguments.get("attach_to_session", False),
        "state": "paused",
        "enabled": False,
    }


def _verdict(reason: _REASONS) -> ActivationVerdict:
    if reason == "absent":
        return ActivationVerdict(reason, "unchecked", False)
    if reason == "supported":
        return ActivationVerdict(reason, "supported", True)
    if reason == "disabled":
        return ActivationVerdict(reason, "supported", False)
    return ActivationVerdict(reason, "unsupported", False)


def _evidence_is_fresh(inputs: ActivationInputs) -> bool:
    if inputs.evidence_collected_at is None and inputs.validated_at is None:
        return True
    if inputs.evidence_collected_at is None or inputs.validated_at is None:
        return False
    try:
        collected = datetime.strptime(inputs.evidence_collected_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        validated = datetime.strptime(inputs.validated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    age = (validated - collected).total_seconds()
    return 0 <= age <= 300


def _duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _safe_activation_file(path: Path) -> bool:
    try:
        parent = path.parent.lstat()
        info = path.lstat()
    except OSError:
        return False
    owner = os.getuid() if hasattr(os, "getuid") else info.st_uid
    return (
        stat.S_ISDIR(parent.st_mode)
        and not path.parent.is_symlink()
        and parent.st_uid == owner
        and stat.S_IMODE(parent.st_mode) & 0o077 == 0
        and stat.S_ISREG(info.st_mode)
        and not path.is_symlink()
        and info.st_uid == owner
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1
        and info.st_size <= MAX_ACTIVATION_BYTES
    )


def _read_record(path: Path) -> tuple[dict[str, object] | None, _REASONS | None]:
    try:
        path.lstat()
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unsafe_file"
    if not _safe_activation_file(path):
        return None, "unsafe_file"
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_ACTIVATION_BYTES or any(
            byte < 32 for byte in raw if byte not in b"\r\n\t"
        ):
            return None, "malformed"
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None, "malformed"
    if not isinstance(value, dict):
        return None, "malformed"
    return value, None


def _binding(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict) or set(value) != {"job_id", "definition_sha256"}:
        return None
    job_id = value.get("job_id")
    digest = value.get("definition_sha256")
    if not isinstance(job_id, str) or not _SAFE_ID.fullmatch(job_id):
        return None
    if not isinstance(digest, str) or not _HEX_64.fullmatch(digest):
        return None
    return job_id, digest


def _record_is_structural(value: Mapping[str, object]) -> bool:
    if set(value) != _RECORD_KEYS:
        return False
    if (
        value.get("protocol") != ACTIVATION_PROTOCOL
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or value.get("state") not in {"enabled", "disabled"}
        or not isinstance(value.get("release"), str)
        or value.get("profile") != "default"
    ):
        return False
    activated = value.get("activated_at")
    deactivated = value.get("deactivated_at")
    state = value["state"]
    if state == "enabled":
        if (
            not isinstance(activated, str)
            or not _TIMESTAMP.fullmatch(activated)
            or deactivated is not None
        ):
            return False
    elif not isinstance(deactivated, str) or not _TIMESTAMP.fullmatch(deactivated):
        return False
    jobs = value.get("jobs")
    bindings = (
        isinstance(jobs, dict)
        and set(jobs) == {"router", "courier"}
        and _binding(jobs["router"]) is not None
        and _binding(jobs["courier"]) is not None
        and _binding(jobs["router"])[0] != _binding(jobs["courier"])[0]  # type: ignore[index]
    )
    all_null = (
        state == "disabled"
        and activated is None
        and all(
            value.get(key) is None
            for key in (
                "hermes_version",
                "manager_install_spec_sha256",
                "seam_evidence_protocol",
                "seam_evidence_sha256",
            )
        )
        and jobs == {"router": None, "courier": None}
    )
    if all_null:
        return True
    if not all(
        isinstance(value.get(key), str)
        for key in (
            "hermes_version",
            "manager_install_spec_sha256",
            "seam_evidence_protocol",
            "seam_evidence_sha256",
        )
    ):
        return False
    if not _HEX_64.fullmatch(str(value["manager_install_spec_sha256"])) or not _HEX_64.fullmatch(
        str(value["seam_evidence_sha256"])
    ):
        return False
    return bindings


def activation_verdict(inputs: ActivationInputs) -> ActivationVerdict:
    """Validate a private attestation against one current evidence snapshot."""
    record, failure = _read_record(inputs.activation_path)
    if failure is not None:
        return _verdict(failure)
    if record is None:
        return _verdict("malformed")
    if not _record_is_structural(record):
        return _verdict("malformed")
    if record["release"] != inputs.release or inputs.release != RELEASE:
        return _verdict("release_drift")
    if record["profile"] != inputs.profile or inputs.profile != "default":
        return _verdict("profile_drift")
    if record["state"] == "disabled":
        return _verdict("disabled")
    if record["hermes_version"] != inputs.hermes_version:
        return _verdict("hermes_drift")
    if not _evidence_is_fresh(inputs):
        return _verdict("seam_evidence_drift")
    if record["manager_install_spec_sha256"] != canonical_sha256(inputs.install_spec):
        return _verdict("spec_drift")
    if not _scripts_match_spec(inputs):
        return _verdict("script_drift")
    if (
        record["seam_evidence_protocol"] != SEAM_PROTOCOL
        or inputs.seam_evidence.get("protocol") != SEAM_PROTOCOL
    ):
        return _verdict("seam_protocol_drift")
    if record["seam_evidence_sha256"] != canonical_sha256(inputs.seam_evidence):
        return _verdict("seam_evidence_drift")
    jobs = record["jobs"]
    if not isinstance(jobs, dict):
        return _verdict("malformed")
    for name in ("router", "courier"):
        current = inputs.jobs.get(name)
        if current is None:
            return _verdict("job_missing")
        binding = _binding(jobs[name])
        if binding is None:
            return _verdict("malformed")
        if current.get("job_id") != binding[0]:
            return _verdict("job_identity_drift")
        if not isinstance(current.get("prompt_sha256"), str):
            return _verdict("prompt_unverifiable")
        try:
            current_digest = job_definition_sha256(current)
        except ValidationError:
            return _verdict("job_definition_drift")
        if current_digest != binding[1]:
            return _verdict("job_definition_drift")
    return _verdict("supported")


def _read_private_json(path: Path, *, maximum_bytes: int) -> object:
    try:
        info = path.lstat()
        owner = os.getuid() if hasattr(os, "getuid") else info.st_uid
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > maximum_bytes
        ):
            raise ValidationError("activation input file is unsafe")
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_duplicates)
    except ValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError("activation input file is unavailable") from exc
    return value


def load_activation_inputs(
    *, activation_path: Path, hermes_home: Path, evidence_path: Path
) -> ActivationInputs:
    """Load current private evidence without opening Hermes or Kanban stores."""
    from .manager_install import seam_evidence_is_valid, validate_cron_install_spec

    evidence = _read_private_json(evidence_path, maximum_bytes=MAX_EVIDENCE_BYTES)
    expected_keys = {
        "protocol",
        "source",
        "collected_at",
        "profile",
        "hermes_version",
        "jobs",
        "seam_evidence",
    }
    if (
        not isinstance(evidence, dict)
        or set(evidence) != expected_keys
        or evidence.get("protocol") != "cyclops-manager-current-evidence/v1"
        or evidence.get("source") != "supported-full-definition-api"
        or evidence.get("profile") != "default"
        or not isinstance(evidence.get("hermes_version"), str)
        or not isinstance(evidence.get("jobs"), dict)
        or set(evidence["jobs"]) != {"router", "courier"}
        or not seam_evidence_is_valid(evidence.get("seam_evidence"))
    ):
        raise ValidationError("activation evidence schema is invalid")
    root = Path(hermes_home)
    if not root.is_absolute() or root.name != "default" or root.parent.name != "profiles":
        raise ValidationError("activation Hermes profile is noncanonical")
    spec_value = _read_private_json(
        root / "cyclops" / "manager-install.json", maximum_bytes=MAX_EVIDENCE_BYTES
    )
    spec = validate_cron_install_spec(spec_value)
    artifacts = spec.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValidationError("activation install spec is invalid")
    scripts: dict[str, bytes] = {}
    for artifact in artifacts:
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("name"), str):
            raise ValidationError("activation install artifacts are invalid")
        name = str(artifact["name"])
        script_path = root / "scripts" / name
        try:
            info = script_path.lstat()
            if (
                script_path.is_symlink()
                or not stat.S_ISREG(info.st_mode)
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
                or info.st_size > MAX_EVIDENCE_BYTES
            ):
                raise ValidationError("activation staged script is unsafe")
            scripts[name] = script_path.read_bytes()
        except OSError as exc:
            raise ValidationError("activation staged script is unavailable") from exc
    jobs = evidence["jobs"]
    if not isinstance(jobs, dict):
        raise ValidationError("activation evidence jobs are invalid")
    return ActivationInputs(
        activation_path=activation_path,
        release=RELEASE,
        profile="default",
        hermes_version=str(evidence["hermes_version"]),
        install_spec=spec,
        scripts=scripts,
        jobs={name: dict(value) for name, value in jobs.items() if isinstance(value, Mapping)},
        seam_evidence=dict(evidence["seam_evidence"]),
        evidence_collected_at=str(evidence["collected_at"]),
        validated_at=datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _timestamp(value: object) -> str:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise ValidationError("activation timestamp is invalid")
    return value


def _private_parent(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValidationError("activation directory is unavailable") from exc
    owner = os.getuid() if hasattr(os, "getuid") else info.st_uid
    if (
        path.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ValidationError("activation directory is unsafe")


def _preflight_target(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ValidationError("activation target is unavailable") from exc
    owner = os.getuid() if hasattr(os, "getuid") else info.st_uid
    if (
        path.is_symlink()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != owner
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        raise ValidationError("activation target is unsafe")


@contextmanager
def _activation_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValidationError("activation lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise ValidationError("activation lock is unavailable") from exc
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def _atomic_private_write(path: Path, value: Mapping[str, object]) -> None:
    _preflight_target(path)
    payload = canonical_bytes(value)
    if len(payload) > MAX_ACTIVATION_BYTES:
        raise ValidationError("activation record exceeds its bound")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _enabled_record(inputs: ActivationInputs, now: str) -> dict[str, object]:
    from .manager_install import seam_evidence_is_valid, validate_cron_install_spec

    if (
        inputs.release != RELEASE
        or inputs.profile != "default"
        or not inputs.hermes_version
        or inputs.seam_evidence.get("protocol") != SEAM_PROTOCOL
        or set(inputs.jobs) != {"router", "courier"}
    ):
        raise ValidationError("activation evidence is unsupported")
    if not _scripts_match_spec(inputs):
        raise ValidationError("activation script evidence is invalid")
    if not _evidence_is_fresh(inputs):
        raise ValidationError("activation evidence is stale")
    try:
        spec = validate_cron_install_spec(dict(inputs.install_spec))
    except ValidationError as exc:
        raise ValidationError("activation install spec is invalid") from exc
    if not seam_evidence_is_valid(inputs.seam_evidence):
        raise ValidationError("activation seam evidence is invalid")
    operations = spec.get("operations")
    if not isinstance(operations, list) or len(operations) != 2:
        raise ValidationError("activation install definitions are invalid")
    bindings: dict[str, dict[str, str]] = {}
    identifiers: set[str] = set()
    for index, name in enumerate(("router", "courier")):
        job = inputs.jobs[name]
        job_id = job.get("job_id")
        prompt_digest = job.get("prompt_sha256")
        if (
            not isinstance(job_id, str)
            or not _SAFE_ID.fullmatch(job_id)
            or job_id in identifiers
            or not isinstance(prompt_digest, str)
            or not _HEX_64.fullmatch(prompt_digest)
        ):
            raise ValidationError("activation job evidence is invalid")
        if job.get("state") != "paused" or job.get("enabled") is not False:
            raise ValidationError("activation requires paused jobs")
        operation = operations[index]
        if not isinstance(operation, Mapping) or not isinstance(
            operation.get("arguments"), Mapping
        ):
            raise ValidationError("activation install definitions are invalid")
        expected_job = _expected_job(operation["arguments"], job_id)
        if job_definition_sha256(job) != job_definition_sha256(expected_job):
            raise ValidationError("activation job definition does not match install spec")
        identifiers.add(job_id)
        bindings[name] = {"job_id": job_id, "definition_sha256": job_definition_sha256(job)}
    return {
        "protocol": ACTIVATION_PROTOCOL,
        "schema_version": 1,
        "state": "enabled",
        "release": RELEASE,
        "profile": "default",
        "activated_at": _timestamp(now),
        "deactivated_at": None,
        "hermes_version": inputs.hermes_version,
        "manager_install_spec_sha256": canonical_sha256(inputs.install_spec),
        "seam_evidence_protocol": SEAM_PROTOCOL,
        "seam_evidence_sha256": canonical_sha256(inputs.seam_evidence),
        "jobs": bindings,
    }


def _public(mode: str, *, wake_enabled: bool) -> dict[str, object]:
    return {"mode": mode, "compatibility_state": "supported", "wake_enabled": wake_enabled}


def activate_manager(
    inputs: ActivationInputs,
    *,
    now: str,
    apply: bool = False,
    environment: Mapping[str, str] | None = None,
    refresh: Callable[[], ActivationInputs] | None = None,
) -> dict[str, object]:
    """Plan or atomically commit activation using the supplied current typed evidence."""
    env = os.environ if environment is None else environment
    if any(marker in env for marker in _TASK_SCOPE_MARKERS):
        raise ValidationError("manager activation is forbidden in task scope")
    record = _enabled_record(inputs, now)
    if not apply:
        return _public("dry-run", wake_enabled=True)
    if refresh is None:
        raise ValidationError("activation apply requires current evidence recollection")
    _private_parent(inputs.activation_path.parent)
    with _activation_lock(inputs.activation_path.with_suffix(".lock")):
        current = refresh()
        if current.activation_path != inputs.activation_path:
            raise ValidationError("activation refresh target changed")
        record = _enabled_record(current, now)
        _atomic_private_write(current.activation_path, record)
        if activation_verdict(current).reason != "supported":
            raise ValidationError("activation readback failed")
    return _public("applied", wake_enabled=True)


def deactivate_manager(
    path: Path,
    *,
    now: str,
    apply: bool = False,
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Plan or atomically replace the activation record with a durable deny."""
    env = os.environ if environment is None else environment
    if any(marker in env for marker in _TASK_SCOPE_MARKERS):
        raise ValidationError("manager deactivation is forbidden in task scope")
    timestamp = _timestamp(now)
    if not apply:
        return _public("dry-run", wake_enabled=False)
    _private_parent(path.parent)
    with _activation_lock(path.with_suffix(".lock")):
        existing, failure = _read_record(path)
        if failure not in {None, "absent"}:
            raise ValidationError("activation record is unsafe")
        if existing is not None and not _record_is_structural(existing):
            raise ValidationError("activation record is malformed")
        if existing is None:
            jobs: object = {"router": None, "courier": None}
            record: dict[str, object] = {
                "protocol": ACTIVATION_PROTOCOL,
                "schema_version": 1,
                "state": "disabled",
                "release": RELEASE,
                "profile": "default",
                "activated_at": None,
                "deactivated_at": timestamp,
                "hermes_version": None,
                "manager_install_spec_sha256": None,
                "seam_evidence_protocol": None,
                "seam_evidence_sha256": None,
                "jobs": jobs,
            }
        else:
            record = dict(existing)
            record.update(state="disabled", deactivated_at=timestamp)
        _atomic_private_write(path, record)
    return _public("applied", wake_enabled=False)
