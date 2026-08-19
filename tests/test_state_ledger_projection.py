from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import manifest_data

from skynet_cyclops import projection
from skynet_cyclops.errors import LedgerError, ProjectionError, ValidationError
from skynet_cyclops.ledger import Ledger
from skynet_cyclops.manifest import parse_manifest
from skynet_cyclops.projection import read_projection, write_projection
from skynet_cyclops.state import Collection, derive_mission_state


def tasks() -> list[dict[str, object]]:
    return [
        {
            "id": "task-1",
            "idempotency_key": "unused",
            "title": "Synthetic",
            "status": "done",
            "assignee": "builder",
            "evidence": ["commit", "tests"],
            "retry_count": 0,
            "depends_on": [],
        },
        {
            "id": "task-2",
            "idempotency_key": "unused-2",
            "title": "Synthetic review",
            "status": "running",
            "assignee": "reviewer",
            "evidence": [],
            "retry_count": 0,
            "depends_on": ["task-1"],
            "active_run_id": "run-1",
        },
        {
            "id": "task-3",
            "idempotency_key": "unused-3",
            "title": "Synthetic verify",
            "status": "pending",
            "assignee": "release",
            "evidence": [],
            "retry_count": 0,
            "depends_on": ["task-2"],
        },
    ]


def test_ledger_schema_security_and_corruption_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "private" / "ledger.db"
    with Ledger.create(path) as ledger:
        assert ledger.schema_version == 2
        assert ledger.mode == "observe"
        pragmas = ledger.pragmas()
        assert pragmas["foreign_keys"] == 1
        assert pragmas["synchronous"] == 2
        assert pragmas["busy_timeout"] <= 5000
    assert os.stat(path.parent).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    with pytest.raises(LedgerError, match="missing"):
        Ledger.open(tmp_path / "missing.db")
    path.write_bytes(b"not sqlite")
    with pytest.raises(LedgerError, match="unavailable"):
        Ledger.open(path)


def test_ledger_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "ledger.db"
    link.symlink_to(target)
    with pytest.raises(LedgerError, match="regular file"):
        Ledger.open(link)


def test_pure_deterministic_state_derivation() -> None:
    manifest = parse_manifest(manifest_data())
    bindings = {"build": "task-1", "review": "task-2", "verify": "task-3"}
    collection = Collection(
        tasks=tasks(),
        runs=[
            {"id": "run-1", "task_id": "task-2", "status": "running", "heartbeat_age_seconds": 12}
        ],
        diagnostics=[],
    )
    first = derive_mission_state(manifest, bindings, collection)
    second = derive_mission_state(manifest, bindings, collection)
    assert first == second
    assert [phase.state for phase in first.phases] == ["done", "review", "pending"]
    assert first.next_phase == "review"
    assert first.outcome == "running"
    assert first.workers[0].run_id == "run-1"


def test_state_derivation_is_topological_but_preserves_manifest_display_order() -> None:
    data = manifest_data()
    data["phases"] = list(reversed(data["phases"]))
    manifest = parse_manifest(data)
    bindings = {"build": "task-1", "review": "task-2", "verify": "task-3"}
    state = derive_mission_state(
        manifest,
        bindings,
        Collection(
            tasks=tasks(),
            runs=[
                {
                    "id": "run-1",
                    "task_id": "task-2",
                    "status": "running",
                    "heartbeat_age_seconds": 12,
                    "retry_count": 0,
                }
            ],
            diagnostics=[],
        ),
    )
    assert [phase.key for phase in state.phases] == ["verify", "review", "build"]
    assert {phase.key: phase.state for phase in state.phases} == {
        "build": "done",
        "review": "review",
        "verify": "pending",
    }
    assert state.next_phase == "review"


