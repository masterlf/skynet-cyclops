from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import manifest_data

import skynet_cyclops.activation as activation_module
from skynet_cyclops.activation import (
    ACTIVATION_PROTOCOL,
    RELEASE,
    ActivationInputs,
    ActivationVerdict,
    HermesCronDefinitionAdapter,
    activate_manager,
    activation_verdict,
    canonical_sha256,
    deactivate_manager,
    job_definition_sha256,
    load_activation_inputs,
)
from skynet_cyclops.errors import ValidationError
from skynet_cyclops.ledger import Ledger
from skynet_cyclops.manager import IncidentObservation, manager_router_gate
from skynet_cyclops.manager_install import build_cron_install_spec, stage_cron_install
from skynet_cyclops.manifest import parse_manifest
from skynet_cyclops.tick import run_tick


def _job(job_id: str, name: str, *, prompt: str, no_agent: bool) -> dict[str, object]:
    return {
        "job_id": job_id,
        "name": name,
        "schedule": "every 2m",
        "repeat": "forever",
        "deliver": ["local" if not no_agent else "telegram"],
        "skills": [],
        "model": None,
        "provider": None,
        "provider_snapshot": None,
        "model_snapshot": None,
        "base_url": None,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "script": f"{name}.py",
        "monitor_script": None,
        "monitor_url": None,
        "no_agent": no_agent,
        "enabled_toolsets": [] if no_agent else ["no_mcp"],
        "workdir": None,
        "continuity": False,
        "context_from": [],
        "attach_to_session": False,
        "state": "paused",
        "enabled": False,
    }


def _inputs(tmp_path: Path) -> ActivationInputs:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce="a" * 64,
    )
    seam = {
        "protocol": "cyclops-hermes-seam-evidence/v1",
        "canonical_profile": True,
        "configured_toolsets": ["no_mcp"],
        "cronjob_full_field_readback": True,
        "courier_empty_is_silent": True,
        "disposable_profile": True,
        "non_task_scoped": True,
        "quiet_agent_calls": 0,
        "resolved_tools": [],
        "resolved_toolsets": [],
    }
    scripts = {
        str(artifact["name"]): str(artifact["content"]).encode()
        for artifact in spec["artifacts"]  # type: ignore[union-attr]
    }
    operations = spec["operations"]
    assert isinstance(operations, list)
    jobs: dict[str, dict[str, object]] = {}
    for role, job_id, operation in zip(
        ("router", "courier"), ("job-router", "job-courier"), operations, strict=True
    ):
        assert isinstance(operation, dict)
        arguments = operation["arguments"]
        assert isinstance(arguments, dict)
        jobs[role] = {
            "job_id": job_id,
            "name": arguments["name"],
            "schedule": arguments["schedule"],
            "repeat": "forever",
            "deliver": [arguments["deliver"]],
            "skills": arguments["skills"],
            "model": None,
            "provider": None,
            "provider_snapshot": None,
            "model_snapshot": None,
            "base_url": None,
            "prompt_sha256": hashlib.sha256(str(arguments["prompt"]).encode()).hexdigest(),
            "script": arguments["script"],
            "monitor_script": None,
            "monitor_url": None,
            "no_agent": arguments["no_agent"],
            "enabled_toolsets": arguments["enabled_toolsets"],
            "workdir": None,
            "continuity": arguments["continuity"],
            "context_from": [],
            "attach_to_session": arguments["attach_to_session"],
            "state": "paused",
            "enabled": False,
        }
    record = {
        "protocol": ACTIVATION_PROTOCOL,
        "schema_version": 1,
        "state": "enabled",
        "release": RELEASE,
        "profile": "default",
        "activated_at": "2026-08-20T09:00:00Z",
        "deactivated_at": None,
        "hermes_version": "0.9.0",
        "manager_install_spec_sha256": canonical_sha256(spec),
        "seam_evidence_protocol": "cyclops-hermes-seam-evidence/v1",
        "seam_evidence_sha256": canonical_sha256(seam),
        "jobs": {
            name: {"job_id": value["job_id"], "definition_sha256": job_definition_sha256(value)}
            for name, value in jobs.items()
        },
    }
    path = tmp_path / "manager-activation.json"
    path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    path.chmod(0o600)
    return ActivationInputs(
        activation_path=path,
        release=RELEASE,
        profile="default",
        hermes_version="0.9.0",
        install_spec=spec,
        scripts=scripts,
        jobs=jobs,
        seam_evidence=seam,
    )


def _private_profile_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir(mode=0o700, exist_ok=True)
    home.chmod(0o700)
    return home


