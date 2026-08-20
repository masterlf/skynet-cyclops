"""Deterministic observe-only supervisor tick."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, cast

from .activation import ActivationVerdict
from .errors import LedgerError
from .ledger import Ledger
from .manager import IncidentObservation
from .manifest import Manifest, canonical_manifest_hash
from .projection import write_projection
from .state import Collection, derive_mission_state


class Collector(Protocol):
    def collect(self, board: str, task_ids: list[str]) -> dict[str, object]: ...


def _base_projection(
    now: float,
    tick_seq: int,
    *,
    state: str,
    post_gap: bool,
    activation: ActivationVerdict,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "projection_version": 2,
        "supervisor": {
            "mode": "observe",
            "state": state,
            "heartbeat_at": now,
            "tick_seq": tick_seq,
            "post_gap": post_gap,
            "compatibility_state": activation.compatibility_state,
            "wake_enabled": activation.wake_enabled,
        },
        "missions": [],
        "incidents": [],
        "cost": {"classification": "unknown"},
    }


def _system_incident(identifier: str, mission_id: str, kind: str) -> dict[str, object]:
    return {
        "id": identifier,
        "generation": 1,
        "mission_id": mission_id,
        "phase_key": "supervisor",
        "kind": kind,
        "subject_task_id": None,
        "subject_run_id": None,
        "severity": "critical",
        "age_ticks": 1,
        "observed_ticks": 1,
        "disposition": "active",
        "lifecycle": "detected",
        "terminal_reason": None,
        "reason_code": None,
        "human_question_code": None,
        "attempt_count": 0,
        "next_attempt_at": 0.0,
        "manager_state": "idle",
        "notification_state": "none",
        "acknowledged_at": None,
        "terminal_at": None,
    }


def run_tick(
    manifest: Manifest,
    ledger_path: str | Path,
    status_path: str | Path,
    collector: Collector,
    *,
    now: float | None = None,
    debounce_ticks: int = 2,
    activation_check: Callable[[], ActivationVerdict] | None = None,
) -> dict[str, Any]:
    timestamp = time.time() if now is None else now
    digest = canonical_manifest_hash(manifest)
    try:
        activation = (
            ActivationVerdict("absent", "unchecked", False)
            if activation_check is None
            else activation_check()
        )
    except Exception:
        activation = ActivationVerdict("malformed", "unsupported", False)
    try:
        ledger = Ledger.open(ledger_path)
    except LedgerError:
        payload = _base_projection(
            timestamp, 0, state="critical", post_gap=False, activation=activation
        )
        payload["missions"] = [
            {
                "id": manifest.mission.id,
                "manifest_sha256": digest,
                "outcome": "unknown",
                "next_phase": None,
                "phases": [],
                "workers": [],
            }
        ]
        payload["incidents"] = [
            _system_incident("ledger-unavailable", manifest.mission.id, "ledger_unavailable")
        ]
        write_projection(status_path, payload)
        return payload
    with ledger:
        if ledger.mission_hash(manifest.mission.id) != digest:
            payload = _base_projection(
                timestamp, 0, state="critical", post_gap=False, activation=activation
            )
            payload["incidents"] = [
                _system_incident(
                    "manifest-binding-mismatch",
                    manifest.mission.id,
                    "manifest_binding_mismatch",
                )
            ]
            write_projection(status_path, payload)
            return payload
        committed_sequence, previous = ledger.tick_state()
        tick_seq = committed_sequence + 1
        post_gap = (
            previous is not None and timestamp - previous > manifest.mission.gap_damper_seconds
        )
        bindings = ledger.bindings(manifest.mission.id)
        raw_collection = collector.collect(manifest.mission.board, list(bindings.values()))
        collection = Collection.from_mapping(raw_collection)
        mission_state = derive_mission_state(manifest, bindings, collection)
        observations: list[IncidentObservation] = []
        for phase in mission_state.phases:
            if phase.state in {"unknown", "failed", "blocked"}:
                typed_facts = {
                    "expected_state": "done",
                    "kind": f"phase_{phase.state}",
                    "mission_id": manifest.mission.id,
                    "observed_state": phase.state,
                    "phase_key": phase.key,
                    "subject_task_id": phase.task_id,
                }
                observations.append(
                    IncidentObservation(
                        mission_id=manifest.mission.id,
                        phase_key=phase.key,
                        kind=f"phase_{phase.state}",
                        subject_task_id=phase.task_id,
                        subject_run_id=None,
                        severity="critical" if phase.state in {"unknown", "failed"} else "warning",
                        observation_sha256=hashlib.sha256(
                            json.dumps(typed_facts, sort_keys=True, separators=(",", ":")).encode()
                        ).hexdigest(),
                        expected_state="done",
                        observed_state=phase.state,
                    )
                )
        ledger.observe_manager_incidents(
            observations,
            mission_id=manifest.mission.id,
            tick_seq=tick_seq,
            now=timestamp,
            persistence_ticks=debounce_ticks,
            observe_only=post_gap,
            commit=False,
        )
        incidents = [
            _project_incident(item, tick_seq)
            for item in ledger.manager_incidents(mission_id=manifest.mission.id)
        ]
        active_critical = any(
            item["severity"] == "critical" and item["disposition"] == "active" for item in incidents
        )
        payload = _base_projection(
            timestamp,
            tick_seq,
            state="critical" if active_critical else ("degraded" if incidents else "ok"),
            post_gap=post_gap,
            activation=activation,
        )
        payload["missions"] = [mission_state.to_dict(digest)]
        payload["incidents"] = incidents
        ledger.commit_tick(tick_seq, timestamp)
        write_projection(status_path, payload)
        return payload


def _project_incident(item: dict[str, object], tick_seq: int) -> dict[str, object]:
    """Reduce private lifecycle state to the strict projection v2 contract."""
    first_tick = cast(int, item["first_tick"])
    lifecycle = str(item["lifecycle"])
    observed_ticks = cast(int, item["observed_ticks"])
    return {
        "id": item["incident_id"],
        "generation": item["generation"],
        "mission_id": item["mission_id"],
        "phase_key": item["phase_key"],
        "kind": item["kind"],
        "subject_task_id": item["subject_task_id"],
        "subject_run_id": item["subject_run_id"],
        "severity": item["severity"],
        "age_ticks": max(1, tick_seq - first_tick + 1),
        "observed_ticks": observed_ticks,
        "disposition": item["disposition"],
        "lifecycle": lifecycle,
        "terminal_reason": item["terminal_reason"],
        "reason_code": item["reason_code"],
        "human_question_code": item["human_question_code"],
        "attempt_count": item["attempt_count"],
        "next_attempt_at": item["next_attempt_at"],
        "manager_state": item["manager_state"],
        "notification_state": item["notification_state"],
        "acknowledged_at": item["acknowledged_at"],
        "terminal_at": item["terminal_at"],
    }
