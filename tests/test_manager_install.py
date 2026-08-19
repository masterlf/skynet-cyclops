from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import skynet_cyclops.manager_install as manager_install
from skynet_cyclops.errors import ValidationError
from skynet_cyclops.manager_install import (
    build_cron_install_spec,
    execute_cron_install_spec,
    stage_cron_install,
)

NONCE = "7" * 64


def visible_job(name: str, job_id: str) -> dict[str, object]:
    return {
        "job_id": job_id,
        "name": name,
        "skill": None,
        "skills": [],
        "prompt_preview": "Cyclops managed job",
        "model": None,
        "provider": None,
        "base_url": None,
        "schedule": "every 2m",
        "repeat": "forever",
        "deliver": "local",
        "next_run_at": "2026-08-20T00:00:00Z",
        "last_run_at": None,
        "last_status": None,
        "last_delivery_error": None,
        "last_fire_error": None,
        "enabled": False,
        "state": "paused",
        "paused_at": "2026-08-19T00:00:00Z",
        "paused_reason": "cyclops-install",
        "script": "cyclops-manager-router.py",
        "enabled_toolsets": ["no_mcp"],
    }


def test_initial_spec_refuses_any_stable_name_conflict() -> None:
    with pytest.raises(ValidationError, match="stable name conflict"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="install",
            visible_jobs=[visible_job("cyclops-manager-router", "job-existing")],
            attempt_nonce=NONCE,
        )


def test_initial_spec_is_closed_machine_readable_and_rolls_back_new_ids() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    assert set(spec) == {
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
    }
    assert spec["protocol"] == "cyclops-cron-install/v1"
    assert spec["release"] == "0.2.0"
    assert spec["attempt_nonce"] == NONCE
    assert [item["action"] for item in spec["operations"]] == ["create", "create"]
    assert spec["operations"][0]["arguments"]["enabled_toolsets"] == ["no_mcp"]
    assert spec["operations"][0]["arguments"]["deliver"] == "local"
    assert spec["operations"][1]["arguments"]["no_agent"] is True
    assert all(item["requires_created_job_id"] is True for item in spec["rollback"])
    assert json.loads(json.dumps(spec, sort_keys=True)) == spec


def test_upgrade_requires_exact_prior_spec_and_full_visible_snapshot() -> None:
    installed = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce="6" * 64,
    )
    with pytest.raises(ValidationError, match="prior spec"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="upgrade",
            visible_jobs=[
                visible_job("cyclops-manager-router", "job-router"),
                visible_job("cyclops-decision-courier", "job-courier"),
            ],
            attempt_nonce=NONCE,
        )
    incomplete = visible_job("cyclops-manager-router", "job-router")
    incomplete.pop("paused_reason")
    with pytest.raises(ValidationError, match="full-field snapshot"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="upgrade",
            visible_jobs=[
                incomplete,
                visible_job("cyclops-decision-courier", "job-courier"),
            ],
            previous_spec=installed,
            attempt_nonce=NONCE,
        )


def test_upgrade_rollback_uses_update_with_prior_arguments() -> None:
    installed = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce="6" * 64,
    )
    snapshot = [
        visible_job("cyclops-manager-router", "job-router"),
        visible_job("cyclops-decision-courier", "job-courier"),
    ]
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="upgrade",
        visible_jobs=snapshot,
        previous_spec=installed,
        attempt_nonce=NONCE,
    )
    assert [item["action"] for item in spec["operations"]] == ["update", "update"]
    assert all(item["action"] == "update" for item in spec["rollback"])
    assert [item["job_id"] for item in spec["rollback"]] == ["job-courier", "job-router"]
    assert spec["rollback"][1]["arguments"] == {
        **installed["operations"][0]["arguments"],
        "job_id": "job-router",
    }


