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
import subprocess  # Fixed read-only Hermes argv with shell=False.  # nosec B404
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .adapter import sanitize_environment
from .errors import ValidationError

ACTIVATION_PROTOCOL = "cyclops-manager-activation/v1"
SEAM_PROTOCOL = "cyclops-hermes-seam-evidence/v1"
RELEASE = "0.2.1"
MAX_ACTIVATION_BYTES = 16 * 1024
MAX_EVIDENCE_BYTES = 256 * 1024
MAX_HERMES_READBACK_BYTES = 1024 * 1024
HERMES_READBACK_TIMEOUT_SECONDS = 10
HERMES_READBACK_COLLECTION_SECONDS = 30
HERMES_DEFINITION_PROTOCOL = "hermes-cron-definition/v1"
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_VERSION_LINE = re.compile(
    r"^Hermes Agent v([A-Za-z0-9][A-Za-z0-9.+-]{0,63})(?: \([^\r\n]{1,64}\))?$"
)
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
        "provider_snapshot",
        "model_snapshot",
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
_HERMES_DEFINITION_FIELDS = frozenset(
    {
        "name",
        "prompt",
        "schedule",
        "repeat",
        "skills",
        "model",
        "provider",
        "provider_snapshot",
        "model_snapshot",
        "base_url",
        "script",
        "no_agent",
        "monitor_script",
        "monitor_url",
        "context_from",
        "enabled_toolsets",
        "workdir",
        "attach_to_session",
        "continuity",
        "deliver",
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


def _canonical_private_profile_home(path: Path) -> Path:
    root = Path(path)
    if (
        not root.is_absolute()
        or root.name != "default"
        or root.parent.name != "profiles"
        or root.parent.parent.name != ".hermes"
    ):
        raise ValidationError("activation Hermes profile is noncanonical")
    owner = os.getuid() if hasattr(os, "getuid") else None
    try:
        if root.resolve(strict=True) != root:
            raise ValidationError("activation Hermes profile is unsafe")
        for directory in (root.parent.parent, root.parent, root):
            info = directory.lstat()
            if (
                directory.is_symlink()
                or not stat.S_ISDIR(info.st_mode)
                or (owner is not None and info.st_uid != owner)
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise ValidationError("activation Hermes profile is unsafe")
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("activation Hermes profile is unavailable") from exc
    return root


class HermesCronDefinitionAdapter:
    """Collect only version and exact cron definitions through supported CLI calls."""

    def __init__(
        self,
        binary: str = "hermes",
        *,
        hermes_home: Path,
        timeout_seconds: int = HERMES_READBACK_TIMEOUT_SECONDS,
        collection_timeout_seconds: int = HERMES_READBACK_COLLECTION_SECONDS,
        max_output_bytes: int = MAX_HERMES_READBACK_BYTES,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not binary or len(binary) > 4096 or "\x00" in binary:
            raise ValidationError("Hermes definition reader binary is invalid")
        if (
            not 1 <= timeout_seconds <= 30
            or not 1 <= collection_timeout_seconds <= 60
            or not 1024 <= max_output_bytes <= 4 * 1024 * 1024
        ):
            raise ValidationError("Hermes definition reader bounds are invalid")
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.collection_timeout_seconds = collection_timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.environment = sanitize_environment(environment)
        self.environment["HERMES_HOME"] = str(_canonical_private_profile_home(hermes_home))

    def _run(self, arguments: tuple[str, ...], *, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValidationError("Hermes definition collection deadline exceeded")
        try:
            completed = subprocess.run(  # noqa: S603
                [self.binary, *arguments],
                shell=False,  # Fixed adapter-owned argv; never a shell.  # nosec B603
                env=self.environment,
                capture_output=True,
                text=False,
                timeout=min(float(self.timeout_seconds), remaining),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValidationError("Hermes definition collection timed out") from exc
        except OSError as exc:
            raise ValidationError("Hermes definition command is unavailable") from exc
        stdout = completed.stdout if isinstance(completed.stdout, bytes) else b""
        stderr = completed.stderr if isinstance(completed.stderr, bytes) else b""
        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            raise ValidationError("Hermes definition command exceeded its output bound")
        try:
            output = stdout.decode("utf-8")
            stderr.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("Hermes definition command returned invalid UTF-8") from exc
        if completed.returncode != 0:
            raise ValidationError("Hermes definition command failed")
        return output

    def collect(self, job_ids: Mapping[str, str]) -> tuple[str, dict[str, dict[str, object]]]:
        if set(job_ids) != {"router", "courier"}:
            raise ValidationError("Hermes definition identities are incomplete")
        safe_ids: dict[str, str] = {}
        for role in ("router", "courier"):
            job_id = job_ids[role]
            if not isinstance(job_id, str) or not _SAFE_ID.fullmatch(job_id):
                raise ValidationError("Hermes definition identity is invalid")
            safe_ids[role] = job_id
        if safe_ids["router"] == safe_ids["courier"]:
            raise ValidationError("Hermes definition identities must be distinct")
        deadline = time.monotonic() + self.collection_timeout_seconds
        version_output = self._run(("--version",), deadline=deadline)
        lines = version_output.splitlines()
        version_match = _VERSION_LINE.fullmatch(lines[0] if lines else "")
        if version_match is None:
            raise ValidationError("Hermes version output is unsupported")
        jobs: dict[str, dict[str, object]] = {}
        for role in ("router", "courier"):
            raw = self._run(("cron", "show", safe_ids[role], "--json"), deadline=deadline)
            try:
                value = json.loads(raw, object_pairs_hook=_duplicates)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValidationError("Hermes definition JSON is malformed") from exc
            jobs[role] = _normalize_hermes_definition(value, expected_job_id=safe_ids[role])
        return version_match.group(1), jobs


def _bounded_text_list(value: object) -> list[str] | None:
    if not isinstance(value, list) or len(value) > 32:
        return None
    result: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or len(item.encode("utf-8")) > 4096
            or any(ord(character) < 32 for character in item)
            or item in result
        ):
            return None
        result.append(item)
    return result


def _optional_text(value: object) -> str | Literal[False] | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value.encode("utf-8")) > 4096
        or any(ord(character) < 32 for character in value)
    ):
        return False
    return value


def _normalize_schedule(value: object) -> object | None:
    if not isinstance(value, dict) or not isinstance(value.get("kind"), str):
        return None
    kind = value["kind"]
    if kind == "interval" and set(value) == {"kind", "minutes"}:
        minutes = value["minutes"]
        if isinstance(minutes, int) and not isinstance(minutes, bool) and 1 <= minutes <= 525600:
            return f"every {minutes}m"
    elif kind == "cron" and set(value) == {"kind", "expr"}:
        expression = _optional_text(value["expr"])
        if isinstance(expression, str) and expression:
            return expression
    elif kind == "once" and set(value) == {"kind", "run_at"}:
        run_at = _optional_text(value["run_at"])
        if isinstance(run_at, str) and run_at:
            return run_at
    elif kind == "legacy" and set(value) == {"kind", "value"}:
        legacy = _optional_text(value["value"])
        if isinstance(legacy, str) and legacy:
            return legacy
    return None


def _normalize_hermes_definition(value: object, *, expected_job_id: str) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value) != {"protocol", "job_id", "effective_state", "definition"}
        or value.get("protocol") != HERMES_DEFINITION_PROTOCOL
        or value.get("job_id") != expected_job_id
        or value.get("effective_state") not in {"paused", "scheduled"}
        or not isinstance(value.get("definition"), dict)
    ):
        raise ValidationError("Hermes definition envelope is unsupported")
    definition = value["definition"]
    if not isinstance(definition, dict) or set(definition) != _HERMES_DEFINITION_FIELDS:
        raise ValidationError("Hermes definition schema is unsupported")
    name = definition["name"]
    prompt = definition["prompt"]
    schedule = _normalize_schedule(definition["schedule"])
    repeat = definition["repeat"]
    deliveries = _bounded_text_list(definition["deliver"])
    skills = _bounded_text_list(definition["skills"])
    enabled_toolsets = _bounded_text_list(definition["enabled_toolsets"])
    context_from = _bounded_text_list(definition["context_from"])
    optional = {
        field: _optional_text(definition[field])
        for field in (
            "model",
            "provider",
            "provider_snapshot",
            "model_snapshot",
            "base_url",
            "script",
            "monitor_script",
            "monitor_url",
            "workdir",
        )
    }
    times = repeat.get("times") if isinstance(repeat, dict) and set(repeat) == {"times"} else False
    if (
        not isinstance(name, str)
        or not _SAFE_ID.fullmatch(name)
        or not isinstance(prompt, str)
        or len(prompt.encode("utf-8")) > MAX_HERMES_READBACK_BYTES
        or schedule is None
        or times is False
        or not (
            times is None or (isinstance(times, int) and not isinstance(times, bool) and times >= 1)
        )
        or skills is None
        or enabled_toolsets is None
        or context_from is None
        or deliveries is None
        or any(item is False for item in optional.values())
        or type(definition["no_agent"]) is not bool
        or type(definition["continuity"]) is not bool
        or type(definition["attach_to_session"]) is not bool
    ):
        raise ValidationError("Hermes definition fields are unsupported")
    return {
        "job_id": expected_job_id,
        "name": name,
        "schedule": schedule,
        "repeat": "forever" if times is None else times,
        "deliver": deliveries,
        "skills": skills,
        **optional,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "no_agent": definition["no_agent"],
        "enabled_toolsets": enabled_toolsets,
        "continuity": definition["continuity"],
        "context_from": context_from,
        "attach_to_session": definition["attach_to_session"],
        "state": value["effective_state"],
        "enabled": value["effective_state"] == "scheduled",
    }


def _normalized_job(value: Mapping[str, object]) -> dict[str, object] | None:
    if set(value) != _JOB_DEFINITION_FIELDS | {"state", "enabled"}:
        return None
    normalized = {key: value[key] for key in sorted(_JOB_DEFINITION_FIELDS)}
    optional = (
        "model",
        "provider",
        "provider_snapshot",
        "model_snapshot",
        "base_url",
        "script",
        "monitor_script",
        "monitor_url",
        "workdir",
    )
    if (
        not isinstance(normalized["job_id"], str)
        or not _SAFE_ID.fullmatch(str(normalized["job_id"]))
        or not isinstance(normalized["name"], str)
        or not _SAFE_ID.fullmatch(str(normalized["name"]))
        or not isinstance(normalized["schedule"], str)
        or not normalized["schedule"]
        or len(str(normalized["schedule"]).encode("utf-8")) > 4096
        or not (
            normalized["repeat"] == "forever"
            or (
                isinstance(normalized["repeat"], int)
                and not isinstance(normalized["repeat"], bool)
                and normalized["repeat"] >= 1
            )
        )
        or not _bounded_text_list(normalized["deliver"])
        or not isinstance(normalized["prompt_sha256"], str)
        or not _HEX_64.fullmatch(str(normalized["prompt_sha256"]))
        or type(normalized["no_agent"]) is not bool
        or type(normalized["continuity"]) is not bool
        or type(normalized["attach_to_session"]) is not bool
        or _bounded_text_list(normalized["skills"]) is None
        or _bounded_text_list(normalized["enabled_toolsets"]) is None
        or _bounded_text_list(normalized["context_from"]) is None
        or any(_optional_text(normalized[field]) is False for field in optional)
        or value["state"] not in {"paused", "scheduled"}
        or type(value["enabled"]) is not bool
        or value["enabled"] is not (value["state"] == "scheduled")
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
    delivery = arguments.get("deliver")
    if isinstance(delivery, str):
        delivery_classes: list[str] = []
        for target in delivery.split(","):
            normalized_target = target.strip().split(":", 1)[0]
            if normalized_target and normalized_target not in delivery_classes:
                delivery_classes.append(normalized_target)
        delivery = delivery_classes or ["local"]
    return {
        "job_id": job_id,
        "name": arguments.get("name"),
        "schedule": arguments.get("schedule"),
        "repeat": "forever" if arguments.get("repeat") == 0 else arguments.get("repeat"),
        "deliver": delivery,
        "skills": arguments.get("skills"),
        "model": arguments.get("model"),
        "provider": arguments.get("provider"),
        "provider_snapshot": None,
        "model_snapshot": None,
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
    *,
    activation_path: Path,
    hermes_home: Path,
    evidence_path: Path,
    hermes_binary: str = "hermes",
) -> ActivationInputs:
    """Load static local bindings, then recollect current Hermes state through its CLI."""
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
    root = _canonical_private_profile_home(hermes_home)
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
    job_ids: dict[str, str] = {}
    for name in ("router", "courier"):
        job = jobs.get(name)
        job_id = job.get("job_id") if isinstance(job, Mapping) else None
        if not isinstance(job_id, str) or not _SAFE_ID.fullmatch(job_id):
            raise ValidationError("activation evidence job identity is invalid")
        job_ids[name] = job_id
    hermes_version, current_jobs = HermesCronDefinitionAdapter(
        binary=hermes_binary, hermes_home=root
    ).collect(job_ids)
    return ActivationInputs(
        activation_path=activation_path,
        release=RELEASE,
        profile="default",
        hermes_version=hermes_version,
        install_spec=spec,
        scripts=scripts,
        jobs=current_jobs,
        seam_evidence=dict(evidence["seam_evidence"]),
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
        configured_job = dict(job)
        configured_job.update(provider_snapshot=None, model_snapshot=None)
        if job_definition_sha256(configured_job) != job_definition_sha256(expected_job):
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
