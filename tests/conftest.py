from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def manifest_data() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mission": {
            "id": "synthetic-release",
            "display_name": "Synthetic Release",
            "board": "default",
            "tick_seconds": 120,
            "gap_damper_seconds": 300,
            "final_phase": "verify",
        },
        "phases": [
            {
                "key": "build",
                "kind": "implementation",
                "title": "Build synthetic candidate",
                "assignee": "builder",
                "depends_on": [],
                "goal_mode": False,
                "max_runtime_seconds": 600,
                "max_retries": 2,
                "evidence_required": ["commit", "tests"],
            },
            {
                "key": "review",
                "kind": "review",
                "title": "Review synthetic candidate",
                "assignee": "reviewer",
                "depends_on": ["build"],
                "goal_mode": False,
                "max_runtime_seconds": 600,
                "max_retries": 1,
                "evidence_required": ["review_outcome"],
            },
            {
                "key": "verify",
                "kind": "verification",
                "title": "Verify synthetic evidence",
                "assignee": "release",
                "depends_on": ["review"],
                "goal_mode": False,
                "max_runtime_seconds": 600,
                "max_retries": 1,
                "evidence_required": ["checksums"],
            },
        ],
    }


def write_manifest(path: Path, data: dict[str, Any] | None = None) -> Path:
    path.write_text(yaml.safe_dump(data or manifest_data(), sort_keys=False), encoding="utf-8")
    return path
