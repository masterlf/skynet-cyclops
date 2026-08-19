#!/usr/bin/env python3
"""Behaviorally verify the installed Hermes seams in an isolated profile home."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType


def main() -> int:
    hermes_home = Path(os.environ["HERMES_HOME"])
    scripts = hermes_home / "scripts"
    scripts.mkdir(parents=True, mode=0o700)
    quiet = scripts / "cyclops-quiet.py"
    quiet.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    quiet.chmod(0o700)

    agent_constructions = 0
    original_run_agent = sys.modules.get("run_agent")
    sentinel = ModuleType("run_agent")

    class ForbiddenAgent:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            nonlocal agent_constructions
            agent_constructions += 1
            raise RuntimeError("no-agent seam constructed an agent")

    sentinel.AIAgent = ForbiddenAgent  # type: ignore[attr-defined]
    sys.modules["run_agent"] = sentinel
    try:
        from agent.delegation_context import (
            is_dispatcher_owned_worker_context,
            non_dispatcher_owned_context,
        )
        from cron.scheduler import _resolve_cron_enabled_toolsets, run_job
        from model_tools import get_tool_definitions

        with non_dispatcher_owned_context():
            non_task_scoped = not is_dispatcher_owned_worker_context()
            toolsets = _resolve_cron_enabled_toolsets({"enabled_toolsets": ["no_mcp"]}, {})
            resolved = get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True)
        names = sorted(
            item["function"]["name"]
            for item in resolved
            if isinstance(item, dict)
            and isinstance(item.get("function"), dict)
            and isinstance(item["function"].get("name"), str)
        )
        success, _document, final_response, error = run_job(
            {
                "id": "cyclops-disposable-courier",
                "name": "cyclops-disposable-courier",
                "script": quiet.name,
                "no_agent": True,
                "deliver": "local",
            }
        )
    finally:
        if original_run_agent is None:
            sys.modules.pop("run_agent", None)
        else:
            sys.modules["run_agent"] = original_run_agent

    report = {
        "protocol": "cyclops-hermes-seam-evidence/v1",
        "canonical_profile": (
            hermes_home.name == "default" and hermes_home.parent.name == "profiles"
        ),
        "configured_toolsets": ["no_mcp"],
        "courier_empty_is_silent": success and final_response == "[SILENT]" and error is None,
        "non_task_scoped": non_task_scoped,
        "quiet_agent_calls": agent_constructions,
        "resolved_tools": names,
        "resolved_toolsets": toolsets,
    }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return (
        0
        if all(
            (
                report["canonical_profile"],
                report["courier_empty_is_silent"],
                report["non_task_scoped"],
                report["quiet_agent_calls"] == 0,
                report["resolved_tools"] == [],
                report["configured_toolsets"] == ["no_mcp"],
                report["resolved_toolsets"] == [],
            )
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