def test_unknown_and_evidence_failure_are_not_green() -> None:
    manifest = parse_manifest(manifest_data())
    collection = Collection(tasks=tasks(), runs=[], diagnostics=[])
    unknown = derive_mission_state(manifest, {}, collection)
    assert unknown.outcome == "unknown"
    data = tasks()
    data[0]["evidence"] = []
    failed = derive_mission_state(
        manifest,
        {"build": "task-1", "review": "task-2", "verify": "task-3"},
        Collection(tasks=data, runs=[], diagnostics=[]),
    )
    assert failed.phases[0].state == "failed"


def test_projection_atomic_mode_schema_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "status.json"
    payload = {
        "schema_version": 1,
        "projection_version": 1,
        "supervisor": {
            "mode": "observe",
            "state": "ok",
            "tick_seq": 1,
            "heartbeat_at": 123.0,
            "post_gap": False,
        },
        "missions": [],
        "incidents": [],
        "cost": {"classification": "unknown"},
    }
    replacements: list[tuple[str, str]] = []
    original_replace = os.replace

    def record_replace(
        src: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        dst: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        replacements.append((os.fspath(src), os.fspath(dst)))
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", record_replace)
    write_projection(target, payload)
    assert replacements and replacements[0][1] == str(target)
    assert os.stat(target).st_mode & 0o777 == 0o600
    assert read_projection(target) == payload
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".status.json.")]
    poisoned = dict(payload, raw_output="secret")
    with pytest.raises(ProjectionError):
        write_projection(target, poisoned)


def test_projection_rejects_symlink_and_oversize(tmp_path: Path) -> None:
    target = tmp_path / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "status.json"
    link.symlink_to(target)
    with pytest.raises(ProjectionError, match="regular file"):
        read_projection(link)
    target.write_text(json.dumps({"x": "y" * 300_000}), encoding="utf-8")
    with pytest.raises(ProjectionError, match="too large"):
        read_projection(target)


def _projection_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "projection_version": 1,
        "supervisor": {
            "mode": "observe",
            "state": "ok",
            "tick_seq": 1,
            "heartbeat_at": 123.0,
            "post_gap": False,
        },
        "missions": [],
        "incidents": [],
        "cost": {"classification": "unknown"},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("projection_version", 2),
        lambda value: value.__setitem__("supervisor", {}),
        lambda value: value["supervisor"].__setitem__("mode", "repair"),
        lambda value: value["supervisor"].__setitem__("post_gap", 0),
        lambda value: value["supervisor"].__setitem__("tick_seq", True),
        lambda value: value.__setitem__("missions", {}),
        lambda value: value.__setitem__("incidents", [None] * 257),
        lambda value: value.__setitem__("cost", {}),
        lambda value: value.__setitem__("cost", {"classification": "free"}),
    ],
)
def test_projection_schema_variants_fail_closed(mutate: object) -> None:
    payload = _projection_payload()
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(ProjectionError):
        projection.validate_projection(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["missions"].append({"id": "/private/path"}),
        lambda value: value["missions"][0]["phases"][0].update(raw_log="secret prose"),
        lambda value: value["missions"][0]["phases"][0].__setitem__("task_id", "/tm" + "p/leak"),
        lambda value: value["missions"][0]["phases"][0].__setitem__("retry_count", True),
        lambda value: value["missions"][0]["workers"][0].__setitem__("assignee", "bad name"),
        lambda value: value["incidents"][0].__setitem__("age_ticks", 0),
        lambda value: value["missions"][0].__setitem__("outcome", "healthy"),
        lambda value: value["missions"][0].__setitem__("manifest_sha256", "z" * 64),
        lambda value: value["missions"][0].__setitem__("manifest_sha256", "a" * 63),
        lambda value: value["missions"][0].__setitem__("next_phase", "bad phase"),
        lambda value: value["missions"][0].__setitem__("phases", "invalid"),
        lambda value: value["missions"][0].__setitem__("workers", "invalid"),
        lambda value: value["missions"][0]["phases"][0].__setitem__("state", "healthy"),
        lambda value: value["missions"][0]["phases"][0].__setitem__(
            "evidence_present", ["tests", "tests"]
        ),
        lambda value: value["missions"][0]["phases"][0].__setitem__(
            "evidence_present", ["bad evidence"]
        ),
        lambda value: value["missions"][0]["workers"][0].update(extra=True),
        lambda value: value["missions"][0]["workers"][0].__setitem__("heartbeat_age_seconds", -1),
        lambda value: value["missions"][0]["workers"][0].__setitem__("retry_count", True),
        lambda value: value["incidents"][0].update(extra=True),
        lambda value: value["incidents"][0].__setitem__("kind", "bad kind"),
        lambda value: value["incidents"][0].__setitem__("severity", "info"),
        lambda value: value["incidents"][0].__setitem__("disposition", "resolved"),
        lambda value: value["incidents"][0].__setitem__("observed_ticks", True),
    ],
)
def test_projection_deeply_rejects_nested_prose_paths_extra_fields_and_bad_types(
    mutate: object,
) -> None:
    payload = _projection_payload()
    payload["missions"] = [
        {
            "id": "synthetic-release",
            "manifest_sha256": "a" * 64,
            "outcome": "running",
            "next_phase": "build",
            "phases": [
                {
                    "key": "build",
                    "state": "running",
                    "task_id": "task-1",
                    "assignee": "builder",
                    "evidence_present": ["tests"],
                    "retry_count": 0,
                }
            ],
            "workers": [
                {
                    "task_id": "task-1",
                    "run_id": "1",
                    "assignee": "builder",
                    "status": "running",
                    "heartbeat_age_seconds": None,
                    "retry_count": 0,
                }
            ],
        }
    ]
    payload["incidents"] = [
        {
            "id": "incident-1",
            "phase_key": "build",
            "kind": "phase_failed",
            "severity": "critical",
            "age_ticks": 1,
            "observed_ticks": 1,
            "disposition": "observing",
        }
    ]
    mutate(payload)  # type: ignore[operator]
    with pytest.raises(ProjectionError):
        projection.validate_projection(payload)


