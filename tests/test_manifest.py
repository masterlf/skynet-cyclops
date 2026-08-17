from __future__ import annotations

import copy
import os
from pathlib import Path

import pytest
from conftest import manifest_data, write_manifest

from skynet_cyclops.errors import ValidationError
from skynet_cyclops.manifest import canonical_manifest_hash, load_manifest, parse_manifest


def test_valid_manifest_and_deterministic_hash(tmp_path: Path) -> None:
    first = parse_manifest(manifest_data())
    reordered = copy.deepcopy(manifest_data())
    reordered["mission"] = dict(reversed(list(reordered["mission"].items())))
    reordered["phases"] = list(reversed(reordered["phases"]))
    assert first.mission.final_phase == "verify"
    assert canonical_manifest_hash(first) == canonical_manifest_hash(parse_manifest(reordered))
    assert len(canonical_manifest_hash(first)) == 64
    assert load_manifest(write_manifest(tmp_path / "mission.yaml")) == first


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(extra=True), "unexpected"),
        (lambda d: d.__setitem__("schema_version", 2), "schema_version"),
        (lambda d: d["mission"].__setitem__("id", "../unsafe"), "safe identifier"),
        (lambda d: d["mission"].__setitem__("tick_seconds", True), "integer"),
        (lambda d: d["mission"].__setitem__("tick_seconds", 4), "tick_seconds"),
        (lambda d: d["phases"].append(copy.deepcopy(d["phases"][0])), "duplicate"),
        (lambda d: d["phases"][0].__setitem__("depends_on", ["missing"]), "unknown dependency"),
        (lambda d: d["phases"][0].__setitem__("depends_on", ["verify"]), "cycle"),
        (lambda d: d["mission"].__setitem__("final_phase", "missing"), "final_phase"),
        (lambda d: d["phases"][1].__setitem__("assignee", "builder"), "distinct reviewer"),
        (lambda d: d["phases"][0].__setitem__("max_retries", 99), "max_retries"),
        (lambda d: d["phases"][0].__setitem__("goal_mode", "false"), "boolean"),
        (lambda d: d["phases"][0].__setitem__("title", "--assignee"), "title"),
        (lambda d: d["phases"][0].__setitem__("evidence_required", ["x", "x"]), "duplicate"),
        (lambda d: d["phases"].__setitem__(slice(None), d["phases"] * 40), "phases"),
    ],
)
def test_hostile_graph_and_schema_inputs(mutate: object, message: str) -> None:
    data = manifest_data()
    mutate(data)  # type: ignore[operator]
    with pytest.raises(ValidationError, match=message):
        parse_manifest(data)


def test_final_phase_must_be_sink_and_reachable() -> None:
    data = manifest_data()
    data["phases"].append(
        {
            "key": "orphan",
            "kind": "verification",
            "title": "Orphan",
            "assignee": "observer",
            "depends_on": [],
            "goal_mode": False,
            "max_runtime_seconds": 60,
            "max_retries": 0,
            "evidence_required": [],
        }
    )
    with pytest.raises(ValidationError, match="reachable"):
        parse_manifest(data)


def test_loader_rejects_oversize_and_yaml_alias_bomb(tmp_path: Path) -> None:
    huge = tmp_path / "huge.yaml"
    huge.write_bytes(b"x" * 300_000)
    with pytest.raises(ValidationError, match="too large"):
        load_manifest(huge)
    alias = tmp_path / "alias.yaml"
    alias.write_text("a: &a [1]\nb: *a\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_manifest(alias)


def test_loader_rejects_unsafe_owner_and_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_manifest(tmp_path / "mission.yaml")
    os.chmod(path, 0o620)
    with pytest.raises(ValidationError, match="permissions"):
        load_manifest(path)
    os.chmod(path, 0o600)
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(ValidationError, match="ownership"):
        load_manifest(path)
