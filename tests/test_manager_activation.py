from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import skynet_cyclops.activation as activation_module
from skynet_cyclops.activation import (
    ACTIVATION_PROTOCOL,
    RELEASE,
    ActivationInputs,
    ActivationVerdict,
    activate_manager,
    activation_verdict,
    canonical_sha256,
    deactivate_manager,
    job_definition_sha256,
    load_activation_inputs,
)
from skynet_cyclops.errors import ValidationError
from skynet_cyclops.manager_install import build_cron_install_spec, stage_cron_install


def _job(job_id: str, name: str, *, prompt: str, no_agent: bool) -> dict[str, object]:
    return {
        "job_id": job_id,
        "name": name,
        "schedule": "every 2m",
        "repeat": "forever",
        "deliver": "local" if not no_agent else "telegram",
        "skills": [],
        "model": None,
        "provider": None,
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
            "deliver": arguments["deliver"],
            "skills": arguments["skills"],
            "model": None,
            "provider": None,
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


def test_stale_current_evidence_fails_closed(tmp_path: Path) -> None:
    inputs = replace(
        _inputs(tmp_path),
        evidence_collected_at="2026-08-20T08:00:00Z",
        validated_at="2026-08-20T09:00:00Z",
    )
    assert activation_verdict(inputs).reason == "seam_evidence_drift"
    inputs.activation_path.unlink()
    with pytest.raises(ValidationError, match="stale"):
        activate_manager(
            inputs,
            now="2026-08-20T09:00:00Z",
            apply=True,
            environment={},
            refresh=lambda: inputs,
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
    home = tmp_path / ".hermes" / "profiles" / "default"
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

    def collect() -> ActivationInputs:
        return load_activation_inputs(
            activation_path=activation_path,
            hermes_home=home,
            evidence_path=evidence_path,
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
    home = tmp_path / ".hermes" / "profiles" / "default"
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
    with pytest.raises(ValidationError, match="noncanonical"):
        load_activation_inputs(
            activation_path=source.activation_path,
            hermes_home=tmp_path / "default",
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
