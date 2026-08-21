"""Profile-local staging and strict cronjob-tool installation contracts.

This module never imports Hermes or opens a Hermes cron store.  It writes only private
script/config artifacts and emits operations for the supported profile-local ``cronjob`` tool.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import subprocess  # Fixed argv, shell=False, sanitized child environment.  # nosec B404
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from .errors import ValidationError
from .manager import MANAGER_PROMPT, MANAGER_PROMPT_V0_2_2

_PROTOCOL = "cyclops-cron-install/v1"
_RELEASE = "0.3.0"
_PRIOR_RELEASE = "0.2.2"
_STABLE_NAMES = ("cyclops-manager-router", "cyclops-decision-courier")
_HEX = frozenset("0123456789abcdef")
_SPEC_KEYS = frozenset(
    {
        "protocol",
        "release",
        "attempt_nonce",
        "profile",
        "operation",
        "snapshot",
        "snapshot_sha256",
        "artifacts",
        "operations",
        "verification",
        "rollback",
    }
)
_VISIBLE_REQUIRED = frozenset(
    {
        "job_id",
        "name",
        "skill",
        "skills",
        "prompt_preview",
        "model",
        "provider",
        "base_url",
        "schedule",
        "repeat",
        "deliver",
        "next_run_at",
        "last_run_at",
        "last_status",
        "last_delivery_error",
        "last_fire_error",
        "enabled",
        "state",
        "paused_at",
        "paused_reason",
    }
)
_VISIBLE_OPTIONAL = frozenset(
    {
        "script",
        "monitor_script",
        "monitor_url",
        "monitor_state",
        "no_agent",
        "enabled_toolsets",
        "workdir",
        "continuity",
        "context_from",
    }
)

CronTool = Callable[..., object]
SeamVerifier = Callable[[], Mapping[str, object]]
FullJobReader = Callable[[str], object]

_SEAM_EVIDENCE_KEYS = frozenset(
    {
        "protocol",
        "canonical_profile",
        "configured_toolsets",
        "cronjob_full_field_readback",
        "courier_empty_is_silent",
        "disposable_profile",
        "non_task_scoped",
        "quiet_agent_calls",
        "resolved_tools",
        "resolved_toolsets",
    }
)
_VERIFIER_OUTPUT_LIMIT = 4096
_VERIFIER_TIMEOUT_SECONDS = 60
_VERIFIER_BOOTSTRAP = (
    "import resource,runpy\n"
    f"resource.setrlimit(resource.RLIMIT_FSIZE,({_VERIFIER_OUTPUT_LIMIT},"
    f"{_VERIFIER_OUTPUT_LIMIT}))\n"
    "runpy.run_module('skynet_cyclops.hermes_cron_seams',run_name='__main__')\n"
)


def _verification() -> dict[str, object]:
    return {
        "hermes_seam_evidence_protocol": "cyclops-hermes-seam-evidence/v1",
        "disposable_profile": True,
        "cronjob_full_field_readback": True,
        "jobs_paused": True,
        "stable_names_exactly_once": True,
        "resolved_toolsets": {"cyclops-manager-router": []},
        "quiet_no_agent": True,
        "task_scope_denied": True,
        "bounded_output_bytes": 4096,
        "exact_output_matches": 1,
    }


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _nonce(value: str | None) -> str:
    candidate = secrets.token_hex(32) if value is None else value
    if (
        not isinstance(candidate, str)
        or len(candidate) != 64
        or any(character not in _HEX for character in candidate)
    ):
        raise ValidationError("install attempt nonce is invalid")
    return candidate


def _digest(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise ValidationError("cron install execution spec is invalid")
    return value


def _home_delivery(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character.isspace() or ord(character) < 33 for character in value)
    ):
        raise ValidationError("home_delivery is invalid")
    return value


def _validate_visible_jobs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > 256:
        raise ValidationError("cron full-field snapshot is invalid")
    jobs: list[dict[str, object]] = []
    identifiers: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValidationError("cron full-field snapshot is invalid")
        keys = set(item)
        if not keys >= _VISIBLE_REQUIRED or keys - _VISIBLE_REQUIRED - _VISIBLE_OPTIONAL:
            raise ValidationError("cron full-field snapshot is incomplete")
        job_id = item.get("job_id")
        name = item.get("name")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 128:
            raise ValidationError("cron full-field snapshot job id is invalid")
        if not isinstance(name, str) or not name or len(name) > 128 or job_id in identifiers:
            raise ValidationError("cron full-field snapshot identity is invalid")
        identifiers.add(job_id)
        jobs.append(dict(item))
    return jobs


def _scripts(nonce: str) -> tuple[dict[str, str], dict[str, str]]:
    marker = f"# cyclops-install-attempt: {nonce}\n"
    router_content = (
        "#!/usr/bin/env python3\n"
        + marker
        + "from skynet_cyclops.cli import main\n"
        + "raise SystemExit(main(['manager', 'router']))\n"
    )
    courier_content = (
        "#!/usr/bin/env python3\n"
        + marker
        + "from skynet_cyclops.cli import main\n"
        + "raise SystemExit(main(['manager', 'courier']))\n"
    )
    return (
        {"name": "cyclops-manager-router.py", "content": router_content},
        {"name": "cyclops-decision-courier.py", "content": courier_content},
    )


def _job_arguments(
    home_delivery: str, *, manager_prompt: str = MANAGER_PROMPT
) -> tuple[dict[str, object], dict[str, object]]:
    router = {
        "name": _STABLE_NAMES[0],
        "schedule": "every 2m",
        "prompt": manager_prompt,
        "repeat": 0,
        "deliver": "local",
        "skills": [],
        "script": "cyclops-manager-router.py",
        "continuity": False,
        "enabled_toolsets": ["no_mcp"],
        "no_agent": False,
        "attach_to_session": False,
    }
    courier = {
        "name": _STABLE_NAMES[1],
        "schedule": "every 2m",
        "prompt": "",
        "repeat": 0,
        "deliver": home_delivery,
        "skills": [],
        "script": "cyclops-decision-courier.py",
        "continuity": False,
        "enabled_toolsets": [],
        "no_agent": True,
        "attach_to_session": False,
    }
    return router, courier


def _validate_previous_spec(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("protocol") != _PROTOCOL:
        raise ValidationError("upgrade requires the exact prior spec")
    if value.get("release") != _PRIOR_RELEASE or value.get("operation") not in {
        "install",
        "upgrade",
    }:
        raise ValidationError("upgrade prior spec is incompatible")
    operations = value.get("operations")
    if not isinstance(operations, list) or len(operations) != 2:
        raise ValidationError("upgrade prior spec is incomplete")
    if not all(isinstance(item, dict) for item in operations):
        raise ValidationError("upgrade prior spec operations are invalid")
    typed_operations = cast(list[dict[str, object]], operations)
    if [item.get("stable_name") for item in typed_operations] != list(_STABLE_NAMES):
        raise ValidationError("upgrade prior spec identity is invalid")
    if any(not isinstance(item.get("arguments"), dict) for item in typed_operations):
        raise ValidationError("upgrade prior spec arguments are invalid")
    try:
        return _validate_cron_install_spec(
            value, expected_release=_PRIOR_RELEASE, manager_prompt=MANAGER_PROMPT_V0_2_2
        )
    except ValidationError as exc:
        raise ValidationError("upgrade requires the exact prior spec") from exc


def build_cron_install_spec(
    *,
    profile: str,
    home_delivery: str,
    operation: str,
    visible_jobs: Sequence[Mapping[str, object]],
    previous_spec: Mapping[str, object] | None = None,
    attempt_nonce: str | None = None,
) -> dict[str, object]:
    """Return a closed machine-readable plan for the profile-local cronjob tool."""
    if profile != "default":
        raise ValidationError("manager profile must be default")
    home_delivery = _home_delivery(home_delivery)
    if operation not in {"install", "upgrade"}:
        raise ValidationError("install operation is invalid")
    nonce = _nonce(attempt_nonce)
    snapshot = _validate_visible_jobs(visible_jobs)
    by_name: dict[str, list[dict[str, object]]] = {
        name: [item for item in snapshot if item["name"] == name] for name in _STABLE_NAMES
    }
    arguments = _job_arguments(home_delivery)
    artifacts = [
        {
            "name": item["name"],
            "content": item["content"],
            "sha256": hashlib.sha256(item["content"].encode()).hexdigest(),
            "mode": "0600",
        }
        for item in _scripts(nonce)
    ]

    operations: list[dict[str, object]] = []
    rollback: list[dict[str, object]] = []
    if operation == "install":
        conflicts = sorted(name for name, matches in by_name.items() if matches)
        if conflicts:
            raise ValidationError("cron stable name conflict")
        for index, (name, job_arguments) in enumerate(zip(_STABLE_NAMES, arguments, strict=True)):
            operations.append(
                {
                    "action": "create",
                    "stable_name": name,
                    "arguments": job_arguments,
                    "pause_after_create": True,
                    "expected_state": "paused",
                }
            )
            rollback.insert(
                0,
                {
                    "action": "remove",
                    "operation_index": index,
                    "stable_name": name,
                    "requires_created_job_id": True,
                },
            )
    else:
        prior = _validate_previous_spec(previous_spec)
        if any(len(matches) != 1 for matches in by_name.values()):
            raise ValidationError("upgrade requires one full-field snapshot per stable name")
        prior_operations = cast(list[dict[str, object]], prior["operations"])
        for index, (name, job_arguments) in enumerate(zip(_STABLE_NAMES, arguments, strict=True)):
            snapshot_job = by_name[name][0]
            if snapshot_job["state"] != "paused" or snapshot_job["enabled"] is not False:
                raise ValidationError("upgrade requires paused jobs")
            job_id = str(snapshot_job["job_id"])
            operations.append(
                {
                    "action": "update",
                    "stable_name": name,
                    "job_id": job_id,
                    "arguments": {**job_arguments, "job_id": job_id},
                    "expected_state": "paused",
                    "snapshot_sha256": _sha256(snapshot_job),
                }
            )
            prior_item = prior_operations[index]
            prior_arguments = cast(dict[str, object], prior_item["arguments"])
            rollback.insert(
                0,
                {
                    "action": "update",
                    "stable_name": name,
                    "job_id": job_id,
                    "arguments": {**prior_arguments, "job_id": job_id},
                    "restore_snapshot_sha256": _sha256(snapshot_job),
                },
            )

    spec: dict[str, object] = {
        "protocol": _PROTOCOL,
        "release": _RELEASE,
        "attempt_nonce": nonce,
        "profile": "default",
        "operation": operation,
        "snapshot": snapshot,
        "snapshot_sha256": _sha256(snapshot),
        "artifacts": artifacts,
        "operations": operations,
        "verification": _verification(),
        "rollback": rollback,
    }
    _validate_cron_install_spec(spec)
    return spec


def _validate_cron_install_spec(
    value: object,
    *,
    expected_release: str = _RELEASE,
    manager_prompt: str = MANAGER_PROMPT,
) -> dict[str, object]:
    """Validate the complete closed mutation contract before any side effect."""
    try:
        if not isinstance(value, dict) or set(value) != _SPEC_KEYS:
            raise ValidationError("cron install execution spec is invalid")
        if (
            value.get("protocol") != _PROTOCOL
            or value.get("release") != expected_release
            or value.get("profile") != "default"
            or value.get("verification") != _verification()
        ):
            raise ValidationError("cron install execution spec is invalid")
        nonce = _nonce(cast(str | None, value.get("attempt_nonce")))
        snapshot_digest = _digest(value.get("snapshot_sha256"))
        snapshot = _validate_visible_jobs(value.get("snapshot"))
        if _sha256(snapshot) != snapshot_digest:
            raise ValidationError("cron install execution spec is invalid")
        snapshot_by_name = {
            name: [item for item in snapshot if item["name"] == name] for name in _STABLE_NAMES
        }
        operation = value.get("operation")
        operations = value.get("operations")
        rollback = value.get("rollback")
        if operation not in {"install", "upgrade"} or not isinstance(operations, list):
            raise ValidationError("cron install execution spec is invalid")
        if len(operations) != 2 or not all(isinstance(item, dict) for item in operations):
            raise ValidationError("cron install operation is invalid")
        if not isinstance(rollback, list) or len(rollback) != 2:
            raise ValidationError("cron install execution spec is invalid")
        typed_operations = cast(list[dict[str, object]], operations)
        if any(item.get("action") not in {"create", "update"} for item in typed_operations):
            raise ValidationError("cron install action is invalid")

        expected_artifacts = [
            {
                "name": item["name"],
                "content": item["content"],
                "sha256": hashlib.sha256(item["content"].encode()).hexdigest(),
                "mode": "0600",
            }
            for item in _scripts(nonce)
        ]
        if value.get("artifacts") != expected_artifacts:
            raise ValidationError("cron install execution spec is invalid")

        courier_arguments = typed_operations[1].get("arguments")
        if not isinstance(courier_arguments, dict):
            raise ValidationError("cron install execution spec is invalid")
        current_arguments = _job_arguments(
            _home_delivery(courier_arguments.get("deliver")), manager_prompt=manager_prompt
        )
        expected_rollback: list[dict[str, object]]

        if operation == "install":
            expected_operations = [
                {
                    "action": "create",
                    "stable_name": name,
                    "arguments": arguments,
                    "pause_after_create": True,
                    "expected_state": "paused",
                }
                for name, arguments in zip(_STABLE_NAMES, current_arguments, strict=True)
            ]
            expected_rollback = [
                {
                    "action": "remove",
                    "operation_index": index,
                    "stable_name": _STABLE_NAMES[index],
                    "requires_created_job_id": True,
                }
                for index in (1, 0)
            ]
            if any(snapshot_by_name.values()):
                raise ValidationError("cron install execution spec is invalid")
        else:
            if any(len(matches) != 1 for matches in snapshot_by_name.values()):
                raise ValidationError("cron install execution spec is invalid")
            job_ids: list[str] = []
            snapshot_digests: list[str] = []
            expected_operations = []
            for index, (name, arguments) in enumerate(
                zip(_STABLE_NAMES, current_arguments, strict=True)
            ):
                item = typed_operations[index]
                job_id = item.get("job_id")
                snapshot_job = snapshot_by_name[name][0]
                if (
                    not isinstance(job_id, str)
                    or not job_id
                    or len(job_id) > 128
                    or job_id in job_ids
                    or snapshot_job["job_id"] != job_id
                    or snapshot_job["state"] != "paused"
                    or snapshot_job["enabled"] is not False
                ):
                    raise ValidationError("cron install execution spec is invalid")
                item_snapshot_digest = _digest(item.get("snapshot_sha256"))
                if _sha256(snapshot_job) != item_snapshot_digest:
                    raise ValidationError("cron install execution spec is invalid")
                job_ids.append(job_id)
                snapshot_digests.append(item_snapshot_digest)
                expected_operations.append(
                    {
                        "action": "update",
                        "stable_name": name,
                        "job_id": job_id,
                        "arguments": {**arguments, "job_id": job_id},
                        "expected_state": "paused",
                        "snapshot_sha256": item_snapshot_digest,
                    }
                )

            if not all(isinstance(item, dict) for item in rollback):
                raise ValidationError("cron install execution spec is invalid")
            typed_rollback = cast(list[dict[str, object]], rollback)
            prior_courier_arguments = typed_rollback[0].get("arguments")
            if not isinstance(prior_courier_arguments, dict):
                raise ValidationError("cron install execution spec is invalid")
            prior_arguments = _job_arguments(
                _home_delivery(prior_courier_arguments.get("deliver")),
                manager_prompt=MANAGER_PROMPT_V0_2_2,
            )
            expected_rollback = [
                {
                    "action": "update",
                    "stable_name": _STABLE_NAMES[index],
                    "job_id": job_ids[index],
                    "arguments": {**prior_arguments[index], "job_id": job_ids[index]},
                    "restore_snapshot_sha256": snapshot_digests[index],
                }
                for index in (1, 0)
            ]

        if operations != expected_operations or rollback != expected_rollback:
            raise ValidationError("cron install execution spec is invalid")
        return value
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("cron install execution spec is invalid") from exc


def build_cron_install_spec_v0_2_2(
    *,
    home_delivery: str,
    attempt_nonce: str,
    profile: str = "default",
    operation: str = "install",
    visible_jobs: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Generate the immutable exact prior install contract accepted for v0.3 upgrade."""
    if profile != "default" or operation != "install" or visible_jobs:
        raise ValidationError("prior install generator accepts only the exact v0.2.2 baseline")
    spec = build_cron_install_spec(
        profile="default",
        home_delivery=home_delivery,
        operation="install",
        visible_jobs=[],
        attempt_nonce=attempt_nonce,
    )
    spec["release"] = _PRIOR_RELEASE
    operations = cast(list[dict[str, object]], spec["operations"])
    router_arguments = cast(dict[str, object], operations[0]["arguments"])
    router_arguments["prompt"] = MANAGER_PROMPT_V0_2_2
    _validate_cron_install_spec(
        spec, expected_release=_PRIOR_RELEASE, manager_prompt=MANAGER_PROMPT_V0_2_2
    )
    return spec


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise ValidationError("staging directory is unsafe")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValidationError("staging directory ownership is unsafe")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


