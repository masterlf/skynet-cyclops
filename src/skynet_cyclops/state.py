"""Pure derivation of mission state from a manifest and bounded collection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .errors import ValidationError
from .manifest import Manifest


@dataclass(frozen=True, slots=True)
class Collection:
    tasks: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    diagnostics: list[dict[str, str | int]]

    @classmethod
    def from_mapping(cls, value: object) -> Collection:
        if not isinstance(value, dict) or set(value) != {"tasks", "runs", "diagnostics"}:
            raise ValidationError("collection has an invalid shape")
        from .adapter import validate_diagnostic_collection, validate_json_collection

        tasks = validate_json_collection(value["tasks"], kind="tasks")
        runs = validate_json_collection(value["runs"], kind="runs")
        diagnostics = validate_diagnostic_collection(value["diagnostics"])
        return cls(tasks=tasks, runs=runs, diagnostics=diagnostics)


@dataclass(frozen=True, slots=True)
class PhaseState:
    key: str
    state: str
    task_id: str | None
    assignee: str
    evidence_present: tuple[str, ...]
    retry_count: int


@dataclass(frozen=True, slots=True)
class WorkerState:
    task_id: str
    run_id: str
    assignee: str
    status: str
    heartbeat_age_seconds: int | None
    retry_count: int


@dataclass(frozen=True, slots=True)
class MissionState:
    id: str
    outcome: str
    next_phase: str | None
    phases: tuple[PhaseState, ...]
    workers: tuple[WorkerState, ...]

    def to_dict(self, manifest_hash: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "manifest_sha256": manifest_hash,
            "outcome": self.outcome,
            "next_phase": self.next_phase,
            "phases": [asdict(item) for item in self.phases],
            "workers": [asdict(item) for item in self.workers],
        }


def derive_mission_state(
    manifest: Manifest, bindings: Mapping[str, str], collection: Collection
) -> MissionState:
    tasks: dict[str, dict[str, Any]] = {}
    duplicate_tasks: set[str] = set()
    for task_row in collection.tasks:
        identifier = task_row.get("id")
        if isinstance(identifier, str):
            if identifier in tasks:
                duplicate_tasks.add(identifier)
            tasks[identifier] = task_row
    runs = {run.get("id"): run for run in collection.runs if isinstance(run.get("id"), str)}
    phase_states: list[PhaseState] = []
    states: dict[str, str] = {}
    workers: list[WorkerState] = []
    for phase in manifest.phases:
        task_id = bindings.get(phase.key)
        task = tasks.get(task_id) if task_id is not None else None
        evidence: tuple[str, ...] = ()
        retry_count = 0
        # Keep a missing binding explicit and fail-closed; do not rely on an optimizable assert.
        if task_id is None or task is None or task_id in duplicate_tasks:
            state = "unknown"
        else:
            evidence = tuple(
                sorted(item for item in task.get("evidence", []) if isinstance(item, str))
            )
            retry_count = task.get("retry_count", 0)
            retry_count = (
                retry_count
                if isinstance(retry_count, int) and not isinstance(retry_count, bool)
                else 0
            )
            status = task.get("status")
            dependencies_done = all(states.get(key) == "done" for key in phase.depends_on)
            if status == "done":
                state = "done" if set(phase.evidence_required).issubset(evidence) else "failed"
            elif status in {"failed", "cancelled"}:
                state = "failed"
            elif status in {"blocked", "needs_input"}:
                state = "blocked"
            elif not dependencies_done:
                state = "pending"
            elif status in {"running", "claimed"}:
                state = "review" if phase.kind == "review" else "running"
            elif status in {"ready", "scheduled"}:
                state = "ready"
            elif status in {"pending", "todo"}:
                state = "pending"
            else:
                state = "unknown"
            run_id = task.get("active_run_id")
            run = runs.get(run_id)
            if isinstance(run_id, str) and run is not None:
                age = run.get("heartbeat_age_seconds")
                heartbeat_age = (
                    age if isinstance(age, int) and not isinstance(age, bool) and age >= 0 else None
                )
                workers.append(
                    WorkerState(
                        task_id=task_id,
                        run_id=run_id,
                        assignee=phase.assignee,
                        status=str(run.get("status", "unknown")),
                        heartbeat_age_seconds=heartbeat_age,
                        retry_count=retry_count,
                    )
                )
        states[phase.key] = state
        phase_states.append(
            PhaseState(
                key=phase.key,
                state=state,
                task_id=task_id,
                assignee=phase.assignee,
                evidence_present=evidence,
                retry_count=retry_count,
            )
        )
    values = set(states.values())
    if "unknown" in values:
        outcome = "unknown"
    elif "failed" in values:
        outcome = "failed"
    elif "blocked" in values:
        outcome = "blocked"
    elif states.get(manifest.mission.final_phase) == "done" and values == {"done"}:
        outcome = "done"
    else:
        outcome = "running"
    next_phase = next((phase.key for phase in phase_states if phase.state != "done"), None)
    return MissionState(
        id=manifest.mission.id,
        outcome=outcome,
        next_phase=next_phase,
        phases=tuple(phase_states),
        workers=tuple(workers),
    )
