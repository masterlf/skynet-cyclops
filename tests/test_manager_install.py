from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest

import skynet_cyclops.manager_install as manager_install
from skynet_cyclops.errors import ValidationError
from skynet_cyclops.manager import MANAGER_PROMPT
from skynet_cyclops.manager_install import (
    build_cron_install_spec,
    execute_cron_install_spec,
    stage_cron_install,
)

NONCE = "7" * 64


def visible_job(name: str, job_id: str) -> dict[str, object]:
    courier = name == "cyclops-decision-courier"
    prompt = "" if courier else MANAGER_PROMPT
    return {
        "job_id": job_id,
        "name": name,
        "skill": None,
        "skills": [],
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        "model": None,
        "provider": None,
        "base_url": None,
        "schedule": "every 2m",
        "repeat": "forever",
        "deliver": "telegram" if courier else "local",
        "next_run_at": "2026-08-20T00:00:00Z",
        "last_run_at": None,
        "last_status": None,
        "last_delivery_error": None,
        "last_fire_error": None,
        "enabled": False,
        "state": "paused",
        "paused_at": "2026-08-19T00:00:00Z",
        "paused_reason": "cyclops-install",
        "script": "cyclops-decision-courier.py" if courier else "cyclops-manager-router.py",
        **({"no_agent": True} if courier else {"enabled_toolsets": ["no_mcp"]}),
    }


def rendered_job(
    arguments: dict[str, object], job_id: str, *, state: str = "paused"
) -> dict[str, object]:
    prompt = str(arguments["prompt"])
    result: dict[str, object] = {
        "job_id": job_id,
        "name": arguments["name"],
        "skill": None,
        "skills": arguments["skills"],
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        "model": None,
        "provider": None,
        "base_url": None,
        "schedule": arguments["schedule"],
        "repeat": "forever",
        "deliver": arguments["deliver"],
        "next_run_at": "synthetic-next",
        "last_run_at": None,
        "last_status": None,
        "last_delivery_error": None,
        "last_fire_error": None,
        "enabled": state != "paused",
        "state": state,
        "paused_at": "synthetic-paused" if state == "paused" else None,
        "paused_reason": "cyclops-install" if state == "paused" else None,
        "script": arguments["script"],
    }
    if arguments["no_agent"] is True:
        result["no_agent"] = True
    if arguments["enabled_toolsets"]:
        result["enabled_toolsets"] = arguments["enabled_toolsets"]
    return result