def _preflight_target(path: Path) -> None:
    if path.is_symlink():
        raise ValidationError("refusing symbolic link staging target")
    if not path.exists():
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError("staging target is not a regular file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValidationError("staging target ownership is unsafe")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValidationError("staging target permissions are unsafe")


def _atomic_private_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".cyclops-stage-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ValidationError("private staging write failed") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _installer_lock(path: Path) -> Iterator[None]:
    _preflight_target(path)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "a+b", closefd=True)
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
    except OSError as exc:
        raise ValidationError("installer lock failed") from exc


def _restore_target(path: Path, previous: tuple[bytes, int] | None, attempted: bytes) -> None:
    _preflight_target(path)
    if path.exists():
        current = path.read_bytes()
        allowed = {attempted}
        if previous is not None:
            allowed.add(previous[0])
        if current not in allowed:
            raise ValidationError("staging target changed during transaction")
    if previous is None:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        return
    content, mode = previous
    _atomic_private_write(path, content)
    os.chmod(path, mode)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def stage_cron_install(spec: Mapping[str, object], hermes_home: Path) -> dict[str, object]:
    """Stage private artifacts under the explicit default Hermes home; never touch cron storage."""
    try:
        validated_spec = _validate_cron_install_spec(dict(spec))
    except ValidationError as exc:
        raise ValidationError("cron install spec schema is invalid") from exc
    root = Path(hermes_home)
    if not root.is_absolute() or root.name != ".hermes":
        raise ValidationError("Hermes profile home is unsafe")
    scripts = root / "scripts"
    config_dir = root / "cyclops"
    try:
        info = root.lstat()
        if (
            root.resolve(strict=True) != root
            or root.is_symlink()
            or not stat.S_ISDIR(info.st_mode)
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValidationError("Hermes profile home is unsafe")
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError("Hermes profile home is unavailable") from exc
    _private_directory(scripts)
    _private_directory(config_dir)
    artifacts = cast(list[dict[str, object]], validated_spec["artifacts"])
    targets = [
        (
            scripts / cast(str, artifact["name"]),
            cast(str, artifact["content"]).encode(),
            cast(str, artifact["sha256"]),
        )
        for artifact in artifacts
    ]
    config = config_dir / "manager-install.json"
    config_bytes = _canonical(validated_spec)
    all_targets = [item[0] for item in targets] + [config]
    attempted = {target: content for target, content, _digest_value in targets}
    attempted[config] = config_bytes
    with _installer_lock(config_dir / "manager-install.lock"):
        for target in all_targets:
            _preflight_target(target)
        previous = {
            target: None
            if not target.exists()
            else (target.read_bytes(), stat.S_IMODE(target.stat().st_mode))
            for target in all_targets
        }
        try:
            for target, content, digest in targets:
                _atomic_private_write(target, content)
                if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise ValidationError("staged script hash verification failed")
            _atomic_private_write(config, config_bytes)
            if json.loads(config.read_text(encoding="utf-8")) != validated_spec:
                raise ValidationError("staged install config verification failed")
        except Exception as exc:
            rollback_failures: list[str] = []
            for target in reversed(all_targets):
                try:
                    _restore_target(target, previous[target], attempted[target])
                except Exception:
                    rollback_failures.append(target.name)
            if rollback_failures:
                raise ValidationError(
                    "private staging rollback failed: " + ", ".join(rollback_failures)
                ) from exc
            if isinstance(exc, ValidationError):
                raise
            raise ValidationError("private staging failed") from exc
    return {
        "state": "staged",
        "protocol": _PROTOCOL,
        "attempt_nonce": validated_spec["attempt_nonce"],
        "artifact_sha256": [item[2] for item in targets],
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
    }


def _tool_mapping(result: object) -> Mapping[str, object] | None:
    if isinstance(result, Mapping):
        return result
    if not isinstance(result, str) or len(result.encode()) > 1_000_000:
        return None
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _tool_success(result: object) -> bool:
    parsed = _tool_mapping(result)
    return parsed is not None and parsed.get("success") is True


def _tool_job_is_paused(result: object) -> bool:
    parsed = _tool_mapping(result)
    if parsed is None:
        return False
    job = parsed.get("job")
    return isinstance(job, Mapping) and job.get("state") == "paused" and job.get("enabled") is False


_SECURITY_READBACK_FIELDS = (
    "job_id",
    "name",
    "skill",
    "skills",
    "prompt_preview",
    "model",
    "provider",
    "base_url",
    "schedule",
    "repeat",
    "deliver",
    "enabled",
    "state",
    "script",
    "monitor_script",
    "monitor_url",
    "no_agent",
    "enabled_toolsets",
    "workdir",
    "continuity",
    "context_from",
)


def _security_readback(job: Mapping[str, object]) -> dict[str, object]:
    defaults: dict[str, object] = {
        "script": None,
        "monitor_script": None,
        "monitor_url": None,
        "no_agent": False,
        "enabled_toolsets": [],
        "workdir": None,
        "continuity": False,
        "context_from": [],
    }
    return {key: job.get(key, defaults.get(key)) for key in _SECURITY_READBACK_FIELDS}


def _expected_readback(
    arguments: Mapping[str, object], job_id: str, *, state: str
) -> dict[str, object]:
    prompt = str(arguments["prompt"])
    prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
    return _security_readback(
        {
            "job_id": job_id,
            "name": arguments["name"],
            "skill": None,
            "skills": arguments["skills"],
            "prompt_preview": prompt_preview,
            "model": None,
            "provider": None,
            "base_url": None,
            "schedule": arguments["schedule"],
            "repeat": "forever",
            "deliver": arguments["deliver"],
            "enabled": state != "paused",
            "state": state,
            "script": arguments["script"],
            "no_agent": arguments["no_agent"],
            "enabled_toolsets": arguments["enabled_toolsets"],
            "continuity": arguments["continuity"],
        }
    )


def _full_prompt_matches(
    reader: FullJobReader | None,
    expected: Mapping[str, Mapping[str, object]],
    prompts: Mapping[str, str],
) -> bool:
    if not expected:
        return not prompts
    if reader is None or set(prompts) != set(expected):
        return False
    for name, expected_job in expected.items():
        job_id = expected_job.get("job_id")
        if not isinstance(job_id, str):
            return False
        try:
            result = reader(job_id)
        except Exception:
            return False
        if not isinstance(result, Mapping):
            return False
        candidate = result.get("job", result)
        if not isinstance(candidate, Mapping):
            return False
        candidate_id = candidate.get("id", candidate.get("job_id"))
        if candidate_id != job_id or candidate.get("prompt") != prompts[name]:
            return False
    return True


def _readback_matches(
    tool: CronTool,
    expected: Mapping[str, Mapping[str, object]],
    *,
    full_job_reader: FullJobReader | None = None,
    expected_prompts: Mapping[str, str] | None = None,
) -> bool:
    try:
        result = tool("list", include_disabled=True)
    except Exception:
        return False
    parsed = _tool_mapping(result)
    if not _tool_success(result) or parsed is None:
        return False
    try:
        jobs = _validate_visible_jobs(parsed.get("jobs"))
    except ValidationError:
        return False
    stable = [job for job in jobs if job["name"] in _STABLE_NAMES]
    if len(stable) != len(expected):
        return False
    by_name = {str(job["name"]): job for job in stable}
    visible_matches = set(by_name) == set(expected) and all(
        _security_readback(by_name[name]) == dict(expected_job)
        for name, expected_job in expected.items()
    )
    return visible_matches and _full_prompt_matches(
        full_job_reader, expected, expected_prompts or {}
    )


def _seam_evidence_is_valid(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == _SEAM_EVIDENCE_KEYS
        and all(
            (
                value.get("protocol") == "cyclops-hermes-seam-evidence/v1",
                value.get("canonical_profile") is True,
                value.get("disposable_profile") is True,
                value.get("configured_toolsets") == ["no_mcp"],
                value.get("cronjob_full_field_readback") is True,
                value.get("resolved_toolsets") == [],
                value.get("resolved_tools") == [],
                value.get("courier_empty_is_silent") is True,
                value.get("non_task_scoped") is True,
                type(value.get("quiet_agent_calls")) is int and value.get("quiet_agent_calls") == 0,
            )
        )
    )


def _run_hermes_seam_verifier() -> Mapping[str, object]:
    """Execute the installed verifier in a private, sanitized child process."""
    with tempfile.TemporaryDirectory(prefix="cyclops-hermes-verifier-") as temporary:
        home = Path(temporary) / "home"
        home.mkdir(mode=0o700)
        hermes_home = home / ".hermes"
        environment = {
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
            "PYTHONNOUSERSITE": "1",
            "PYTHONUTF8": "1",
        }
        stdout_path = Path(temporary) / "stdout"
        stderr_path = Path(temporary) / "stderr"
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(  # noqa: S603 - fixed interpreter and bootstrap
                [sys.executable, "-c", _VERIFIER_BOOTSTRAP],
                shell=False,  # Static bounded argv; never a shell.  # nosec B603
                check=False,
                stdout=stdout,
                stderr=stderr,
                cwd=home,
                env=environment,
                timeout=_VERIFIER_TIMEOUT_SECONDS,
            )
        stdout_bytes = stdout_path.read_bytes()
        stderr_bytes = stderr_path.read_bytes()
        if (
            completed.returncode != 0
            or len(stdout_bytes) > _VERIFIER_OUTPUT_LIMIT
            or len(stderr_bytes) > _VERIFIER_OUTPUT_LIMIT
        ):
            raise ValidationError("Hermes seam verifier failed")
        try:
            lines = stdout_bytes.decode("utf-8", errors="strict").splitlines()
            stderr_bytes.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ValidationError("Hermes seam verifier output is invalid") from exc
        if len(lines) != 1:
            raise ValidationError("Hermes seam verifier output is invalid")
        try:
            report = json.loads(lines[0])
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValidationError("Hermes seam verifier output is invalid") from exc
        if not _seam_evidence_is_valid(report):
            raise ValidationError("Hermes seam verifier evidence is invalid")
        return cast(Mapping[str, object], report)


def execute_cron_install_spec(
    spec: Mapping[str, object],
    tool: CronTool,
    *,
    seam_verifier: SeamVerifier | None = None,
    full_job_reader: FullJobReader | None = None,
) -> dict[str, object]:
    """Reference fail-closed interpreter for the emitted supported-tool operations."""
    validated_spec = _validate_cron_install_spec(dict(spec))
    operation = validated_spec["operation"]
    operations = cast(list[dict[str, object]], validated_spec["operations"])
    rollback = cast(list[dict[str, object]], validated_spec["rollback"])
    snapshot = cast(list[dict[str, object]], validated_spec["snapshot"])
    expected: dict[str, dict[str, object]] = {
        str(job["name"]): _security_readback(job)
        for job in snapshot
        if job["name"] in _STABLE_NAMES
    }
    expected_prompts: dict[str, str] = {}
    if operation == "upgrade":
        for item in rollback:
            arguments = cast(dict[str, object], item["arguments"])
            expected_prompts[str(item["stable_name"])] = str(arguments["prompt"])
    if full_job_reader is None:
        candidate_reader = getattr(tool, "read_full_job", None)
        if callable(candidate_reader):
            full_job_reader = cast(FullJobReader, candidate_reader)
    if operation == "upgrade" and not _readback_matches(
        tool,
        expected,
        full_job_reader=full_job_reader,
        expected_prompts=expected_prompts,
    ):
        raise ValidationError("upgrade live definitions do not match the exact prior release")
    created: dict[int, str] = {}
    failed = False
    for index, item in enumerate(operations):
        arguments = cast(dict[str, object], item["arguments"])
        action = cast(str, item["action"])
        try:
            result = tool(str(action), **arguments)
        except Exception:
            failed = True
            break
        if not _tool_success(result):
            failed = True
            break
        if action == "update" and not _tool_job_is_paused(result):
            failed = True
            break
        if action == "create":
            parsed = _tool_mapping(result)
            if parsed is None:
                failed = True
                break
            job_id = parsed.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                failed = True
                break
            created[index] = job_id
            expected[str(item["stable_name"])] = _expected_readback(
                arguments, job_id, state="scheduled"
            )
            expected_prompts[str(item["stable_name"])] = str(arguments["prompt"])
            if not _readback_matches(
                tool,
                expected,
                full_job_reader=full_job_reader,
                expected_prompts=expected_prompts,
            ):
                failed = True
                break
            if item.get("pause_after_create") is True:
                try:
                    pause = tool("pause", job_id=job_id, reason="cyclops-install")
                except Exception:
                    failed = True
                    break
                if not _tool_success(pause) or not _tool_job_is_paused(pause):
                    failed = True
                    break
                expected[str(item["stable_name"])] = _expected_readback(
                    arguments, job_id, state="paused"
                )
                if not _readback_matches(
                    tool,
                    expected,
                    full_job_reader=full_job_reader,
                    expected_prompts=expected_prompts,
                ):
                    failed = True
                    break
        else:
            job_id = cast(str, item["job_id"])
            expected[str(item["stable_name"])] = _expected_readback(
                arguments, job_id, state="paused"
            )
            expected_prompts[str(item["stable_name"])] = str(arguments["prompt"])
            if not _readback_matches(
                tool,
                expected,
                full_job_reader=full_job_reader,
                expected_prompts=expected_prompts,
            ):
                failed = True
                break
    if not failed:
        verifier = seam_verifier
        if verifier is None:
            verifier = _run_hermes_seam_verifier
        try:
            failed = not _seam_evidence_is_valid(verifier())
        except Exception:
            failed = True
    if not failed:
        return {
            "state": "applied_paused",
            "operation": operation,
            "created_job_ids": list(created.values()),
            "rollback_failures": [],
        }

    rollback_failures: list[str] = []
    for item in rollback:
        action = cast(str, item["action"])
        try:
            if action == "remove":
                rollback_index = item.get("operation_index")
                if not isinstance(rollback_index, int) or rollback_index not in created:
                    continue
                result = tool("remove", job_id=created[rollback_index])
            else:
                result = tool("update", **cast(dict[str, object], item["arguments"]))
        except Exception:
            rollback_failures.append(str(item.get("stable_name", "unknown")))
            continue
        if not _tool_success(result) or (action == "update" and not _tool_job_is_paused(result)):
            rollback_failures.append(str(item.get("stable_name", "unknown")))
    rollback_expected = {
        str(job["name"]): _security_readback(job)
        for job in snapshot
        if job["name"] in _STABLE_NAMES
    }
    rollback_prompts: dict[str, str] = {}
    if operation == "upgrade":
        for item in rollback:
            arguments = cast(dict[str, object], item["arguments"])
            rollback_prompts[str(item["stable_name"])] = str(arguments["prompt"])
    if not _readback_matches(
        tool,
        rollback_expected,
        full_job_reader=full_job_reader,
        expected_prompts=rollback_prompts,
    ):
        rollback_failures.append("readback")
    return {
        "state": "rollback_failed" if rollback_failures else "rolled_back",
        "operation": operation,
        "created_job_ids": list(created.values()),
        "rollback_failures": rollback_failures,
    }


def validate_cron_install_spec(value: object) -> dict[str, object]:
    """Public strict validator for staged activation evidence consumers."""
    return _validate_cron_install_spec(value)


def seam_evidence_is_valid(value: object) -> bool:
    """Return whether a report matches the reviewed installed-Hermes seam contract."""
    return _seam_evidence_is_valid(value)