def test_projection_write_crash_removes_temporary_and_preserves_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "status.json"
    target.write_text("previous\n", encoding="utf-8")
    os.chmod(target, 0o600)

    def crash_replace(_source: object, _target: object) -> None:
        raise OSError("synthetic crash")

    monkeypatch.setattr(os, "replace", crash_replace)
    with pytest.raises(ProjectionError, match="could not be written"):
        write_projection(target, _projection_payload())
    assert target.read_text(encoding="utf-8") == "previous\n"
    assert not [path for path in tmp_path.iterdir() if path.name.startswith(".status.json.")]


def test_projection_rejects_oversized_write_permissions_and_invalid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(projection, "MAX_PROJECTION_BYTES", 32)
    with pytest.raises(ProjectionError, match="too large"):
        write_projection(tmp_path / "large.json", _projection_payload())
    monkeypatch.setattr(projection, "MAX_PROJECTION_BYTES", 256 * 1024)
    target = tmp_path / "status.json"
    target.write_text("not json", encoding="utf-8")
    os.chmod(target, 0o600)
    with pytest.raises(ProjectionError, match="unavailable"):
        read_projection(target)
    target.write_text(json.dumps(_projection_payload()), encoding="utf-8")
    os.chmod(target, 0o644)
    with pytest.raises(ProjectionError, match="permissions"):
        read_projection(target)


def test_projection_rejects_wrong_owner_on_read_and_existing_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "status.json"
    write_projection(target, _projection_payload())
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ProjectionError, match="ownership"):
        read_projection(target)
    with pytest.raises(ProjectionError, match="ownership"):
        write_projection(target, _projection_payload())


