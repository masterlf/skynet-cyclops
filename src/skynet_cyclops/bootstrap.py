"""Explicit, dry-run-first, race-safe, idempotent manifest bootstrap."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .errors import AdapterError, ValidationError
from .ledger import Ledger
from .manifest import Manifest, Phase, canonical_manifest_hash


class BootstrapAdapter(Protocol):
    def preflight_profile(self, name: str) -> None: ...
    def list_tasks(self, board: str) -> list[dict[str, Any]]: ...
    def create_task(
        self, board: str, title: str, assignee: str, parents: list[str], idempotency_key: str
    ) -> dict[str, Any]: ...
    def link_tasks(self, board: str, parent_id: str, child_id: str) -> None: ...
    def task_parents(self, board: str, task_id: str) -> list[str]: ...
    def task_status(self, board: str, task_id: str) -> str: ...
    def promote_task(self, board: str, task_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class BootstrapItem:
    phase_key: str
    title: str
    assignee: str
    dependency_phases: tuple[str, ...]
    idempotency_key: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["dependency_phases"] = list(self.dependency_phases)
        return value


def _topological_phases(manifest: Manifest) -> list[Phase]:
    by_key = {phase.key: phase for phase in manifest.phases}
    pending = {key: set(phase.depends_on) for key, phase in by_key.items()}
    ordered: list[Phase] = []
    completed: set[str] = set()
    while pending:
        ready = sorted(key for key, dependencies in pending.items() if dependencies <= completed)
        if not ready:
            raise ValidationError("phase graph cannot be ordered")
        for key in ready:
            ordered.append(by_key[key])
            completed.add(key)
            del pending[key]
    return ordered


def plan_bootstrap(manifest: Manifest) -> tuple[BootstrapItem, ...]:
    digest = canonical_manifest_hash(manifest)
    return tuple(
        BootstrapItem(
            phase_key=phase.key,
            title=phase.title,
            assignee=phase.assignee,
            dependency_phases=tuple(sorted(phase.depends_on)),
            idempotency_key="cyclops-"
            + hashlib.sha256(f"{digest}\0{phase.key}".encode()).hexdigest()[:32],
        )
        for phase in _topological_phases(manifest)
    )


def _reconcile_created_task(
    adapter: BootstrapAdapter, board: str, idempotency_key: str
) -> str | None:
    matches = [
        task
        for task in adapter.list_tasks(board)
        if task.get("bootstrap_key") == idempotency_key and isinstance(task.get("id"), str)
    ]
    if len(matches) > 1:
        raise AdapterError("Hermes idempotency reconciliation is ambiguous")
    return None if not matches else str(matches[0]["id"])


def apply_bootstrap(
    manifest: Manifest, adapter: BootstrapAdapter, ledger: Ledger
) -> dict[str, str]:
    """Create a blocked graph, verify all edges, then expose roots to the dispatcher."""
    plan = plan_bootstrap(manifest)
    for profile in sorted({item.assignee for item in plan}):
        adapter.preflight_profile(profile)
    manifest_hash = canonical_manifest_hash(manifest)
    ledger.register_mission(manifest.mission.id, manifest_hash)
    bindings = ledger.bindings(manifest.mission.id)

    existing = adapter.list_tasks(manifest.mission.board)
    by_key = {
        task["bootstrap_key"]: task
        for task in existing
        if isinstance(task.get("bootstrap_key"), str) and isinstance(task.get("id"), str)
    }

    # Stage 1: every card is created blocked and without edges. No ready window exists.
    for item in plan:
        if item.phase_key in bindings:
            continue
        task = by_key.get(item.idempotency_key)
        if task is not None:
            task_id = str(task["id"])
        else:
            ledger.prepare_intent(
                item.idempotency_key, manifest.mission.id, item.phase_key, time.time()
            )
            try:
                created = adapter.create_task(
                    manifest.mission.board,
                    item.title,
                    item.assignee,
                    [],
                    item.idempotency_key,
                )
                task_id = str(created["id"])
            except AdapterError:
                reconciled = _reconcile_created_task(
                    adapter, manifest.mission.board, item.idempotency_key
                )
                if reconciled is None:
                    raise
                task_id = reconciled
        ledger.bind(manifest.mission.id, item.phase_key, task_id, item.idempotency_key)
        ledger.complete_intent(item.idempotency_key)
        bindings[item.phase_key] = task_id

    if set(bindings) != {item.phase_key for item in plan}:
        raise AdapterError("Hermes bootstrap bindings are incomplete")

    # Stage 2: add every dependency. A lost response is reconciled by reading parents.
    for item in plan:
        child_id = bindings[item.phase_key]
        current = set(adapter.task_parents(manifest.mission.board, child_id))
        expected = {bindings[key] for key in item.dependency_phases}
        if not current <= expected:
            raise AdapterError("Hermes bootstrap graph has unexpected dependencies")
        for parent_id in sorted(expected - current):
            try:
                adapter.link_tasks(manifest.mission.board, parent_id, child_id)
            except AdapterError:
                if parent_id not in adapter.task_parents(manifest.mission.board, child_id):
                    raise

    # Stage 3: exact read-back. No phase is promoted before this full-graph gate passes.
    for item in plan:
        actual = set(adapter.task_parents(manifest.mission.board, bindings[item.phase_key]))
        expected = {bindings[key] for key in item.dependency_phases}
        if actual != expected:
            raise AdapterError("Hermes bootstrap graph verification failed")

    # Stage 4: only roots become visible. This authority is not reachable from tick.
    for item in plan:
        if item.dependency_phases:
            continue
        task_id = bindings[item.phase_key]
        if adapter.task_status(manifest.mission.board, task_id) in {"ready", "running", "done"}:
            continue
        try:
            adapter.promote_task(manifest.mission.board, task_id)
        except AdapterError:
            if adapter.task_status(manifest.mission.board, task_id) not in {
                "ready",
                "running",
                "done",
            }:
                raise
    return bindings