def valid_seam_evidence() -> dict[str, object]:
    return {
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


class FakeCronTool:
    def __init__(
        self,
        jobs: list[dict[str, object]] | None = None,
        *,
        fail_action: str | None = None,
        fail_occurrence: int = 1,
        pause_state: str = "paused",
    ) -> None:
        self.jobs = {str(job["job_id"]): dict(job) for job in jobs or []}
        self.calls: list[tuple[str, str | None]] = []
        self.counts: dict[str, int] = {}
        self.created = 0
        self.fail_action = fail_action
        self.fail_occurrence = fail_occurrence
        self.pause_state = pause_state
        self.full_prompts = {
            str(job["job_id"]): "" if job["name"] == "cyclops-decision-courier" else MANAGER_PROMPT
            for job in jobs or []
        }

    def __call__(self, action: str, **arguments: Any) -> dict[str, object]:
        self.calls.append((action, arguments.get("job_id")))
        self.counts[action] = self.counts.get(action, 0) + 1
        if action == self.fail_action and self.counts[action] == self.fail_occurrence:
            return {"success": False, "error": "synthetic failure"}
        if action == "create":
            self.created += 1
            job_id = f"created-{self.created}"
            self.jobs[job_id] = rendered_job(arguments, job_id, state="scheduled")
            self.full_prompts[job_id] = str(arguments["prompt"])
            return {"success": True, "job_id": job_id}
        if action == "pause":
            job = self.jobs[str(arguments["job_id"])]
            paused = self.pause_state == "paused"
            job.update(
                state=self.pause_state,
                enabled=not paused,
                paused_at="synthetic-paused" if paused else None,
                paused_reason="cyclops-install" if paused else None,
            )
            return {"success": True, "job": job}
        if action == "update":
            job_id = str(arguments["job_id"])
            self.jobs[job_id] = rendered_job(arguments, job_id)
            self.full_prompts[job_id] = str(arguments["prompt"])
            return {"success": True, "job": self.jobs[job_id]}
        if action == "remove":
            job_id = str(arguments["job_id"])
            self.jobs.pop(job_id, None)
            self.full_prompts.pop(job_id, None)
            return {"success": True}
        if action == "list":
            return {"success": True, "jobs": list(self.jobs.values())}
        raise AssertionError(action)

    def read_full_job(self, job_id: str) -> dict[str, object] | None:
        if job_id not in self.full_prompts:
            return None
        return {"id": job_id, "prompt": self.full_prompts[job_id]}


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
        "snapshot",
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
    tool = FakeCronTool(fail_action="create", fail_occurrence=2)
    report = execute_cron_install_spec(spec, tool)
    assert report == {
        "state": "rolled_back",
        "operation": "install",
        "created_job_ids": ["created-1"],
        "rollback_failures": [],
    }
    assert [call for call in tool.calls if call[0] != "list"] == [
        ("create", None),
        ("pause", "created-1"),
        ("create", None),
        ("remove", "created-1"),
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
    tool = FakeCronTool(
        [
            visible_job("cyclops-manager-router", "job-router"),
            visible_job("cyclops-decision-courier", "job-courier"),
        ],
        fail_action="update",
        fail_occurrence=2,
    )
    report = execute_cron_install_spec(spec, tool)
    assert report["state"] == "rolled_back"
    assert [call for call in tool.calls if call[0] != "list"] == [
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
    with pytest.raises(ValidationError, match="spec schema"):
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
    success_tool = FakeCronTool()
    assert execute_cron_install_spec(spec, success_tool, seam_verifier=valid_seam_evidence) == {
        "state": "applied_paused",
        "operation": "install",
        "created_job_ids": ["created-1", "created-2"],
        "rollback_failures": [],
    }
    assert success_tool.counts["list"] == 4

    def failed_tool(action: str, **_arguments: Any) -> dict[str, object]:
        if action == "create":
            return {"success": True}
        if action == "list":
            return {"success": True, "jobs": []}
        return {"success": False}

    report = execute_cron_install_spec(spec, failed_tool)
    assert report["state"] == "rolled_back"
    assert report["created_job_ids"] == []


def test_executor_rolls_back_wrong_full_field_readback_and_failed_seam() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    jobs: dict[str, dict[str, object]] = {}
    full_prompts: dict[str, str] = {}
    created = 0

    def tool(action: str, **arguments: Any) -> dict[str, object]:
        nonlocal created
        if action == "create":
            created += 1
            job_id = f"job-{created}"
            jobs[job_id] = rendered_job(arguments, job_id, state="scheduled")
            full_prompts[job_id] = str(arguments["prompt"])
            return {"success": True, "job_id": job_id}
        if action == "pause":
            jobs[str(arguments["job_id"])].update(
                state="paused",
                enabled=False,
                paused_at="synthetic-paused",
                paused_reason="cyclops-install",
            )
            return {"success": True, "job": jobs[str(arguments["job_id"])]}
        if action == "list":
            return {"success": True, "jobs": list(jobs.values())}
        if action == "remove":
            job_id = str(arguments["job_id"])
            jobs.pop(job_id, None)
            full_prompts.pop(job_id, None)
            return {"success": True}
        raise AssertionError(action)

    def read_full_job(job_id: str) -> dict[str, object] | None:
        if job_id not in full_prompts:
            return None
        return {"id": job_id, "prompt": full_prompts[job_id]}

    report = execute_cron_install_spec(
        spec,
        tool,
        seam_verifier=valid_seam_evidence,
        full_job_reader=read_full_job,
    )
    assert report["state"] == "applied_paused"
    assert len(jobs) == 2

    def wrong_prompt_tool(action: str, **arguments: Any) -> dict[str, object]:
        result = tool(action, **arguments)
        if action == "list" and jobs:
            first = next(iter(jobs.values()))
            first["prompt_preview"] = "wrong"
        return result

    jobs.clear()
    created = 0
    report = execute_cron_install_spec(
        spec,
        wrong_prompt_tool,
        seam_verifier=valid_seam_evidence,
        full_job_reader=read_full_job,
    )
    assert report["state"] == "rolled_back"
    assert jobs == {}

    jobs.clear()
    created = 0
    report = execute_cron_install_spec(
        spec, tool, seam_verifier=lambda: {}, full_job_reader=read_full_job
    )
    assert report["state"] == "rolled_back"
    assert jobs == {}


def test_executor_consumes_supported_cronjob_json_results() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    stateful = FakeCronTool()

    def json_tool(action: str, **arguments: Any) -> str:
        return json.dumps(stateful(action, **arguments))

    report = execute_cron_install_spec(
        spec,
        json_tool,
        seam_verifier=valid_seam_evidence,
        full_job_reader=stateful.read_full_job,
    )
    assert report["state"] == "applied_paused"


def test_tool_result_and_verification_boundaries_fail_closed() -> None:
    with pytest.raises(ValidationError, match="execution spec"):
        manager_install._digest("short")
    assert manager_install._tool_mapping('{"success":true}') == {"success": True}
    assert manager_install._tool_mapping("not-json") is None
    assert manager_install._tool_mapping("[]") is None
    assert manager_install._tool_mapping("x" * 1_000_001) is None
    assert manager_install._tool_mapping([]) is None
    assert manager_install._seam_evidence_is_valid(valid_seam_evidence())
    incomplete = valid_seam_evidence()
    incomplete.pop("cronjob_full_field_readback")
    assert not manager_install._seam_evidence_is_valid(incomplete)
    extra = valid_seam_evidence()
    extra["unexpected"] = True
    assert not manager_install._seam_evidence_is_valid(extra)


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


@pytest.mark.parametrize("operations", [[None, None], [{}, None]])
def test_upgrade_rejects_non_mapping_prior_operations(operations: list[object]) -> None:
    installed = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce="6" * 64,
    )
    malformed = json.loads(json.dumps(installed))
    malformed["operations"] = operations

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
    with pytest.raises(ValidationError, match="spec schema"):
        stage_cron_install(malformed, tmp_path / "other")
    malformed = json.loads(json.dumps(spec))
    malformed["artifacts"][0]["extra"] = True
    with pytest.raises(ValidationError, match="spec schema"):
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

    def fail_private_write(*_args: object) -> None:
        raise ValidationError("private staging write failed")

    monkeypatch.setattr(manager_install, "_atomic_private_write", fail_private_write)
    with pytest.raises(ValidationError, match="write failed"):
        stage_cron_install(spec, tmp_path / "write-fail")
    monkeypatch.undo()

    real_read_bytes = Path.read_bytes
    reads = 0

    def tamper_first_read(path: Path) -> bytes:
        nonlocal reads
        reads += 1
        return b"tampered" if reads == 1 else real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", tamper_first_read)
    with pytest.raises(ValidationError, match="hash verification"):
        hash_fail_profile = tmp_path / "hash-fail"
        stage_cron_install(spec, hash_fail_profile)
    assert not (hash_fail_profile / "scripts" / "cyclops-manager-router.py").exists()
    assert not (hash_fail_profile / "scripts" / "cyclops-decision-courier.py").exists()
    assert not (hash_fail_profile / "cyclops" / "manager-install.json").exists()
    monkeypatch.undo()

    monkeypatch.setattr(manager_install.json, "loads", lambda _value: {})
    with pytest.raises(ValidationError, match="config verification"):
        config_fail_profile = tmp_path / "config-fail"
        stage_cron_install(spec, config_fail_profile)
    assert not (config_fail_profile / "scripts" / "cyclops-manager-router.py").exists()
    assert not (config_fail_profile / "scripts" / "cyclops-decision-courier.py").exists()
    assert not (config_fail_profile / "cyclops" / "manager-install.json").exists()


@pytest.mark.parametrize("fail_on_write", [2, 3])
def test_stage_write_failure_restores_exact_pre_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on_write: int
) -> None:
    profile = tmp_path / "profile"
    scripts = profile / "scripts"
    config_dir = profile / "cyclops"
    scripts.mkdir(parents=True)
    config_dir.mkdir()
    targets = [
        scripts / "cyclops-manager-router.py",
        scripts / "cyclops-decision-courier.py",
        config_dir / "manager-install.json",
    ]
    for index, target in enumerate(targets):
        target.write_bytes(f"old-{index}".encode())
        target.chmod(0o400 if index == 0 else 0o600)
    before = [(target.read_bytes(), stat.S_IMODE(target.stat().st_mode)) for target in targets]
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    real_write = manager_install._atomic_private_write
    writes = 0

    def fail_after_prior_replacements(path: Path, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == fail_on_write:
            raise ValidationError("synthetic write failure")
        real_write(path, content)

    monkeypatch.setattr(manager_install, "_atomic_private_write", fail_after_prior_replacements)
    with pytest.raises(ValidationError, match="synthetic write failure"):
        stage_cron_install(spec, profile)

    after = [(target.read_bytes(), stat.S_IMODE(target.stat().st_mode)) for target in targets]
    assert after == before


@pytest.mark.parametrize("fail_on_write", [2, 3])
def test_stage_write_failure_removes_files_created_by_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_on_write: int
) -> None:
    profile = tmp_path / "profile"
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    real_write = manager_install._atomic_private_write
    writes = 0

    def fail_after_prior_replacements(path: Path, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == fail_on_write:
            raise ValidationError("synthetic write failure")
        real_write(path, content)

    monkeypatch.setattr(manager_install, "_atomic_private_write", fail_after_prior_replacements)
    with pytest.raises(ValidationError, match="synthetic write failure"):
        stage_cron_install(spec, profile)

    assert not (profile / "scripts" / "cyclops-manager-router.py").exists()
    assert not (profile / "scripts" / "cyclops-decision-courier.py").exists()
    assert not (profile / "cyclops" / "manager-install.json").exists()


def test_stage_rollback_refuses_file_changed_outside_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    router = profile / "scripts" / "cyclops-manager-router.py"
    router.parent.mkdir(parents=True)
    router.write_text("old-router", encoding="utf-8")
    router.chmod(0o600)
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    real_write = manager_install._atomic_private_write
    writes = 0

    def change_then_fail(path: Path, content: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            router.write_text("operator-change", encoding="utf-8")
            raise ValidationError("synthetic write failure")
        real_write(path, content)

    monkeypatch.setattr(manager_install, "_atomic_private_write", change_then_fail)
    with pytest.raises(ValidationError, match="rollback failed"):
        stage_cron_install(spec, profile)

    assert router.read_text(encoding="utf-8") == "operator-change"


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
    calls = 0

    def must_not_call(_action: str, **_arguments: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"success": False}

    with pytest.raises(ValidationError, match="execution spec"):
        execute_cron_install_spec(rollback_bad, must_not_call)
    assert calls == 0

    def remove_fails(action: str, **_arguments: Any) -> dict[str, object]:
        if action == "create":
            return {"success": True, "job_id": "created"}
        return {"success": False}

    report = execute_cron_install_spec(spec, remove_fails)
    assert report["state"] == "rollback_failed"
    assert report["rollback_failures"] == ["cyclops-manager-router", "readback"]


@pytest.mark.parametrize(
    "tamper",
    [
        lambda value: value.__setitem__("extra", True),
        lambda value: value.__setitem__("profile", "manager"),
        lambda value: value.__setitem__("snapshot_sha256", "0" * 64),
        lambda value: value["artifacts"][0].__setitem__("content", "tampered"),
        lambda value: value["operations"][1].__setitem__("arguments", "wrong"),
        lambda value: value.__setitem__("rollback", []),
        lambda value: value["operations"][0].__setitem__("stable_name", "unrelated"),
        lambda value: value["operations"][0]["arguments"].__setitem__("deliver", "all"),
        lambda value: value["rollback"].reverse(),
    ],
)
def test_executor_rejects_semantic_tampering_before_tool_call(tamper: Any) -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    tampered = json.loads(json.dumps(spec))
    tamper(tampered)
    calls = 0

    def tool(_action: str, **_arguments: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"success": True}

    with pytest.raises(ValidationError, match="execution spec"):
        execute_cron_install_spec(tampered, tool)
    assert calls == 0


def test_executor_rejects_unrelated_upgrade_target_before_tool_call() -> None:
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
    tampered = json.loads(json.dumps(spec))
    tampered["operations"][0]["arguments"]["job_id"] = "unrelated-job"
    calls = 0

    def tool(_action: str, **_arguments: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"success": True}

    with pytest.raises(ValidationError, match="execution spec"):
        execute_cron_install_spec(tampered, tool)
    assert calls == 0


def test_executor_binds_consistently_tampered_upgrade_ids_to_snapshot() -> None:
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
    tampered = json.loads(json.dumps(spec))
    tampered["operations"][0]["job_id"] = "unrelated-job"
    tampered["operations"][0]["arguments"]["job_id"] = "unrelated-job"
    tampered["rollback"][1]["job_id"] = "unrelated-job"
    tampered["rollback"][1]["arguments"]["job_id"] = "unrelated-job"
    calls = 0

    def tool(_action: str, **_arguments: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"success": True}

    with pytest.raises(ValidationError, match="execution spec"):
        execute_cron_install_spec(tampered, tool)
    assert calls == 0


def test_executor_rejects_direct_verification_tampering_before_tool_call() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    verification = spec["verification"]
    assert isinstance(verification, dict)
    verification["jobs_paused"] = False
    calls = 0

    def tool(_action: str, **_arguments: Any) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"success": True}

    with pytest.raises(ValidationError, match="execution spec"):
        execute_cron_install_spec(spec, tool)
    assert calls == 0


def test_executor_rolls_back_after_tool_exception_or_unverified_pause() -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    stateful = FakeCronTool()

    def raises_on_second_create(action: str, **arguments: Any) -> dict[str, object]:
        if action == "create" and stateful.counts.get("create", 0) == 1:
            stateful.calls.append((action, arguments.get("job_id")))
            stateful.counts[action] = 2
            raise RuntimeError("synthetic tool failure")
        return stateful(action, **arguments)

    report = execute_cron_install_spec(spec, raises_on_second_create)
    assert report["state"] == "rolled_back"
    assert stateful.calls[-2:] == [("remove", "created-1"), ("list", None)]

    pause_not_verified = FakeCronTool(pause_state="scheduled")
    report = execute_cron_install_spec(spec, pause_not_verified)
    assert report["state"] == "rolled_back"


def test_executor_requires_non_truncated_prompt_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = build_cron_install_spec(
        profile="default",
        home_delivery="telegram",
        operation="install",
        visible_jobs=[],
        attempt_nonce=NONCE,
    )
    monkeypatch.setattr(
        manager_install,
        "_run_hermes_seam_verifier",
        lambda: valid_seam_evidence(),
        raising=False,
    )
    tool = FakeCronTool()
    report = execute_cron_install_spec(spec, tool, full_job_reader=tool.read_full_job)
    assert report["state"] == "applied_paused"

    tampered = FakeCronTool()

    def truncated_reader(job_id: str) -> dict[str, object] | None:
        job = tampered.read_full_job(job_id)
        if job is not None and job["prompt"] == MANAGER_PROMPT:
            job["prompt"] = MANAGER_PROMPT[:100]
        return job

    report = execute_cron_install_spec(spec, tampered, full_job_reader=truncated_reader)
    assert report["state"] == "rolled_back"
    assert tampered.jobs == {}


def test_seam_verifier_subprocess_has_private_minimal_environment_and_no_parent_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_before = dict(os.environ)
    os.environ.update(
        {
            "HERMES_HOME": str(tmp_path / "live-profile"),
            "HERMES_KANBAN_TASK": "synthetic-task",
            "HERMES_DELEGATION_PARENT": "synthetic-parent",
            "UNRELATED_SECRET": "must-not-be-inherited",
        }
    )
    expected_parent = dict(os.environ)
    context_home: ContextVar[str] = ContextVar("hermes_home_override")
    override_token = context_home.set(str(tmp_path / "context-profile"))
    live_sentinel = tmp_path / "live-profile" / "sentinel"
    live_sentinel.parent.mkdir()
    live_sentinel.write_text("unchanged", encoding="utf-8")
    observed: list[dict[str, str]] = []
    stop = threading.Event()

    def observe_parent() -> None:
        while not stop.is_set():
            observed.append(dict(os.environ))
            time.sleep(0.001)

    thread = threading.Thread(target=observe_parent)
    thread.start()

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert set(environment) == {
            "HOME",
            "HERMES_HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTHONNOUSERSITE",
            "PYTHONUTF8",
        }
        assert environment["HOME"] != os.environ["HOME"]
        assert Path(environment["HERMES_HOME"]).is_relative_to(Path(environment["HOME"]))
        assert kwargs["timeout"] == 60
        assert kwargs["shell"] is False
        assert str(kwargs["cwd"]) == environment["HOME"]
        assert "capture_output" not in kwargs
        stdout: Any = kwargs["stdout"]
        assert hasattr(stdout, "write")
        stdout.write(json.dumps(valid_seam_evidence()).encode() + b"\n")
        stdout.flush()
        time.sleep(0.02)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=None,
            stderr=None,
        )

    monkeypatch.setattr(manager_install.subprocess, "run", fake_run, raising=False)
    try:
        assert manager_install._run_hermes_seam_verifier() == valid_seam_evidence()
    finally:
        stop.set()
        thread.join()
        context_home.reset(override_token)
        os.environ.clear()
        os.environ.update(parent_before)
    assert observed
    assert all(snapshot == expected_parent for snapshot in observed)
    assert live_sentinel.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize(
    ("stdout_bytes", "stderr_bytes", "returncode", "message"),
    [
        (b"not-json\n", b"", 0, "output is invalid"),
        (b"{}\n", b"", 0, "evidence is invalid"),
        (b"{}\n{}\n", b"", 0, "output is invalid"),
        (b"\xff\n", b"", 0, "output is invalid"),
        (b"x" * 4097, b"", 0, "verifier failed"),
        (b"", b"x" * 4097, 0, "verifier failed"),
        (b"", b"", 1, "verifier failed"),
    ],
)
def test_seam_verifier_rejects_unbounded_or_noncanonical_child_output(
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    returncode: int,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        command: Any = args[0]
        stdout: Any = kwargs["stdout"]
        stderr: Any = kwargs["stderr"]
        stdout.write(stdout_bytes)
        stderr.write(stderr_bytes)
        stdout.flush()
        stderr.flush()
        return subprocess.CompletedProcess(args=command, returncode=returncode)

    monkeypatch.setattr(manager_install.subprocess, "run", fake_run)
    with pytest.raises(ValidationError, match=message):
        manager_install._run_hermes_seam_verifier()