def test_absent_is_unchecked_and_exact_current_binding_is_supported(tmp_path: Path) -> None:
    absent = _inputs(tmp_path)
    absent.activation_path.unlink()
    assert activation_verdict(absent).public == ("unchecked", False)

    current = _inputs(tmp_path)
    verdict = activation_verdict(current)
    assert verdict.reason == "supported"
    assert verdict.public == ("supported", True)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda value: value.__setitem__("release", "0.2.0"), "release_drift"),
        (lambda value: value.__setitem__("profile", "other"), "profile_drift"),
        (lambda value: value.__setitem__("hermes_version", "other"), "hermes_drift"),
        (lambda value: value["install_spec"].update(extra=True), "spec_drift"),
        (
            lambda value: value["scripts"].update({next(iter(value["scripts"])): b"changed"}),
            "script_drift",
        ),
        (lambda value: value["seam_evidence"].update(verified=False), "seam_evidence_drift"),
        (lambda value: value["jobs"].pop("courier"), "job_missing"),
        (lambda value: value["jobs"]["router"].update(job_id="other"), "job_identity_drift"),
        (lambda value: value["jobs"]["router"].update(schedule="daily"), "job_definition_drift"),
        (lambda value: value["jobs"]["router"].pop("prompt_sha256"), "prompt_unverifiable"),
    ],
)
def test_each_current_evidence_drift_fails_closed(
    tmp_path: Path, mutation: object, reason: str
) -> None:
    inputs = _inputs(tmp_path)
    mutable = {
        "install_spec": dict(inputs.install_spec),
        "seam_evidence": dict(inputs.seam_evidence),
        "scripts": dict(inputs.scripts),
        "jobs": {name: dict(job) for name, job in inputs.jobs.items()},
    }
    if reason == "release_drift":
        inputs = replace(inputs, release="0.2.0")
    elif reason == "profile_drift":
        inputs = replace(inputs, profile="other")
    elif reason == "hermes_drift":
        inputs = replace(inputs, hermes_version="other")
    else:
        mutation(mutable)  # type: ignore[operator]
        inputs = ActivationInputs(
            activation_path=inputs.activation_path,
            release=inputs.release,
            profile=inputs.profile,
            hermes_version=inputs.hermes_version,
            install_spec=mutable["install_spec"],
            scripts=mutable["scripts"],
            jobs=mutable["jobs"],
            seam_evidence=mutable["seam_evidence"],
        )
    verdict = activation_verdict(inputs)
    assert verdict.reason == reason
    assert verdict.public == ("unsupported", False)


