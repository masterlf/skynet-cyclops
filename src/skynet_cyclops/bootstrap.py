"""Explicit, dry-run-first, race-safe, idempotent manifest bootstrap."""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .errors import AdapterError
from .ledger import Ledger
from .manifest import Manifest, canonical_manifest_hash, topological_phases


class BootstrapAdapter(Protocol):
    def preflight_profile(self, name: str) -> None: ...
    def list_tasks(self, board: str) -> list[dict[str, Any]]: ...
    def create_task(
        self,
        board: str,
        title: str,
        assignee: str,
        parents: list[str],
        idempotency_key: str,
        *,
        phase_key: str,
        kind: str,
        goal_mode: bool,
        max_runtime_seconds: int,
        max_retries: int,
        evidence_required: list[str],
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
    kind: str
    goal_mode: bool
    max_runtime_seconds: int
    max_retries: int
    evidence_required: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["dependency_phases"] = list(self.dependency_phases)
        value["evidence_required"] = list(self.evidence_required)
        return value

    def contract(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "phase_key": self.phase_key,
            "kind": self.kind,
            "goal_mode": self.goal_mode,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_retries": self.max_retries,
            "evidence_required": list(self.evidence_required),
        }


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
            kind=phase.kind,
            goal_mode=phase.goal_mode,
            max_runtime_seconds=phase.max_runtime_seconds,
            max_retries=phase.max_retries,
            evidence_required=tuple(sorted(phase.evidence_required)),
        )
        for phase in topological_phases(manifest)
    )


def _task_matches_item(task: dict[str, Any], item: BootstrapItem) -> bool:
    return (
        task.get("bootstrap_key") == item.idempotency_key
        and task.get("title") == item.title
        and task.get("assignee") == item.assignee
        and task.get("status") == "blocked"
        and task.get("bootstrap_contract") == item.contract()
    )


def _select_candidate(
    tasks: list[dict[str, Any]], item: BootstrapItem, *, reconciliation: bool
) -> str | None:
    matches = [
        task
        for task in tasks
        if task.get("bootstrap_key") == item.idempotency_key and isinstance(task.get("id"), str)
    ]
    if len(matches) > 1:
        context = "reconciliation" if reconciliation else "selection"
        raise AdapterError(f"Hermes bootstrap card {context} is ambiguous")
    if not matches:
        return None
    if not _task_matches_item(matches[0], item):
        raise AdapterError("Hermes bootstrap card does not match the planned phase")
    return str(matches[0]["id"])


def _reconcile_created_task(
    adapter: BootstrapAdapter, board: str, item: BootstrapItem
) -> str | None:
    return _select_candidate(adapter.list_tasks(board), item, reconciliation=True)


def apply_bootstrap(
    manifest: Manifest, adapter: BootstrapAdapter, ledger: Ledger
) -> dict[str, str]:
    """Create a blocked graph, verify all edges, then expose roots to the dispatcher."""
    with ledger.bootstrap_lock():
        return _apply_bootstrap_locked(manifest, adapter, ledger)


def _apply_bootstrap_locked(
    manifest: Manifest, adapter: BootstrapAdapter, ledger: Ledger
) -> dict[str, str]:
    plan = plan_bootstrap(manifest)
    for profile in sorted({item.assignee for item in plan}):
        adapter.preflight_profile(profile)
    manifest_hash = canonical_manifest_hash(manifest)
    ledger.register_mission(manifest.mission.id, manifest_hash)
    bindings = ledger.bindings(manifest.mission.id)

    existing = adapter.list_tasks(manifest.mission.board)

    # Stage 1: every card is created blocked and without edges. No ready window exists.
    for item in plan:
        if item.phase_key in bindings:
            continue
        task_id = _select_candidate(existing, item, reconciliation=False)
        if task_id is not None:
            pass
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
                    phase_key=item.phase_key,
                    kind=item.kind,
                    goal_mode=item.goal_mode,
                    max_runtime_seconds=item.max_runtime_seconds,
                    max_retries=item.max_retries,
                    evidence_required=list(item.evidence_required),
                )
            except AdapterError:
                reconciled = _reconcile_created_task(adapter, manifest.mission.board, item)
                if reconciled is None:
                    raise
                task_id = reconciled
            else:
                task_id = _reconcile_created_task(adapter, manifest.mission.board, item)
                if task_id is None or task_id != str(created["id"]):
                    raise AdapterError("Hermes create response does not match the planned phase")
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
