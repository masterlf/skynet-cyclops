from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import manifest_data, write_manifest

from skynet_cyclops import cli
from skynet_cyclops.cli import ExitCode, main
from skynet_cyclops.config import load_config
from skynet_cyclops.errors import (
    AdapterError,
    CyclopsError,
    LedgerError,
    ProjectionError,
    ValidationError,
)
from skynet_cyclops.ledger import Ledger
from skynet_cyclops.manifest import canonical_manifest_hash, parse_manifest
from skynet_cyclops.tick import run_tick


class Collector:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = tasks
        self.calls: list[str] = []

    def collect(self, board: str, task_ids: list[str]) -> dict[str, object]:
        self.calls.append(board)
        assert sorted(task_ids) == ["a", "b", "c"]
        return {"tasks": self.tasks, "runs": [], "diagnostics": []}


def config_file(tmp_path: Path, manifest: Path) -> Path:
    config = {
        "schema_version": 1,
        "manifest_path": str(manifest),
        "ledger_path": str(tmp_path / "state" / "ledger.db"),
        "status_path": str(tmp_path / "state" / "status.json"),
        "hermes_binary": "hermes",
        "incident_debounce_ticks": 2,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_incident_debounce_and_post_gap_observe_only(tmp_path: Path) -> None:
    manifest = parse_manifest(manifest_data())
    ledger_path = tmp_path / "ledger.db"
    status_path = tmp_path / "status.json"
    with Ledger.create(ledger_path) as ledger:
        ledger.register_mission(manifest.mission.id, canonical_manifest_hash(manifest))
        for phase, task_id in (("build", "a"), ("review", "b"), ("verify", "c")):
            ledger.bind(manifest.mission.id, phase, task_id, f"key-{phase}")
    collector = Collector([])
    first = run_tick(manifest, ledger_path, status_path, collector, now=1000.0, debounce_ticks=2)
    assert first["incidents"][0]["disposition"] == "observing"
    second = run_tick(manifest, ledger_path, status_path, collector, now=1120.0, debounce_ticks=2)
    assert second["incidents"][0]["disposition"] == "active"
    gap = run_tick(manifest, ledger_path, status_path, collector, now=2000.0, debounce_ticks=2)
    assert gap["supervisor"]["post_gap"] is True
    assert gap["supervisor"]["mode"] == "observe"
    assert collector.calls == ["default", "default", "default"]
    assert "actions" not in json.dumps(gap)


def test_collection_failure_does_not_advance_tick_gap_or_debounce_state(tmp_path: Path) -> None:
    manifest = parse_manifest(manifest_data())
    ledger_path = tmp_path / "ledger.db"
    status_path = tmp_path / "status.json"
    with Ledger.create(ledger_path) as ledger:
        ledger.register_mission(manifest.mission.id, canonical_manifest_hash(manifest))
        for phase, task_id in (("build", "a"), ("review", "b"), ("verify", "c")):
            ledger.bind(manifest.mission.id, phase, task_id, f"key-{phase}")
    collector = Collector([])
    first = run_tick(manifest, ledger_path, status_path, collector, now=1000.0, debounce_ticks=2)
    assert first["supervisor"]["tick_seq"] == 1

    class FailingCollector:
        def collect(self, board: str, task_ids: list[str]) -> dict[str, object]:
            raise AdapterError("synthetic adapter outage")

    with pytest.raises(AdapterError):
        run_tick(
            manifest, ledger_path, status_path, FailingCollector(), now=1200.0, debounce_ticks=2
        )
    with pytest.raises(AdapterError):
        run_tick(
            manifest, ledger_path, status_path, FailingCollector(), now=1800.0, debounce_ticks=2
        )
    connection = sqlite3.connect(ledger_path)
    assert connection.execute(
        "SELECT tick_seq, last_heartbeat FROM meta WHERE singleton=1"
    ).fetchone() == (1, 1000.0)
    connection.close()
    recovered = run_tick(
        manifest, ledger_path, status_path, collector, now=2000.0, debounce_ticks=2
    )
    assert recovered["supervisor"]["tick_seq"] == 2
    assert recovered["supervisor"]["post_gap"] is True
    assert recovered["incidents"][0]["observed_ticks"] == 2
    assert recovered["incidents"][0]["disposition"] == "observing"
    resumed = run_tick(manifest, ledger_path, status_path, collector, now=2010.0, debounce_ticks=2)
    assert resumed["supervisor"]["post_gap"] is False
    assert resumed["incidents"][0]["disposition"] == "active"


def test_crash_before_tick_commit_rolls_back_manager_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = parse_manifest(manifest_data())
    ledger_path = tmp_path / "ledger.db"
    with Ledger.create(ledger_path) as ledger:
        ledger.register_mission(manifest.mission.id, canonical_manifest_hash(manifest))
        for phase, task_id in (("build", "a"), ("review", "b"), ("verify", "c")):
            ledger.bind(manifest.mission.id, phase, task_id, f"key-{phase}")

    def crash(_ledger: Ledger, _sequence: int, _now: float) -> None:
        raise LedgerError("synthetic pre-commit crash")

    monkeypatch.setattr(Ledger, "commit_tick", crash)
    with pytest.raises(LedgerError, match="synthetic"):
        run_tick(
            manifest,
            ledger_path,
            tmp_path / "status.json",
            Collector([]),
            now=1000.0,
        )
    monkeypatch.undo()
    with Ledger.open(ledger_path) as ledger:
        assert ledger.tick_state() == (0, None)
        assert ledger.manager_incidents() == []


def test_missing_ledger_projects_fail_closed_without_collection(tmp_path: Path) -> None:
    manifest = parse_manifest(manifest_data())
    collector = Collector([])
    payload = run_tick(
        manifest, tmp_path / "missing.db", tmp_path / "status.json", collector, now=1.0
    )
    assert payload["supervisor"]["mode"] == "observe"
    assert payload["supervisor"]["state"] == "critical"
    assert collector.calls == []


def test_cli_validate_bootstrap_dry_default_status_and_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = write_manifest(tmp_path / "mission.yaml")
    assert main(["manifest", "validate", str(manifest_path)]) == ExitCode.OK
    assert "manifest_sha256" in capsys.readouterr().out
    assert main(["bootstrap", str(manifest_path)]) == ExitCode.OK
    dry = json.loads(capsys.readouterr().out)
    assert dry["mode"] == "dry-run"
    assert len(dry["plan"]) == 3
    assert main(["manifest", "validate", str(tmp_path / "missing.yaml")]) == ExitCode.INVALID_INPUT
    assert "error:" in capsys.readouterr().err


def test_cli_status_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest_path = write_manifest(tmp_path / "mission.yaml")
    config = config_file(tmp_path, manifest_path)
    payload = {
        "schema_version": 1,
        "projection_version": 1,
        "supervisor": {
            "mode": "observe",
            "state": "ok",
            "heartbeat_at": 1.0,
            "tick_seq": 1,
            "post_gap": False,
        },
        "missions": [],
        "incidents": [],
        "cost": {"classification": "unknown"},
    }
    status_path = tmp_path / "state" / "status.json"
    status_path.parent.mkdir()
    from skynet_cyclops.projection import write_projection

    write_projection(status_path, payload)
    assert main(["status", "--config", str(config), "--json"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out) == payload


def test_cli_manager_install_dry_run_emits_spec_and_apply_only_stages(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "manager",
        "install",
        "--profile",
        "default",
        "--home-delivery",
        "telegram",
    ]
    assert main(arguments) == ExitCode.OK
    spec = json.loads(capsys.readouterr().out)
    assert spec["protocol"] == "cyclops-cron-install/v1"
    assert spec["release"] == "0.2.0"
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("[]\n", encoding="utf-8")
    profile = tmp_path / "profile"
    assert (
        main(
            [
                *arguments,
                "--apply",
                "--snapshot",
                str(snapshot),
                "--hermes-home",
                str(profile),
            ]
        )
        == ExitCode.OK
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["protocol"] == "cyclops-cron-install/v1"
    assert (profile / "scripts" / "cyclops-manager-router.py").is_file()
    assert (profile / "cyclops" / "manager-install.json").is_file()
    assert not (profile / "cron").exists()
    assert main([*arguments, "--operation", "upgrade"]) == ExitCode.INVALID_INPUT
    assert "snapshot" in capsys.readouterr().err


def test_cli_manager_router_denies_task_scope_and_courier_is_silent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path = write_manifest(tmp_path / "mission.yaml")
    config = config_file(tmp_path, manifest_path)
    with Ledger.create(tmp_path / "state" / "ledger.db"):
        pass
    assert main(["manager", "router", "--config", str(config)]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out) == {"wakeAgent": False}
    assert main(["manager", "courier", "--config", str(config)]) == ExitCode.OK
    assert capsys.readouterr().out == ""


def test_manifest_binding_mismatch_fails_closed_without_collection(tmp_path: Path) -> None:
    manifest = parse_manifest(manifest_data())
    ledger_path = tmp_path / "ledger.db"
    with Ledger.create(ledger_path) as ledger:
        ledger.register_mission(manifest.mission.id, "0" * 64)
    collector = Collector([])
    payload = run_tick(manifest, ledger_path, tmp_path / "status.json", collector, now=1.0)
    assert payload["supervisor"]["state"] == "critical"
    assert payload["incidents"][0]["kind"] == "manifest_binding_mismatch"
    assert collector.calls == []


def test_completed_tick_has_no_incidents_and_reports_done(tmp_path: Path) -> None:
    manifest = parse_manifest(manifest_data())
    ledger_path = tmp_path / "ledger.db"
    with Ledger.create(ledger_path) as ledger:
        ledger.register_mission(manifest.mission.id, canonical_manifest_hash(manifest))
        for phase, task_id in (("build", "a"), ("review", "b"), ("verify", "c")):
            ledger.bind(manifest.mission.id, phase, task_id, f"key-{phase}")
    rows = [
        {"id": "a", "status": "done", "evidence": ["commit", "tests"]},
        {"id": "b", "status": "done", "evidence": ["review_outcome"]},
        {"id": "c", "status": "done", "evidence": ["checksums"]},
    ]
    payload = run_tick(manifest, ledger_path, tmp_path / "status.json", Collector(rows), now=1.0)
    assert payload["supervisor"]["state"] == "ok"
    assert payload["missions"][0]["outcome"] == "done"
    assert payload["incidents"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("schema_version", 2),
        lambda value: value.__setitem__("ledger_path", ""),
        lambda value: value.__setitem__("hermes_binary", "bad\x00binary"),
        lambda value: value.__setitem__("incident_debounce_ticks", True),
        lambda value: value.update(extra=True),
    ],
)
def test_config_rejects_unsupported_or_unsafe_values(tmp_path: Path, mutate: object) -> None:
    manifest = write_manifest(tmp_path / "mission.yaml")
    path = config_file(tmp_path, manifest)
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(value)  # type: ignore[operator]
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ValidationError, match="configuration"):
        load_config(path)


def test_config_rejects_symlink_oversize_and_malformed_yaml(tmp_path: Path) -> None:
    manifest = write_manifest(tmp_path / "mission.yaml")
    real = config_file(tmp_path, manifest)
    link = tmp_path / "linked.yaml"
    link.symlink_to(real)
    with pytest.raises(ValidationError, match="regular file"):
        load_config(link)
    real.write_bytes(b"x" * 70_000)
    with pytest.raises(ValidationError, match="too large"):
        load_config(real)
    real.write_text("[unterminated", encoding="utf-8")
    with pytest.raises(ValidationError, match="unavailable"):
        load_config(real)


def test_config_rejects_unsafe_owner_and_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = write_manifest(tmp_path / "mission.yaml")
    path = config_file(tmp_path, manifest)
    path.chmod(0o620)
    with pytest.raises(ValidationError, match="permissions"):
        load_config(path)
    path.chmod(0o600)
    owner = path.stat().st_uid
    monkeypatch.setattr("skynet_cyclops.config.os.getuid", lambda: owner + 1)
    with pytest.raises(ValidationError, match="ownership"):
        load_config(path)


def test_cli_bootstrap_apply_creates_missing_ledger_and_tick_status_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = write_manifest(tmp_path / "mission.yaml")
    config = config_file(tmp_path, manifest_path)
    expected = {"build": "task-1", "review": "task-2", "verify": "task-3"}
    monkeypatch.setattr(cli, "apply_bootstrap", lambda *_args: expected)
    assert (
        main(["bootstrap", str(manifest_path), "--apply", "--config", str(config)]) == ExitCode.OK
    )
    assert json.loads(capsys.readouterr().out) == {"mode": "applied", "bindings": expected}
    assert (tmp_path / "state" / "ledger.db").exists()

    tick_payload = {
        "schema_version": 1,
        "projection_version": 1,
        "supervisor": {
            "mode": "observe",
            "state": "ok",
            "heartbeat_at": 1.0,
            "tick_seq": 7,
            "post_gap": False,
        },
        "missions": [],
        "incidents": [],
        "cost": {"classification": "unknown"},
    }
    monkeypatch.setattr(cli, "run_tick", lambda *_args, **_kwargs: tick_payload)
    assert main(["tick", "--config", str(config)]) == ExitCode.OK
    assert capsys.readouterr().out == ""
    assert main(["tick", "--config", str(config), "--json"]) == ExitCode.OK
    assert json.loads(capsys.readouterr().out) == tick_payload

    from skynet_cyclops.projection import write_projection

    write_projection(tmp_path / "state" / "status.json", tick_payload)
    assert main(["status", "--config", str(config)]) == ExitCode.OK
    assert capsys.readouterr().out == "mode=observe state=ok tick=7 incidents=0\n"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AdapterError("adapter failed"), ExitCode.EXTERNAL_FAILURE),
        (LedgerError("ledger failed"), ExitCode.STATE_UNAVAILABLE),
        (ProjectionError("projection failed"), ExitCode.STATE_UNAVAILABLE),
        (CyclopsError("internal failed"), ExitCode.INTERNAL_ERROR),
    ],
)
def test_cli_maps_safe_error_classes_to_stable_exit_codes(
    error: Exception,
    expected: ExitCode,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_args: object) -> ExitCode:
        raise error

    monkeypatch.setattr(cli, "_execute", fail)
    assert main(["status", "--config", "synthetic.yaml"]) == expected
    output = capsys.readouterr().err
    assert output.startswith("error: ")
    assert str(error) in output