def test_hostile_activation_files_deny_without_private_projection(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.write_text('{"protocol":"x","protocol":"y"}', encoding="utf-8")
    assert activation_verdict(inputs).reason == "malformed"
    inputs.activation_path.unlink()
    inputs.activation_path.symlink_to(tmp_path / "missing")
    verdict = activation_verdict(inputs)
    assert verdict.reason == "unsafe_file"
    assert verdict.public == ("unsupported", False)


def test_activate_is_dry_run_first_and_deactivate_is_atomic_and_idempotent(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    plan = activate_manager(inputs, now="2026-08-20T09:00:00Z", apply=False, environment={})
    assert plan == {"mode": "dry-run", "compatibility_state": "supported", "wake_enabled": True}
    assert not inputs.activation_path.exists()

    applied = activate_manager(
        inputs,
        now="2026-08-20T09:00:00Z",
        apply=True,
        environment={},
        refresh=lambda: inputs,
    )
    assert applied == {"mode": "applied", "compatibility_state": "supported", "wake_enabled": True}
    assert inputs.activation_path.stat().st_mode & 0o777 == 0o600
    assert activation_verdict(inputs).wake_enabled is True

    dry = deactivate_manager(
        inputs.activation_path, now="2026-08-20T09:01:00Z", apply=False, environment={}
    )
    assert dry["mode"] == "dry-run"
    assert activation_verdict(inputs).wake_enabled is True
    deactivate_manager(
        inputs.activation_path, now="2026-08-20T09:01:00Z", apply=True, environment={}
    )
    assert activation_verdict(inputs).reason == "disabled"
    deactivate_manager(
        inputs.activation_path, now="2026-08-20T09:01:00Z", apply=True, environment={}
    )
    assert activation_verdict(inputs).reason == "disabled"


def test_activation_apply_rejects_task_scope_and_unsafe_parent(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    with pytest.raises(ValidationError, match="task scope"):
        activate_manager(
            inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={"HERMES_KANBAN_TASK": "synthetic"},
        )
    os.chmod(tmp_path, 0o755)  # noqa: S103 - deliberately hostile fixture
    with pytest.raises(ValidationError, match="directory"):
        activate_manager(
            inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: inputs,
        )


def test_activation_requires_paused_jobs_but_runtime_hash_excludes_pause_state(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    running = {
        name: {**job, "state": "scheduled", "enabled": True} for name, job in inputs.jobs.items()
    }
    running_inputs = replace(inputs, jobs=running)
    with pytest.raises(ValidationError, match="paused"):
        activate_manager(
            running_inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: running_inputs,
        )

    activate_manager(
        inputs,
        now="2026-08-20T09:00:00Z",
        apply=True,
        environment={},
        refresh=lambda: inputs,
    )
    assert activation_verdict(running_inputs).reason == "supported"


def test_activation_apply_rejects_job_definition_not_matching_install_spec(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    jobs = {name: dict(job) for name, job in inputs.jobs.items()}
    jobs["router"]["schedule"] = "daily"
    changed = replace(inputs, jobs=jobs)
    with pytest.raises(ValidationError, match="definition"):
        activate_manager(
            changed,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: changed,
        )
    assert not inputs.activation_path.exists()


def test_activation_binds_generated_provider_and_model_snapshots(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    jobs = {name: dict(job) for name, job in inputs.jobs.items()}
    jobs["router"].update(provider_snapshot="synthetic-provider", model_snapshot="synthetic-model")
    current = replace(inputs, jobs=jobs)

    activate_manager(
        current,
        now="2026-08-20T09:00:00Z",
        apply=True,
        environment={},
        refresh=lambda: current,
    )

    assert activation_verdict(current).reason == "supported"
    drifted_jobs = {name: dict(job) for name, job in jobs.items()}
    drifted_jobs["router"]["model_snapshot"] = "other-model"
    assert activation_verdict(replace(current, jobs=drifted_jobs)).reason == "job_definition_drift"


def test_static_evidence_timestamps_cannot_authorize_or_expire_a_current_readback(
    tmp_path: Path,
) -> None:
    inputs = replace(
        _inputs(tmp_path),
        evidence_collected_at="2026-08-20T08:00:00Z",
        validated_at="2026-08-20T09:00:00Z",
    )
    assert activation_verdict(inputs).reason == "supported"
    inputs.activation_path.unlink()
    assert (
        activate_manager(
            inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: inputs,
        )["wake_enabled"]
        is True
    )


def test_first_use_deactivation_is_a_valid_durable_deny(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    deactivate_manager(
        inputs.activation_path,
        now="2026-08-20T09:00:00Z",
        apply=True,
        environment={},
    )
    assert activation_verdict(inputs).reason == "disabled"


def test_private_current_evidence_loader_recollects_staged_inputs(tmp_path: Path) -> None:
    source = _inputs(tmp_path)
    home = _private_profile_home(tmp_path)
    stage_cron_install(source.install_spec, home)
    evidence_path = tmp_path / "current-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "protocol": "cyclops-manager-current-evidence/v1",
                "source": "supported-full-definition-api",
                "collected_at": datetime.now(UTC)
                .replace(microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "profile": "default",
                "hermes_version": source.hermes_version,
                "jobs": source.jobs,
                "seam_evidence": source.seam_evidence,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    evidence_path.chmod(0o600)
    activation_path = tmp_path / "loaded-activation.json"
    binary, _definitions = _fake_hermes(tmp_path, source)

    def collect() -> ActivationInputs:
        return load_activation_inputs(
            activation_path=activation_path,
            hermes_home=home,
            evidence_path=evidence_path,
            hermes_binary=str(binary),
        )

    loaded = collect()
    result = activate_manager(
        loaded,
        now=datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
        apply=True,
        environment={},
        refresh=collect,
    )
    assert result["wake_enabled"] is True
    assert activation_verdict(collect()).reason == "supported"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda job: job.pop("name"),
        lambda job: job.update(extra=True),
        lambda job: job.update(job_id="bad id"),
        lambda job: job.update(name="bad name"),
        lambda job: job.update(prompt_sha256="A" * 64),
        lambda job: job.update(no_agent=1),
        lambda job: job.update(continuity=1),
        lambda job: job.update(attach_to_session=1),
        lambda job: job.update(skills="bad"),
        lambda job: job.update(enabled_toolsets="bad"),
        lambda job: job.update(context_from="bad"),
        lambda job: job.update(state="failed"),
        lambda job: job.update(provider_snapshot=[]),
    ],
)
def test_full_job_definition_schema_is_closed(tmp_path: Path, mutate: object) -> None:
    job = dict(_inputs(tmp_path).jobs["router"])
    mutate(job)  # type: ignore[operator]
    with pytest.raises(ValidationError, match="definition evidence"):
        job_definition_sha256(job)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda record: record.update(extra=True),
        lambda record: record.update(protocol="wrong"),
        lambda record: record.update(schema_version=True),
        lambda record: record.update(state="other"),
        lambda record: record.update(profile="other"),
        lambda record: record.update(activated_at="not-a-time"),
        lambda record: record.update(deactivated_at="2026-08-20T09:00:00Z"),
        lambda record: record.update(manager_install_spec_sha256="A" * 64),
        lambda record: record["jobs"].update(extra=None),
        lambda record: record["jobs"]["router"].update(job_id="bad id"),
        lambda record: record["jobs"]["courier"].update(job_id=record["jobs"]["router"]["job_id"]),
    ],
)
def test_activation_record_schema_is_closed(tmp_path: Path, mutate: object) -> None:
    inputs = _inputs(tmp_path)
    record = json.loads(inputs.activation_path.read_text(encoding="utf-8"))
    mutate(record)  # type: ignore[operator]
    inputs.activation_path.write_text(
        json.dumps(record, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    assert activation_verdict(inputs).reason == "malformed"


def test_activation_file_controls_oversize_links_and_permissions_deny(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.write_bytes(b'{"x":"\x01"}')
    assert activation_verdict(inputs).reason == "malformed"
    inputs.activation_path.write_bytes(b"x" * (16 * 1024 + 1))
    assert activation_verdict(inputs).reason == "unsafe_file"
    inputs.activation_path.write_text("{}", encoding="utf-8")
    inputs.activation_path.chmod(0o644)
    assert activation_verdict(inputs).reason == "unsafe_file"
    inputs.activation_path.chmod(0o600)
    linked = tmp_path / "linked-activation.json"
    os.link(inputs.activation_path, linked)
    assert activation_verdict(inputs).reason == "unsafe_file"


def test_deactivate_rejects_malformed_record_without_repair(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="malformed"):
        deactivate_manager(
            inputs.activation_path,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
        )
    assert inputs.activation_path.read_text(encoding="utf-8") == "{}"


def _staged_current_evidence(tmp_path: Path) -> tuple[ActivationInputs, Path, Path]:
    source = _inputs(tmp_path)
    home = _private_profile_home(tmp_path)
    stage_cron_install(source.install_spec, home)
    evidence_path = tmp_path / "current-hostile-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "protocol": "cyclops-manager-current-evidence/v1",
                "source": "supported-full-definition-api",
                "collected_at": datetime.now(UTC)
                .replace(microsecond=0)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "profile": "default",
                "hermes_version": source.hermes_version,
                "jobs": source.jobs,
                "seam_evidence": source.seam_evidence,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    evidence_path.chmod(0o600)
    return source, home, evidence_path


def _definition_payload(
    source: ActivationInputs, role: str, *, prompt: str | None = None
) -> dict[str, object]:
    job = source.jobs[role]
    operations = source.install_spec["operations"]
    assert isinstance(operations, list)
    operation = operations[0 if role == "router" else 1]
    assert isinstance(operation, dict)
    arguments = operation["arguments"]
    assert isinstance(arguments, dict)
    return {
        "protocol": "hermes-cron-definition/v1",
        "job_id": job["job_id"],
        "effective_state": "paused",
        "definition": {
            "name": arguments["name"],
            "prompt": arguments["prompt"] if prompt is None else prompt,
            "schedule": {"kind": "interval", "minutes": 2},
            "repeat": {"times": None},
            "skills": arguments["skills"],
            "model": None,
            "provider": None,
            "provider_snapshot": None,
            "model_snapshot": None,
            "base_url": None,
            "script": arguments["script"],
            "no_agent": arguments["no_agent"],
            "monitor_script": None,
            "monitor_url": None,
            "context_from": [],
            "enabled_toolsets": arguments["enabled_toolsets"],
            "workdir": None,
            "attach_to_session": arguments["attach_to_session"],
            "continuity": arguments["continuity"],
            "deliver": [arguments["deliver"]],
        },
    }


def _fake_hermes(tmp_path: Path, source: ActivationInputs) -> tuple[Path, Path]:
    definitions = _private_profile_home(tmp_path) / "definitions.json"
    definitions.write_text(
        json.dumps(
            {
                str(source.jobs["router"]["job_id"]): _definition_payload(source, "router"),
                str(source.jobs["courier"]["job_id"]): _definition_payload(source, "courier"),
            }
        ),
        encoding="utf-8",
    )
    binary = tmp_path / "hermes"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('Hermes Agent v0.9.0 (2026.8.20)')\n"
        "    print('Install directory: /synthetic/hermes')\n"
        "    print('Python: 3.11.15')\n"
        "    print('OpenAI SDK: 2.24.0')\n"
        "elif len(sys.argv) == 5 and sys.argv[1:3] == ['cron', 'show'] "
        "and sys.argv[4] == '--json':\n"
        "    path = pathlib.Path(os.environ['HERMES_HOME']) / 'definitions.json'\n"
        "    values = json.loads(path.read_text())\n"
        "    value = values.get(sys.argv[3])\n"
        "    if value is None:\n"
        "        raise SystemExit(1)\n"
        "    print(json.dumps(value, sort_keys=True, separators=(',', ':')))\n"
        "else:\n"
        "    raise SystemExit(2)\n",
        encoding="utf-8",
    )
    binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return binary, definitions


def test_loader_recollects_version_and_both_exact_definitions_from_hermes_cli(
    tmp_path: Path,
) -> None:
    source, home, evidence_path = _staged_current_evidence(tmp_path)
    binary, _definitions = _fake_hermes(tmp_path, source)
    stale = json.loads(evidence_path.read_text(encoding="utf-8"))
    stale["hermes_version"] = "stale-version"
    stale["jobs"]["router"]["schedule"] = "stale schedule"
    evidence_path.write_text(json.dumps(stale), encoding="utf-8")

    loaded = load_activation_inputs(
        activation_path=source.activation_path,
        hermes_home=home,
        evidence_path=evidence_path,
        hermes_binary=str(binary),
    )

    assert loaded.hermes_version == "0.9.0"
    assert loaded.jobs["router"]["schedule"] == "every 2m"
    assert loaded.jobs["router"]["prompt_sha256"] == source.jobs["router"]["prompt_sha256"]
    assert loaded.jobs["courier"]["job_id"] == source.jobs["courier"]["job_id"]


def test_loader_sets_exact_authorized_profile_home_in_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, home, evidence_path = _staged_current_evidence(tmp_path)
    payloads = {
        str(source.jobs["router"]["job_id"]): _definition_payload(source, "router"),
        str(source.jobs["courier"]["job_id"]): _definition_payload(source, "courier"),
    }
    calls: list[dict[str, str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(kwargs["env"])
        output = (
            b"Hermes Agent v0.9.0 (2026.8.20)\n"
            if argv[1:] == ["--version"]
            else json.dumps(payloads[argv[3]]).encode("utf-8")
        )
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other" / ".hermes" / "profiles" / "other"))
    monkeypatch.setenv("API_TOKEN", "secret")
    monkeypatch.setattr(activation_module.subprocess, "run", fake_run)
    load_activation_inputs(
        activation_path=source.activation_path,
        hermes_home=home,
        evidence_path=evidence_path,
    )

    assert len(calls) == 3
    assert all(environment["HERMES_HOME"] == str(home) for environment in calls)
    assert all("API_TOKEN" not in environment for environment in calls)


def test_definition_adapter_uses_fixed_argv_bounds_and_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _inputs(tmp_path)
    payloads = {
        str(source.jobs["router"]["job_id"]): _definition_payload(source, "router"),
        str(source.jobs["courier"]["job_id"]): _definition_payload(source, "courier"),
    }
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        if argv[1:] == ["--version"]:
            output = b"Hermes Agent v0.9.0 (2026.8.20)\n"
        else:
            output = json.dumps(payloads[argv[3]]).encode("utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(activation_module.subprocess, "run", fake_run)
    adapter = HermesCronDefinitionAdapter(
        hermes_home=_private_profile_home(tmp_path),
        environment={"PATH": "/usr/bin", "HOME": str(tmp_path), "API_TOKEN": "secret"},
    )
    version, jobs = adapter.collect(
        {
            "router": str(source.jobs["router"]["job_id"]),
            "courier": str(source.jobs["courier"]["job_id"]),
        }
    )

    assert version == "0.9.0"
    assert jobs["router"]["prompt_sha256"] == source.jobs["router"]["prompt_sha256"]
    assert [call[0][1:] for call in calls] == [
        ["--version"],
        ["cron", "show", "job-router", "--json"],
        ["cron", "show", "job-courier", "--json"],
    ]
    assert all(call[1]["shell"] is False and call[1]["text"] is False for call in calls)
    assert all("API_TOKEN" not in call[1]["env"] for call in calls)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(protocol="wrong"),
        lambda value: value.update(job_id="other"),
        lambda value: value.update(effective_state="failed"),
        lambda value: value["definition"].update(extra=True),
        lambda value: value["definition"].update(name="bad name"),
        lambda value: value["definition"].update(prompt=1),
        lambda value: value["definition"].update(schedule={"kind": "interval", "minutes": True}),
        lambda value: value["definition"].update(repeat={"times": 0}),
        lambda value: value["definition"].update(skills=["duplicate", "duplicate"]),
        lambda value: value["definition"].update(model=[]),
        lambda value: value["definition"].update(no_agent=1),
        lambda value: value["definition"].update(continuity=1),
        lambda value: value["definition"].update(attach_to_session=None),
    ],
)
def test_definition_adapter_rejects_wrong_protocol_identity_state_and_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: object
) -> None:
    source = _inputs(tmp_path)
    payload = _definition_payload(source, "router")
    mutation(payload)  # type: ignore[operator]

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        output = (
            b"Hermes Agent v0.9.0 (2026.8.20)\n"
            if argv[1:] == ["--version"]
            else json.dumps(payload).encode("utf-8")
        )
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(activation_module.subprocess, "run", fake_run)
    with pytest.raises(ValidationError, match="definition"):
        HermesCronDefinitionAdapter(hermes_home=_private_profile_home(tmp_path)).collect(
            {"router": "job-router", "courier": "job-courier"}
        )


@pytest.mark.parametrize(
    ("completed", "message"),
    [
        (subprocess.CompletedProcess([], 1, stdout=b"private", stderr=b"private"), "failed"),
        (subprocess.CompletedProcess([], 0, stdout=b"x" * 1025, stderr=b""), "output bound"),
        (subprocess.CompletedProcess([], 0, stdout=b"\xff", stderr=b""), "UTF-8"),
        (subprocess.CompletedProcess([], 0, stdout=b"not-json", stderr=b""), "JSON"),
    ],
)
def test_definition_adapter_rejects_command_and_output_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[bytes],
    message: str,
) -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if argv[1:] == ["--version"] and message == "JSON":
            return subprocess.CompletedProcess(
                argv, 0, stdout=b"Hermes Agent v0.9.0 (2026.8.20)\n", stderr=b""
            )
        return completed

    monkeypatch.setattr(activation_module.subprocess, "run", fake_run)
    with pytest.raises(ValidationError, match=message):
        HermesCronDefinitionAdapter(
            hermes_home=_private_profile_home(tmp_path), max_output_bytes=1024
        ).collect({"router": "job-router", "courier": "job-courier"})


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.TimeoutExpired(["hermes"], 1), "timed out"),
        (FileNotFoundError("synthetic private path"), "unavailable"),
    ],
)
def test_definition_adapter_rejects_timeout_and_missing_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException, message: str
) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise failure

    monkeypatch.setattr(activation_module.subprocess, "run", fail)
    with pytest.raises(ValidationError, match=message) as caught:
        HermesCronDefinitionAdapter(hermes_home=_private_profile_home(tmp_path)).collect(
            {"router": "job-router", "courier": "job-courier"}
        )
    assert "synthetic private path" not in str(caught.value)


def test_definition_adapter_rejects_invalid_configuration_identity_and_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _private_profile_home(tmp_path)
    with pytest.raises(ValidationError, match="binary"):
        HermesCronDefinitionAdapter(binary="", hermes_home=home)
    with pytest.raises(ValidationError, match="bounds"):
        HermesCronDefinitionAdapter(hermes_home=home, timeout_seconds=0)
    adapter = HermesCronDefinitionAdapter(hermes_home=home)
    with pytest.raises(ValidationError, match="incomplete"):
        adapter.collect({"router": "job-router"})
    with pytest.raises(ValidationError, match="identity"):
        adapter.collect({"router": "bad id", "courier": "job-courier"})
    with pytest.raises(ValidationError, match="distinct"):
        adapter.collect({"router": "same", "courier": "same"})

    monkeypatch.setattr(
        activation_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=b"unsupported version output\n", stderr=b""
        ),
    )
    with pytest.raises(ValidationError, match="version"):
        adapter.collect({"router": "job-router", "courier": "job-courier"})