def test_executor_removes_only_created_jobs_after_partial_install_failure() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    calls: list[tuple[str, str | None]] = []

    def tool(action: str, **arguments: Any) -> dict[str, object]:
        calls.append((action, arguments.get("job_id")))
        if action == "create" and len([item for item in calls if item[0] == "create"]) == 1:
            return {"success": True, "job_id": "new-router", "job": {"state": "paused"}}
        if action == "remove":
            return {"success": True}
        return {"success": False, "error": "synthetic failure"}

    report = execute_cron_install_spec(spec, tool)
    assert report == {
        "state": "rolled_back",
        "operation": "install",
        "created_job_ids": ["new-router"],
        "rollback_failures": [],
    }
    assert calls == [
        ("create", None),
        ("pause", "new-router"),
        ("remove", "new-router"),
    ]


def test_executor_restores_upgrade_with_update_until_converged() -> None:
    installed = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce="6" * 64,
    )
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="upgrade",
        visible_jobs=[
            visible_job("cyclops-manager-router", "job-router"),
            visible_job("cyclops-decision-courier", "job-courier"),
        ],
        previous_spec=installed,
        attempt_nonce=NONCE,
    )
    updates = 0
    calls: list[tuple[str, str | None]] = []

    def tool(action: str, **arguments: Any) -> dict[str, object]:
        nonlocal updates
        calls.append((action, arguments.get("job_id")))
        if action == "update":
            updates += 1
            if updates == 2:
                return {"success": False, "error": "synthetic failure"}
        return {"success": True, "job": {"state": "paused", "enabled": False}}

    report = execute_cron_install_spec(spec, tool)
    assert report["state"] == "rolled_back"
    assert calls == [
        ("update", "job-router"),
        ("update", "job-courier"),
        ("update", "job-courier"),
        ("update", "job-router"),
    ]


def test_stage_writes_only_private_hash_verified_artifacts(tmp_path: Path) -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    result = stage_cron_install(spec, tmp_path / "profile")
    assert result["state"] == "staged"
    scripts = tmp_path / "profile" / "scripts"
    config = tmp_path / "profile" / "cyclops" / "manager-install.json"
    assert stat.S_IMODE(scripts.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert json.loads(config.read_text(encoding="utf-8")) == spec
    for artifact in spec["artifacts"]:
        path = scripts / artifact["name"]
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not any("cron" in path.name for path in (tmp_path / "profile").iterdir())


def test_stage_rejects_symlink_without_partial_replacement(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    scripts = profile / "scripts"
    scripts.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("operator-owned\n", encoding="utf-8")
    (scripts / "cyclops-manager-router.py").symlink_to(outside)
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    with pytest.raises(ValidationError, match="symbolic link"):
        stage_cron_install(spec, profile)
    assert outside.read_text(encoding="utf-8") == "operator-owned\n"
    assert not (profile / "cyclops" / "manager-install.json").exists()


def test_nonce_is_high_entropy_and_input_is_strict() -> None:
    first = build_cron_install_spec(
        profile="default", home_delivery="telegram", operation="install", visible_jobs=[]
    )
    second = build_cron_install_spec(
        profile="default", home_delivery="telegram", operation="install", visible_jobs=[]
    )
    assert len(first["attempt_nonce"]) == 64
    assert first["attempt_nonce"] != second["attempt_nonce"]
    with pytest.raises(ValidationError, match="nonce"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="install",
            visible_jobs=[],
            attempt_nonce="short",
        )
    assert os.environ.get("HERMES_HOME") != str(Path("/definitely-not-used"))


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"profile": "manager"}, "profile"),
        ({"home_delivery": "bad delivery"}, "home_delivery"),
        ({"operation": "replace"}, "operation"),
        ({"visible_jobs": "jobs"}, "snapshot"),
        ({"visible_jobs": [object()]}, "snapshot"),
    ],
)
def test_install_spec_rejects_untyped_boundaries(overrides: dict[str, object], match: str) -> None:
    arguments: dict[str, object] = {
        "profile": "default",
        "home_delivery": "telegram",
        "operation": "install",
        "visible_jobs": [],
        "attempt_nonce": NONCE,
    }
    arguments.update(overrides)
    with pytest.raises(ValidationError, match=match):
        build_cron_install_spec(**arguments)  # type: ignore[arg-type]


