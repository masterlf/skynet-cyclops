"""Verify installed Hermes cron seams in a synthetic disposable profile only."""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import ModuleType


def _verify_in_disposable_profile(hermes_home: Path) -> dict[str, object]:
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
        delegation = importlib.import_module("agent.delegation_context")
        scheduler = importlib.import_module("cron.scheduler")
        model_tools = importlib.import_module("model_tools")
        cronjob = importlib.import_module("tools.cronjob_tools").cronjob
        is_dispatcher_owned_worker_context = delegation.is_dispatcher_owned_worker_context
        non_dispatcher_owned_context = delegation.non_dispatcher_owned_context
        resolve_toolsets = scheduler._resolve_cron_enabled_toolsets
        run_job = scheduler.run_job
        get_tool_definitions = model_tools.get_tool_definitions

        with non_dispatcher_owned_context():
            non_task_scoped = not is_dispatcher_owned_worker_context()
            toolsets = resolve_toolsets({"enabled_toolsets": ["no_mcp"]}, {})
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
        created_id: str | None = None
        cronjob_full_field_readback = False
        try:
            created = json.loads(
                cronjob(
                    "create",
                    name="cyclops-disposable-readback",
                    schedule="every 2m",
                    prompt="synthetic-seam",
                    repeat=0,
                    deliver="local",
                    skills=[],
                    script=quiet.name,
                    continuity=False,
                    enabled_toolsets=["no_mcp"],
                    no_agent=False,
                    attach_to_session=False,
                )
            )
            created_id = created.get("job_id")
            paused = json.loads(
                cronjob("pause", job_id=created_id, reason="cyclops-disposable-verification")
            )
            listed = json.loads(cronjob("list", include_disabled=True))
            matches = [job for job in listed.get("jobs", []) if job.get("job_id") == created_id]
            expected = {
                "name": "cyclops-disposable-readback",
                "skill": None,
                "skills": [],
                "prompt_preview": "synthetic-seam",
                "model": None,
                "provider": None,
                "base_url": None,
                "schedule": "every 2m",
                "repeat": "forever",
                "deliver": "local",
                "enabled": False,
                "state": "paused",
                "script": quiet.name,
                "enabled_toolsets": ["no_mcp"],
            }
            cronjob_full_field_readback = (
                created.get("success") is True
                and paused.get("success") is True
                and listed.get("success") is True
                and len(matches) == 1
                and all(matches[0].get(key) == value for key, value in expected.items())
            )
        finally:
            if created_id is not None:
                removed = json.loads(cronjob("remove", job_id=created_id))
                cronjob_full_field_readback = (
                    cronjob_full_field_readback and removed.get("success") is True
                )
    finally:
        if original_run_agent is None:
            sys.modules.pop("run_agent", None)
        else:
            sys.modules["run_agent"] = original_run_agent

    return {
        "protocol": "cyclops-hermes-seam-evidence/v1",
        "canonical_profile": (
            hermes_home.name == "default" and hermes_home.parent.name == "profiles"
        ),
        "configured_toolsets": ["no_mcp"],
        "cronjob_full_field_readback": cronjob_full_field_readback,
        "courier_empty_is_silent": success and final_response == "[SILENT]" and error is None,
        "disposable_profile": True,
        "non_task_scoped": non_task_scoped,
        "quiet_agent_calls": agent_constructions,
        "resolved_tools": names,
        "resolved_toolsets": toolsets,
    }


def verify_hermes_cron_seams() -> dict[str, object]:
    """Run the seam verifier without reading or mutating any configured Hermes profile."""
    previous = {key: os.environ.get(key) for key in ("HOME", "HERMES_HOME")}
    with tempfile.TemporaryDirectory(prefix="cyclops-hermes-seam-") as temporary:
        root = Path(temporary)
        hermes_home = root / "home" / ".hermes" / "profiles" / "default"
        os.environ["HOME"] = str(root / "home")
        os.environ["HERMES_HOME"] = str(hermes_home)
        try:
            return _verify_in_disposable_profile(hermes_home)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main() -> int:
    report = verify_hermes_cron_seams()
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return (
        0
        if all(
            (
                report["canonical_profile"],
                report["courier_empty_is_silent"],
                report["cronjob_full_field_readback"],
                report["disposable_profile"],
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