@pytest.mark.parametrize(
    ("schedule", "repeat", "expected_schedule", "expected_repeat"),
    [
        ({"kind": "cron", "expr": "*/2 * * * *"}, {"times": 2}, "*/2 * * * *", 2),
        (
            {"kind": "once", "run_at": "2026-08-21T00:00:00Z"},
            {"times": None},
            "2026-08-21T00:00:00Z",
            "forever",
        ),
        ({"kind": "legacy", "value": "every 2m"}, {"times": None}, "every 2m", "forever"),
    ],
)
def test_definition_adapter_normalizes_supported_schedule_and_repeat_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule: dict[str, object],
    repeat: dict[str, object],
    expected_schedule: object,
    expected_repeat: object,
) -> None:
    source = _inputs(tmp_path)
    payload = _definition_payload(source, "router")
    payload["definition"]["schedule"] = schedule  # type: ignore[index]
    payload["definition"]["repeat"] = repeat  # type: ignore[index]

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        output = (
            b"Hermes Agent v0.9.0 (2026.8.20)\n"
            if argv[1:] == ["--version"]
            else json.dumps({**payload, "job_id": argv[3]}).encode("utf-8")
        )
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(activation_module.subprocess, "run", fake_run)
    adapter = HermesCronDefinitionAdapter(hermes_home=_private_profile_home(tmp_path))
    _version, jobs = adapter.collect({"router": "job-router", "courier": "job-courier"})
    assert jobs["router"]["schedule"] == expected_schedule
    assert jobs["router"]["repeat"] == expected_repeat