def test_ledger_manifest_identity_incident_resolution_and_unsafe_metadata(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    with Ledger.create(path) as ledger:
        ledger.register_mission("mission", "a" * 64)
        with pytest.raises(LedgerError, match="hash"):
            ledger.register_mission("mission", "b" * 64)
        candidate = {
            "incident_id": "incident-1",
            "phase_key": "build",
            "kind": "phase_failed",
            "severity": "critical",
        }
        observed = ledger.reconcile_incidents("mission", 1, [candidate], 2)
        assert observed[0]["disposition"] == "observing"
        assert ledger.reconcile_incidents("mission", 2, [], 2) == []
    os.chmod(path, 0o644)
    with pytest.raises(LedgerError, match="permissions"):
        Ledger.open(path)


def test_ledger_rejects_wrong_owner_schema_and_unsafe_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.db"
    with Ledger.create(path):
        pass
    monkeypatch.setattr(os, "getuid", lambda: path.stat().st_uid + 1)
    with pytest.raises(LedgerError, match="ownership"):
        Ledger.open(path)
    monkeypatch.undo()

    connection = sqlite3.connect(path)
    connection.execute("UPDATE meta SET schema_version=3 WHERE singleton=1")
    connection.commit()
    connection.close()
    with pytest.raises(LedgerError, match="schema"):
        Ledger.open(path)

    real_directory = tmp_path / "real"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)
    with pytest.raises(LedgerError, match="directory is unsafe"):
        Ledger.create(linked_directory / "new.db")


def test_bootstrap_apply_lock_is_exclusive_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    first = Ledger.create(path)
    second = Ledger.open(path)
    try:
        with (
            first.bootstrap_lock(),
            pytest.raises(LedgerError, match="already running"),
            second.bootstrap_lock(),
        ):
            pass
        with second.bootstrap_lock():
            assert (tmp_path / ".ledger.db.bootstrap.lock").stat().st_mode & 0o777 == 0o600
    finally:
        second.close()
        first.close()


@pytest.mark.parametrize(
    ("status", "expected_state", "expected_outcome"),
    [
        ("failed", "failed", "failed"),
        ("cancelled", "failed", "failed"),
        ("blocked", "blocked", "blocked"),
        ("needs_input", "blocked", "blocked"),
        ("ready", "ready", "running"),
        ("scheduled", "ready", "running"),
        ("pending", "pending", "running"),
        ("todo", "pending", "running"),
        ("mystery", "unknown", "unknown"),
    ],
)
def test_state_derivation_maps_task_failures_and_scheduler_states(
    status: str, expected_state: str, expected_outcome: str
) -> None:
    manifest = parse_manifest(manifest_data())
    rows = tasks()
    rows[0]["status"] = status
    state = derive_mission_state(
        manifest,
        {"build": "task-1", "review": "task-2", "verify": "task-3"},
        Collection(tasks=rows, runs=[], diagnostics=[]),
    )
    assert state.phases[0].state == expected_state
    assert state.outcome == expected_outcome


def test_state_derivation_requires_unique_bindings_and_completes_only_with_all_evidence() -> None:
    manifest = parse_manifest(manifest_data())
    rows = tasks()
    duplicate = derive_mission_state(
        manifest,
        {"build": "task-1", "review": "task-2", "verify": "task-3"},
        Collection(tasks=[*rows, dict(rows[0])], runs=[], diagnostics=[]),
    )
    assert duplicate.phases[0].state == "unknown"
    assert duplicate.outcome == "unknown"

    for row, evidence in zip(
        rows,
        (["commit", "tests"], ["review_outcome"], ["checksums"]),
        strict=True,
    ):
        row["status"] = "done"
        row["evidence"] = evidence
    complete = derive_mission_state(
        manifest,
        {"build": "task-1", "review": "task-2", "verify": "task-3"},
        Collection(tasks=rows, runs=[], diagnostics=[]),
    )
    assert complete.outcome == "done"
    assert complete.next_phase is None


def test_collection_accepts_adapter_redacted_diagnostics_and_rejects_wrong_shape() -> None:
    mapping = {
        "tasks": [],
        "runs": [],
        "diagnostics": [
            {
                "task_id": "task-1",
                "title_length": 14,
                "status": "running",
                "assignee": "builder",
                "diagnostic_count": 1,
            }
        ],
    }
    assert Collection.from_mapping(mapping).diagnostics == mapping["diagnostics"]
    with pytest.raises(ValidationError, match="collection has an invalid shape"):
        Collection.from_mapping({"tasks": [], "runs": []})