def test_snapshot_rejects_bad_and_duplicate_identities() -> None:
    bad_id = visible_job("unrelated", "")
    with pytest.raises(ValidationError, match="job id"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="install",
            visible_jobs=[bad_id],
            attempt_nonce=NONCE,
        )
    duplicate = [visible_job("one", "same"), visible_job("two", "same")]
    with pytest.raises(ValidationError, match="identity"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="install",
            visible_jobs=duplicate,
            attempt_nonce=NONCE,
        )


def test_upgrade_rejects_incompatible_prior_spec_and_unpaused_job() -> None:
    installed = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce="6" * 64,
    )
    snapshot = [
        visible_job("cyclops-manager-router", "job-router"),
        visible_job("cyclops-decision-courier", "job-courier"),
    ]
    incompatible = dict(installed)
    incompatible["release"] = "9.9.9"
    with pytest.raises(ValidationError, match="incompatible"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="upgrade",
            visible_jobs=snapshot,
            previous_spec=incompatible,
            attempt_nonce=NONCE,
        )
    snapshot[0]["enabled"] = True
    snapshot[0]["state"] = "scheduled"
    with pytest.raises(ValidationError, match="paused"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="upgrade",
            visible_jobs=snapshot,
            previous_spec=installed,
            attempt_nonce=NONCE,
        )


def test_stage_and_executor_reject_tampered_specs(tmp_path: Path) -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    tampered = dict(spec)
    tampered["extra"] = True
    with pytest.raises(ValidationError, match="schema"):
        stage_cron_install(tampered, tmp_path / "profile")
    with pytest.raises(ValidationError, match="profile home"):
        stage_cron_install(spec, Path("relative"))
    tampered = json.loads(json.dumps(spec))
    tampered["artifacts"][0]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="integrity"):
        stage_cron_install(tampered, tmp_path / "profile")
    with pytest.raises(ValidationError, match="execution spec"):
        execute_cron_install_spec({**spec, "protocol": "wrong"}, lambda **_kwargs: {})


def test_executor_success_and_failed_rollback_are_explicit() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    created = 0

    def success_tool(action: str, **_arguments: Any) -> dict[str, object]:
        nonlocal created
        if action == "create":
            created += 1
            return {"success": True, "job_id": f"job-{created}"}
        if action == "pause":
            return {"success": True, "job": {"state": "paused", "enabled": False}}
        return {"success": True}

    assert execute_cron_install_spec(spec, success_tool) == {
        "state": "applied_paused",
        "operation": "install",
        "created_job_ids": ["job-1", "job-2"],
        "rollback_failures": [],
    }

    def failed_tool(action: str, **_arguments: Any) -> dict[str, object]:
        if action == "create":
            return {"success": True}
        return {"success": False}

    report = execute_cron_install_spec(spec, failed_tool)
    assert report["state"] == "rolled_back"
    assert report["created_job_ids"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("operations", []),
        lambda value: value["operations"][0].__setitem__("stable_name", "wrong"),
        lambda value: value["operations"][0].__setitem__("arguments", "wrong"),
    ],
)
def test_upgrade_rejects_malformed_prior_spec(mutate: Any) -> None:
    installed = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce="6" * 64,
    )
    malformed = json.loads(json.dumps(installed))
    mutate(malformed)
    with pytest.raises(ValidationError, match="prior spec"):
        build_cron_install_spec(
            profile="default",
            home_delivery="telegram",
            operation="upgrade",
            visible_jobs=[
                visible_job("cyclops-manager-router", "job-router"),
                visible_job("cyclops-decision-courier", "job-courier"),
            ],
            previous_spec=malformed,
            attempt_nonce=NONCE,
        )