def test_definition_adapter_preserves_all_normalized_delivery_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _inputs(tmp_path)
    payload = _definition_payload(source, "router")
    payload["definition"]["deliver"] = ["telegram", "local"]  # type: ignore[index]

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        output = (
            b"Hermes Agent v0.9.0 (2026.8.20)\n"
            if argv[1:] == ["--version"]
            else json.dumps({**payload, "job_id": argv[3]}).encode("utf-8")
        )
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(activation_module.subprocess, "run", fake_run)
    adapter = HermesCronDefinitionAdapter(hermes_home=_private_profile_home(tmp_path))
    _version, jobs = adapter.collect({"router": "job-router", "courier": "job-courier"})
    assert jobs["router"]["deliver"] == ["telegram", "local"]


def test_actual_prompt_drift_after_attestation_denies_router_and_matches_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, home, evidence_path = _staged_current_evidence(tmp_path)
    binary, definitions_path = _fake_hermes(tmp_path, source)
    ambient_home = tmp_path / ".hermes" / "profiles" / "other"
    ambient_home.mkdir(parents=True, mode=0o700)
    ambient_definitions = {
        str(source.jobs["router"]["job_id"]): _definition_payload(
            source, "router", prompt="ambient unauthorized prompt"
        ),
        str(source.jobs["courier"]["job_id"]): _definition_payload(
            source, "courier", prompt="ambient unauthorized prompt"
        ),
    }
    (ambient_home / "definitions.json").write_text(
        json.dumps(ambient_definitions), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(ambient_home))

    def current_verdict() -> ActivationVerdict:
        return activation_verdict(
            load_activation_inputs(
                activation_path=source.activation_path,
                hermes_home=home,
                evidence_path=evidence_path,
                hermes_binary=str(binary),
            )
        )

    assert current_verdict().reason == "supported"
    definitions = json.loads(definitions_path.read_text(encoding="utf-8"))
    router_id = str(source.jobs["router"]["job_id"])
    definitions[router_id]["definition"]["prompt"] += " drift"
    definitions_path.write_text(json.dumps(definitions), encoding="utf-8")

    ledger_path = tmp_path / "router-ledger.db"
    with Ledger.create(ledger_path) as ledger:
        ledger.register_mission("synthetic-release", "b" * 64)
        incident = IncidentObservation(
            mission_id="synthetic-release",
            phase_key="verify",
            kind="phase_failed",
            subject_task_id="t_example",
            subject_run_id=None,
            severity="critical",
            observation_sha256="a" * 64,
            expected_state="done",
            observed_state="failed",
        )
        ledger.observe_manager_incidents([incident], tick_seq=1, now=100.0)
        ledger.observe_manager_incidents([incident], tick_seq=2, now=101.0)
        assert manager_router_gate(
            ledger, now=101.0, environment={}, activation_check=current_verdict
        ) == {"wakeAgent": False}
        assert ledger.manager_budget("synthetic-release", "1970-01-01") == 0
        assert ledger._connection.execute("SELECT COUNT(*) FROM wake_attempts").fetchone() == (0,)

    class UnusedCollector:
        def collect(self, board: str, task_ids: list[str]) -> dict[str, object]:
            raise AssertionError((board, task_ids))

    projection = run_tick(
        parse_manifest(manifest_data()),
        tmp_path / "missing-ledger.db",
        tmp_path / "status.json",
        UnusedCollector(),
        now=101.0,
        activation_check=current_verdict,
    )
    verdict = current_verdict()
    assert verdict.reason == "job_definition_drift"
    assert (
        projection["supervisor"]["compatibility_state"],
        projection["supervisor"]["wake_enabled"],
    ) == verdict.public


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra=True),
        lambda value: value.update(protocol="wrong"),
        lambda value: value.update(source="preview-only"),
        lambda value: value.update(profile="other"),
        lambda value: value.update(hermes_version=1),
        lambda value: value.update(jobs=[]),
        lambda value: value["jobs"].pop("router"),
        lambda value: value["seam_evidence"].update(quiet_agent_calls=1),
    ],
)
def test_current_evidence_envelope_is_closed(tmp_path: Path, mutate: object) -> None:
    source, home, evidence_path = _staged_current_evidence(tmp_path)
    value = json.loads(evidence_path.read_text(encoding="utf-8"))
    mutate(value)  # type: ignore[operator]
    evidence_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValidationError, match="evidence schema"):
        load_activation_inputs(
            activation_path=source.activation_path,
            hermes_home=home,
            evidence_path=evidence_path,
        )


