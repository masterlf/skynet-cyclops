"""Strict mission manifest models, parser, graph checks, and canonical hashing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NoReturn

import yaml
from yaml.events import AliasEvent

from .errors import ValidationError

MAX_MANIFEST_BYTES = 256 * 1024
MAX_PHASES = 64
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ALLOWED_KINDS = frozenset({"implementation", "review", "verification", "gate"})


@dataclass(frozen=True, slots=True)
class Mission:
    id: str
    display_name: str
    board: str
    tick_seconds: int
    gap_damper_seconds: int
    final_phase: str


@dataclass(frozen=True, slots=True)
class Phase:
    key: str
    kind: str
    title: str
    assignee: str
    depends_on: tuple[str, ...]
    goal_mode: bool
    max_runtime_seconds: int
    max_retries: int
    evidence_required: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    schema_version: int
    mission: Mission
    phases: tuple[Phase, ...]

    def phase(self, key: str) -> Phase:
        return next(item for item in self.phases if item.key == key)


class _NoAliasSafeLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise ValidationError("YAML aliases are not allowed")
        return super().compose_node(parent, index)


def _fail(message: str) -> NoReturn:
    raise ValidationError(message)


def _mapping(value: object, name: str, allowed: set[str], required: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(f"{name} must be a mapping")
    keys = set(value)
    extra = keys - allowed
    missing = required - keys
    if extra:
        _fail(f"{name} has unexpected fields")
    if missing:
        _fail(f"{name} is missing required fields")
    return value


def _string(value: object, name: str, maximum: int, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(f"{name} must be a bounded non-empty string")
    if any(ord(character) < 32 for character in value):
        _fail(f"{name} contains control characters")
    if identifier and not _IDENTIFIER.fullmatch(value):
        _fail(f"{name} must be a safe identifier")
    return value


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        _fail(f"{name} is outside its allowed range")
    return value


def _string_list(value: object, name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        _fail(f"{name} must be a bounded list")
    result = tuple(_string(item, name, 64, identifier=True) for item in value)
    if len(set(result)) != len(result):
        _fail(f"{name} contains a duplicate")
    return result


def parse_manifest(data: object) -> Manifest:
    root = _mapping(
        data,
        "manifest",
        {"schema_version", "mission", "phases"},
        {"schema_version", "mission", "phases"},
    )
    schema_version = _integer(root["schema_version"], "schema_version", 1, 1)
    raw_mission = _mapping(
        root["mission"],
        "mission",
        {"id", "display_name", "board", "tick_seconds", "gap_damper_seconds", "final_phase"},
        {"id", "display_name", "board", "tick_seconds", "gap_damper_seconds", "final_phase"},
    )
    mission = Mission(
        id=_string(raw_mission["id"], "mission.id", 64, identifier=True),
        display_name=_string(raw_mission["display_name"], "mission.display_name", 128),
        board=_string(raw_mission["board"], "mission.board", 64, identifier=True),
        tick_seconds=_integer(raw_mission["tick_seconds"], "mission.tick_seconds", 10, 3600),
        gap_damper_seconds=_integer(
            raw_mission["gap_damper_seconds"], "mission.gap_damper_seconds", 30, 86400
        ),
        final_phase=_string(raw_mission["final_phase"], "mission.final_phase", 64, identifier=True),
    )
    raw_phases = root["phases"]
    if not isinstance(raw_phases, list) or not 1 <= len(raw_phases) <= MAX_PHASES:
        _fail(f"phases must contain between 1 and {MAX_PHASES} items")
    phases: list[Phase] = []
    phase_fields = {
        "key",
        "kind",
        "title",
        "assignee",
        "depends_on",
        "goal_mode",
        "max_runtime_seconds",
        "max_retries",
        "evidence_required",
    }
    for index, value in enumerate(raw_phases):
        raw = _mapping(value, f"phases[{index}]", phase_fields, phase_fields)
        kind = _string(raw["kind"], f"phases[{index}].kind", 32, identifier=True)
        if kind not in _ALLOWED_KINDS:
            _fail(f"phases[{index}].kind is unsupported")
        goal_mode = raw["goal_mode"]
        if not isinstance(goal_mode, bool):
            _fail(f"phases[{index}].goal_mode must be a boolean")
        phases.append(
            Phase(
                key=_string(raw["key"], f"phases[{index}].key", 64, identifier=True),
                kind=kind,
                title=_phase_title(raw["title"], f"phases[{index}].title"),
                assignee=_string(raw["assignee"], f"phases[{index}].assignee", 64, identifier=True),
                depends_on=_string_list(
                    raw["depends_on"], f"phases[{index}].depends_on", MAX_PHASES
                ),
                goal_mode=goal_mode,
                max_runtime_seconds=_integer(
                    raw["max_runtime_seconds"], f"phases[{index}].max_runtime_seconds", 30, 86400
                ),
                max_retries=_integer(raw["max_retries"], f"phases[{index}].max_retries", 0, 10),
                evidence_required=_string_list(
                    raw["evidence_required"], f"phases[{index}].evidence_required", 32
                ),
            )
        )
    manifest = Manifest(schema_version=schema_version, mission=mission, phases=tuple(phases))
    _validate_graph(manifest)
    return manifest


def _validate_graph(manifest: Manifest) -> None:
    by_key = {phase.key: phase for phase in manifest.phases}
    if len(by_key) != len(manifest.phases):
        _fail("phase keys contain a duplicate")
    if manifest.mission.final_phase not in by_key:
        _fail("mission.final_phase is unknown")
    for phase in manifest.phases:
        for dependency in phase.depends_on:
            if dependency not in by_key:
                _fail(f"phase {phase.key} has an unknown dependency")
        if phase.kind == "review":
            if not phase.depends_on:
                _fail(f"review phase {phase.key} must have a dependency")
            if any(by_key[key].assignee == phase.assignee for key in phase.depends_on):
                _fail(f"review phase {phase.key} requires a distinct reviewer")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            _fail("phase graph contains a cycle")
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key].depends_on:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for phase in manifest.phases:
        visit(phase.key)
    final = manifest.mission.final_phase
    if any(final in phase.depends_on for phase in manifest.phases):
        _fail("mission.final_phase must be a sink")
    ancestors: set[str] = set()

    def collect(key: str) -> None:
        if key in ancestors:
            return
        ancestors.add(key)
        for dependency in by_key[key].depends_on:
            collect(dependency)

    collect(final)
    if ancestors != set(by_key):
        _fail("every phase must be reachable through mission.final_phase")


def _phase_title(value: object, name: str) -> str:
    title = _string(value, name, 200)
    if title.startswith("-"):
        _fail(f"{name} must not begin with a dash")
    return title


def topological_phases(manifest: Manifest) -> tuple[Phase, ...]:
    """Return a stable dependency-first phase order."""
    by_key = {phase.key: phase for phase in manifest.phases}
    pending = {key: set(phase.depends_on) for key, phase in by_key.items()}
    ordered: list[Phase] = []
    completed: set[str] = set()
    while pending:
        ready = sorted(key for key, dependencies in pending.items() if dependencies <= completed)
        if not ready:
            _fail("phase graph cannot be ordered")
        for key in ready:
            ordered.append(by_key[key])
            completed.add(key)
            del pending[key]
    return tuple(ordered)


def load_manifest(path: str | os.PathLike[str]) -> Manifest:
    candidate = Path(path)
    try:
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or candidate.is_symlink():
            _fail("manifest must be a regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            _fail("manifest ownership is unsafe")
        if stat.S_IMODE(info.st_mode) & 0o022:
            _fail("manifest permissions are unsafe")
        if info.st_size > MAX_MANIFEST_BYTES:
            _fail("manifest is too large")
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValidationError("manifest is unavailable") from exc
    try:
        data = yaml.load(  # Custom SafeLoader rejects aliases.  # nosec B506
            text,
            Loader=_NoAliasSafeLoader,  # noqa: S506
        )
    except ValidationError:
        raise
    except yaml.YAMLError as exc:
        raise ValidationError("manifest YAML is invalid") from exc
    return parse_manifest(data)


def canonical_manifest_hash(manifest: Manifest) -> str:
    normalized = asdict(manifest)
    normalized["phases"] = sorted(normalized["phases"], key=lambda phase: phase["key"])
    for phase in normalized["phases"]:
        phase["depends_on"] = sorted(phase["depends_on"])
        phase["evidence_required"] = sorted(phase["evidence_required"])
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