def test_stage_rejects_unsafe_directories_targets_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValidationError, match="directory"):
        stage_cron_install(spec, linked)

    profile = tmp_path / "profile"
    target = profile / "scripts" / "cyclops-manager-router.py"
    target.mkdir(parents=True)
    with pytest.raises(ValidationError, match="regular file"):
        stage_cron_install(spec, profile)
    target.rmdir()
    target.write_text("old\n", encoding="utf-8")
    target.chmod(0o644)
    with pytest.raises(ValidationError, match="permissions"):
        stage_cron_install(spec, profile)

    malformed = json.loads(json.dumps(spec))
    malformed["artifacts"] = []
    with pytest.raises(ValidationError, match="artifacts"):
        stage_cron_install(malformed, tmp_path / "other")
    malformed = json.loads(json.dumps(spec))
    malformed["artifacts"][0]["extra"] = True
    with pytest.raises(ValidationError, match="artifact schema"):
        stage_cron_install(malformed, tmp_path / "another")

    current_uid = os.getuid()
    monkeypatch.setattr(manager_install.os, "getuid", lambda: current_uid + 1)
    with pytest.raises(ValidationError, match="ownership"):
        stage_cron_install(spec, tmp_path / "owned")


def test_stage_fails_closed_on_write_and_readback_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )

    def fail_fchmod(*_args: object) -> None:
        raise OSError("synthetic")

    monkeypatch.setattr(manager_install.os, "fchmod", fail_fchmod)
    with pytest.raises(ValidationError, match="write failed"):
        stage_cron_install(spec, tmp_path / "write-fail")
    monkeypatch.undo()

    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"tampered")
    with pytest.raises(ValidationError, match="hash verification"):
        stage_cron_install(spec, tmp_path / "hash-fail")
    monkeypatch.undo()

    monkeypatch.setattr(manager_install.json, "loads", lambda _value: {})
    with pytest.raises(ValidationError, match="config verification"):
        stage_cron_install(spec, tmp_path / "config-fail")


def test_executor_rejects_bad_operations_and_reports_rollback_failures() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    with pytest.raises(ValidationError, match="execution spec"):
        execute_cron_install_spec({**spec, "operations": None}, lambda **_kwargs: {})
    with pytest.raises(ValidationError, match="operation"):
        execute_cron_install_spec({**spec, "operations": [None]}, lambda **_kwargs: {})
    bad_action = json.loads(json.dumps(spec))
    bad_action["operations"][0]["action"] = "resume"
    with pytest.raises(ValidationError, match="action"):
        execute_cron_install_spec(bad_action, lambda **_kwargs: {})

    rollback_bad = json.loads(json.dumps(spec))
    rollback_bad["rollback"] = [None, {"action": "resume", "stable_name": "bad"}]
    report = execute_cron_install_spec(
        rollback_bad, lambda _action, **_arguments: {"success": False}
    )
    assert report["state"] == "rollback_failed"
    assert report["rollback_failures"] == ["invalid-rollback-entry", "bad"]

    def remove_fails(action: str, **_arguments: Any) -> dict[str, object]:
        if action == "create":
            return {"success": True, "job_id": "created"}
        return {"success": False}

    report = execute_cron_install_spec(spec, remove_fails)
    assert report["state"] == "rollback_failed"
    assert report["rollback_failures"] == ["cyclops-manager-router"]


def test_executor_rolls_back_after_tool_exception_or_unverified_pause() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    calls: list[tuple[str, str | None]] = []

    def raises_on_second_create(action: str, **arguments: Any) -> dict[str, object]:
        calls.append((action, arguments.get("job_id")))
        if action == "create" and len([call for call in calls if call[0] == "create"]) == 1:
            return {"success": True, "job_id": "created-router"}
        if action == "pause":
            return {"success": True, "job": {"state": "paused", "enabled": False}}
        if action == "remove":
            return {"success": True}
        raise RuntimeError("synthetic tool failure")

    report = execute_cron_install_spec(spec, raises_on_second_create)
    assert report["state"] == "rolled_back"
    assert calls[-1] == ("remove", "created-router")

    def pause_not_verified(action: str, **_arguments: Any) -> dict[str, object]:
        if action == "create":
            return {"success": True, "job_id": "created-router"}
        if action == "pause":
            return {"success": True, "job": {"state": "scheduled", "enabled": True}}
        return {"success": True}

    report = execute_cron_install_spec(spec, pause_not_verified)
    assert report["state"] == "rolled_back"
