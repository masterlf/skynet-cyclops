"""Deterministic observe-only supervisor tick."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Protocol

from .errors import LedgerError
from .ledger import Ledger
from .manifest import Manifest, canonical_manifest_hash
from .projection import write_projection
from .state import Collection, derive_mission_state


class Collector(Protocol):
    def collect(self, board: str, task_ids: list[str]) -> dict[str, object]: ...


def _base_projection(now: float, tick_seq: int, *, state: str, post_gap: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "projection_version": 1,
        "supervisor": {
            "mode": "observe",
            "state": state,
            "heartbeat_at": now,
            "tick_seq": tick_seq,
            "post_gap": post_gap,
        },
        "missions": [],
        "incidents": [],
        "cost": {"classification": "unknown"},
    }


def run_tick(
    manifest: Manifest,
    ledger_path: str | Path,
    status_path: str | Path,
    collector: Collector,
    *,
    now: float | None = None,
    debounce_ticks: int = 2,
) -> dict[str, Any]:
    timestamp = time.time() if now is None else now
    digest = canonical_manifest_hash(manifest)
    try:
        ledger = Ledger.open(ledger_path)
    except LedgerError:
        payload = _base_projection(timestamp, 0, state="critical", post_gap=False)
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
            {
                "id": "ledger-unavailable",
                "phase_key": "supervisor",
                "kind": "ledger_unavailable",
                "severity": "critical",
                "age_ticks": 1,
                "observed_ticks": 1,
                "disposition": "active",
            }
        ]
        write_projection(status_path, payload)
        return payload
    with ledger:
        if ledger.mission_hash(manifest.mission.id) != digest:
            payload = _base_projection(timestamp, 0, state="critical", post_gap=False)
            payload["incidents"] = [
                {
                    "id": "manifest-binding-mismatch",
                    "phase_key": "supervisor",
                    "kind": "manifest_binding_mismatch",
                    "severity": "critical",
                    "age_ticks": 1,
                    "observed_ticks": 1,
                    "disposition": "active",
                }
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
        candidates: list[dict[str, str]] = []
        for phase in mission_state.phases:
            if phase.state in {"unknown", "failed", "blocked"}:
                incident_id = hashlib.sha256(
                    f"{manifest.mission.id}\0{phase.key}\0{phase.state}".encode()
                ).hexdigest()[:24]
                candidates.append(
                    {
                        "incident_id": incident_id,
                        "phase_key": phase.key,
                        "kind": f"phase_{phase.state}",
                        "severity": "critical"
                        if phase.state in {"unknown", "failed"}
                        else "warning",
                    }
                )
        incidents = ledger.reconcile_incidents(
            manifest.mission.id, tick_seq, candidates, debounce_ticks
        )
        active_critical = any(
            item["severity"] == "critical" and item["disposition"] == "active" for item in incidents
        )
        payload = _base_projection(
            timestamp,
            tick_seq,
            state="critical" if active_critical else ("degraded" if incidents else "ok"),
            post_gap=post_gap,
        )
        payload["missions"] = [mission_state.to_dict(digest)]
        payload["incidents"] = incidents
        ledger.commit_tick(tick_seq, timestamp)
        write_projection(status_path, payload)
        return payload