def test_loader_rejects_unsafe_evidence_spec_script_and_profile(tmp_path: Path) -> None:
    source, home, evidence_path = _staged_current_evidence(tmp_path)
    evidence_path.chmod(0o644)
    with pytest.raises(ValidationError, match="unsafe"):
        load_activation_inputs(
            activation_path=source.activation_path, hermes_home=home, evidence_path=evidence_path
        )
    evidence_path.chmod(0o600)
    for noncanonical_home in (
        tmp_path / "default",
        tmp_path / ".hermes" / "profiles" / "default",
        tmp_path / ".hermes" / "profiles" / "other",
    ):
        with pytest.raises(ValidationError, match="noncanonical"):
            load_activation_inputs(
                activation_path=source.activation_path,
                hermes_home=noncanonical_home,
                evidence_path=evidence_path,
            )
    home.chmod(0o750)
    with pytest.raises(ValidationError, match=r"profile.*unsafe"):
        load_activation_inputs(
            activation_path=source.activation_path, hermes_home=home, evidence_path=evidence_path
        )
    home.chmod(0o700)
    linked_home = tmp_path / "linked-hermes"
    linked_home.symlink_to(home, target_is_directory=True)
    with pytest.raises(ValidationError, match=r"noncanonical|unsafe"):
        load_activation_inputs(
            activation_path=source.activation_path,
            hermes_home=linked_home,
            evidence_path=evidence_path,
        )
    spec_path = home / "cyclops" / "manager-install.json"
    spec_path.chmod(0o644)
    with pytest.raises(ValidationError, match="unsafe"):
        load_activation_inputs(
            activation_path=source.activation_path, hermes_home=home, evidence_path=evidence_path
        )
    spec_path.chmod(0o600)
    script_path = home / "scripts" / "cyclops-manager-router.py"
    script_path.chmod(0o644)
    with pytest.raises(ValidationError, match="script is unsafe"):
        load_activation_inputs(
            activation_path=source.activation_path, hermes_home=home, evidence_path=evidence_path
        )


