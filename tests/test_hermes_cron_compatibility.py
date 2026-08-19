from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HERMES_SOURCE = Path("/usr/local/lib/hermes-agent")
HERMES_PYTHON = HERMES_SOURCE / "venv" / "bin" / "python"


@pytest.mark.skipif(not HERMES_PYTHON.is_file(), reason="installed Hermes seam unavailable")
def test_disposable_default_profile_cron_seams(tmp_path: Path) -> None:
    environment = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "HERMES_HOME": str(tmp_path / "home" / ".hermes" / "profiles" / "default"),
        "PYTHONPATH": str(HERMES_SOURCE),
    }
    completed = subprocess.run(  # noqa: S603 - current interpreter and repository harness
        [str(HERMES_PYTHON), str(REPO / "scripts" / "verify_hermes_cron_seams.py")],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report == {
        "protocol": "cyclops-hermes-seam-evidence/v1",
        "canonical_profile": True,
        "configured_toolsets": ["no_mcp"],
        "courier_empty_is_silent": True,
        "non_task_scoped": True,
        "quiet_agent_calls": 0,
        "resolved_tools": [],
        "resolved_toolsets": [],
    }
    assert Path(environment["HERMES_HOME"]).is_relative_to(tmp_path)
