"""Profile-local staging and strict cronjob-tool installation contracts.

This module never imports Hermes or opens a Hermes cron store.  It writes only private
script/config artifacts and emits operations for the supported profile-local ``cronjob`` tool.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import cast

from .errors import ValidationError
from .manager import MANAGER_PROMPT

_PROTOCOL = "cyclops-cron-install/v1"
_RELEASE = "0.2.0"
_STABLE_NAMES = ("cyclops-manager-router", "cyclops-decision-courier")
_HEX = frozenset("0123456789abcdef")
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


def _job_arguments(home_delivery: str) -> tuple[dict[str, object], dict[str, object]]:
    router = {
        "name": _STABLE_NAMES[0],
        "schedule": "every 2m",
        "prompt": MANAGER_PROMPT,
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
    if value.get("release") != _RELEASE or value.get("operation") not in {"install", "upgrade"}:
        raise ValidationError("upgrade prior spec is incompatible")
    operations = value.get("operations")
    if not isinstance(operations, list) or len(operations) != 2:
        raise ValidationError("upgrade prior spec is incomplete")
    if [item.get("stable_name") for item in operations if isinstance(item, dict)] != list(
        _STABLE_NAMES
    ):
        raise ValidationError("upgrade prior spec identity is invalid")
    if any(not isinstance(item.get("arguments"), dict) for item in operations):
        raise ValidationError("upgrade prior spec arguments are invalid")
    return value


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
    if (
        not isinstance(home_delivery, str)
        or not home_delivery
        or len(home_delivery) > 256
        or any(character.isspace() or ord(character) < 33 for character in home_delivery)
    ):
        raise ValidationError("home_delivery is invalid")
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

    return {
        "protocol": _PROTOCOL,
        "release": _RELEASE,
        "attempt_nonce": nonce,
        "profile": "default",
        "operation": operation,
        "snapshot_sha256": _sha256(snapshot),
        "artifacts": artifacts,
        "operations": operations,
        "verification": {
            "hermes_seam_evidence_protocol": "cyclops-hermes-seam-evidence/v1",
            "jobs_paused": True,
            "stable_names_exactly_once": True,
            "resolved_toolsets": {"cyclops-manager-router": []},
            "quiet_no_agent": True,
            "task_scope_denied": True,
            "bounded_output_bytes": 4096,
            "exact_output_matches": 1,
        },
        "rollback": rollback,
    }


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


def stage_cron_install(spec: Mapping[str, object], hermes_home: Path) -> dict[str, object]:
    """Stage private artifacts under one explicit profile home; never touch cron storage."""
    if spec.get("protocol") != _PROTOCOL or set(spec) != {
        "protocol",
        "release",
        "attempt_nonce",
        "profile",
        "operation",
        "snapshot_sha256",
        "artifacts",
        "operations",
        "verification",
        "rollback",
    }:
        raise ValidationError("cron install spec schema is invalid")
    root = Path(hermes_home)
    if not root.is_absolute() or root == Path(root.anchor):
        raise ValidationError("Hermes profile home is unsafe")
    scripts = root / "scripts"
    config_dir = root / "cyclops"
    _private_directory(root)
    _private_directory(scripts)
    _private_directory(config_dir)
    artifacts = spec.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ValidationError("cron install artifacts are invalid")
    targets: list[tuple[Path, bytes, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"name", "content", "sha256", "mode"}:
            raise ValidationError("cron install artifact schema is invalid")
        name = artifact["name"]
        content = artifact["content"]
        digest = artifact["sha256"]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(content, str)
            or not isinstance(digest, str)
            or hashlib.sha256(content.encode()).hexdigest() != digest
            or artifact["mode"] != "0600"
        ):
            raise ValidationError("cron install artifact integrity is invalid")
        targets.append((scripts / name, content.encode(), digest))
    config = config_dir / "manager-install.json"
    for target, _content, _digest in targets:
        _preflight_target(target)
    _preflight_target(config)
    for target, content, digest in targets:
        _atomic_private_write(target, content)
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValidationError("staged script hash verification failed")
    config_bytes = _canonical(spec)
    _atomic_private_write(config, config_bytes)
    if json.loads(config.read_text(encoding="utf-8")) != spec:
        raise ValidationError("staged install config verification failed")
    return {
        "state": "staged",
        "protocol": _PROTOCOL,
        "attempt_nonce": spec["attempt_nonce"],
        "artifact_sha256": [item[2] for item in targets],
        "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
    }


def _tool_success(result: object) -> bool:
    return isinstance(result, Mapping) and result.get("success") is True


def _tool_job_is_paused(result: object) -> bool:
    if not isinstance(result, Mapping):
        return False
    job = result.get("job")
    return isinstance(job, Mapping) and job.get("state") == "paused" and job.get("enabled") is False


def execute_cron_install_spec(spec: Mapping[str, object], tool: CronTool) -> dict[str, object]:
    """Reference fail-closed interpreter for the emitted supported-tool operations."""
    if spec.get("protocol") != _PROTOCOL or spec.get("release") != _RELEASE:
        raise ValidationError("cron install execution spec is invalid")
    _nonce(cast(str | None, spec.get("attempt_nonce")))
    operation = spec.get("operation")
    operations = spec.get("operations")
    rollback = spec.get("rollback")
    if (
        operation not in {"install", "upgrade"}
        or not isinstance(operations, list)
        or not isinstance(rollback, list)
    ):
        raise ValidationError("cron install execution spec is invalid")
    created: dict[int, str] = {}
    failed = False
    for index, item in enumerate(operations):
        if not isinstance(item, dict) or not isinstance(item.get("arguments"), dict):
            raise ValidationError("cron install operation is invalid")
        action = item.get("action")
        if action not in {"create", "update"}:
            raise ValidationError("cron install action is invalid")
        try:
            result = tool(str(action), **item["arguments"])
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
            if not isinstance(result, Mapping):
                failed = True
                break
            job_id = result.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                failed = True
                break
            created[index] = job_id
            if item.get("pause_after_create") is True:
                try:
                    pause = tool("pause", job_id=job_id, reason="cyclops-install")
                except Exception:
                    failed = True
                    break
                if not _tool_success(pause) or not _tool_job_is_paused(pause):
                    failed = True
                    break
    if not failed:
        return {
            "state": "applied_paused",
            "operation": operation,
            "created_job_ids": list(created.values()),
            "rollback_failures": [],
        }

    rollback_failures: list[str] = []
    for item in rollback:
        if not isinstance(item, dict):
            rollback_failures.append("invalid-rollback-entry")
            continue
        action = item.get("action")
        try:
            if action == "remove":
                rollback_index = item.get("operation_index")
                if not isinstance(rollback_index, int) or rollback_index not in created:
                    continue
                result = tool("remove", job_id=created[rollback_index])
            elif action == "update" and isinstance(item.get("arguments"), dict):
                result = tool("update", **item["arguments"])
            else:
                rollback_failures.append(str(item.get("stable_name", "invalid")))
                continue
        except Exception:
            rollback_failures.append(str(item.get("stable_name", "unknown")))
            continue
        if not _tool_success(result) or (action == "update" and not _tool_job_is_paused(result)):
            rollback_failures.append(str(item.get("stable_name", "unknown")))
    return {
        "state": "rollback_failed" if rollback_failures else "rolled_back",
        "operation": operation,
        "created_job_ids": list(created.values()),
        "rollback_failures": rollback_failures,
    }