def test_definition_adapter_rejects_missing_and_wrong_owner_profile_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing" / ".hermes"
    with pytest.raises(ValidationError, match="unavailable"):
        HermesCronDefinitionAdapter(hermes_home=missing)

    home = _private_profile_home(tmp_path)
    owner = home.stat().st_uid
    monkeypatch.setattr(activation_module.os, "getuid", lambda: owner + 1)
    with pytest.raises(ValidationError, match=r"profile.*unsafe"):
        HermesCronDefinitionAdapter(hermes_home=home)


def test_activation_apply_requires_recollection_and_stable_target(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    with pytest.raises(ValidationError, match="recollection"):
        activate_manager(inputs, now="2026-08-20T09:00:00Z", apply=True, environment={})
    other = replace(inputs, activation_path=tmp_path / "other.json")
    with pytest.raises(ValidationError, match="target changed"):
        activate_manager(
            inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: other,
        )


def test_validator_exercises_closed_private_drift_reasons(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    record = json.loads(inputs.activation_path.read_text(encoding="utf-8"))
    record["release"] = "0.2.0"
    inputs.activation_path.write_text(json.dumps(record), encoding="utf-8")
    assert activation_verdict(inputs).reason == "release_drift"

    inputs = _inputs(tmp_path)
    record = json.loads(inputs.activation_path.read_text(encoding="utf-8"))
    record["seam_evidence_protocol"] = "wrong"
    inputs.activation_path.write_text(json.dumps(record), encoding="utf-8")
    assert activation_verdict(inputs).reason == "seam_protocol_drift"

    inputs = _inputs(tmp_path)
    jobs = {name: dict(job) for name, job in inputs.jobs.items()}
    jobs["router"].pop("prompt_sha256")
    assert activation_verdict(replace(inputs, jobs=jobs)).reason == "prompt_unverifiable"

    inputs = _inputs(tmp_path)
    jobs = {name: dict(job) for name, job in inputs.jobs.items()}
    jobs["router"]["extra"] = True
    assert activation_verdict(replace(inputs, jobs=jobs)).reason == "job_definition_drift"


def test_private_json_loader_rejects_missing_bad_json_and_duplicate_keys(tmp_path: Path) -> None:
    source, home, evidence_path = _staged_current_evidence(tmp_path)
    evidence_path.unlink()
    with pytest.raises(ValidationError, match="unavailable"):
        load_activation_inputs(
            activation_path=source.activation_path, hermes_home=home, evidence_path=evidence_path
        )
    evidence_path.write_text("{", encoding="utf-8")
    evidence_path.chmod(0o600)
    with pytest.raises(ValidationError, match="unavailable"):
        load_activation_inputs(
            activation_path=source.activation_path, hermes_home=home, evidence_path=evidence_path
        )
    evidence_path.write_text('{"protocol":"x","protocol":"y"}', encoding="utf-8")
    with pytest.raises(ValidationError, match="unavailable"):
        load_activation_inputs(
            activation_path=source.activation_path, hermes_home=home, evidence_path=evidence_path
        )


def test_activation_and_deactivation_defensive_failures_are_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = _inputs(tmp_path)
    inputs.activation_path.unlink()
    with pytest.raises(ValidationError, match="timestamp"):
        activate_manager(inputs, now="bad", environment={})
    with pytest.raises(ValidationError, match="task scope"):
        deactivate_manager(
            inputs.activation_path,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={"HERMES_DELEGATED_CHILD": "1"},
        )

    lock = inputs.activation_path.with_suffix(".lock")
    lock.symlink_to(tmp_path / "missing-lock")
    with pytest.raises(ValidationError, match="lock"):
        activate_manager(
            inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: inputs,
        )
    lock.unlink()

    monkeypatch.setattr(
        activation_module,
        "activation_verdict",
        lambda _inputs: ActivationVerdict("malformed", "unsupported", False),
    )
    with pytest.raises(ValidationError, match="readback"):
        activate_manager(
            inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: inputs,
        )
    monkeypatch.undo()
    inputs.activation_path.chmod(0o644)
    with pytest.raises(ValidationError, match="unsafe"):
        deactivate_manager(
            inputs.activation_path,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
        )
