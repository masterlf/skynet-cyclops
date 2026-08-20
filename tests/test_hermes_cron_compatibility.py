from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
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
        "HERMES_HOME": str(tmp_path / "home" / ".hermes"),
        "PYTHONPATH": os.pathsep.join((str(REPO / "src"), str(HERMES_SOURCE))),
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
        "cronjob_full_field_readback": True,
        "courier_empty_is_silent": True,
        "disposable_profile": True,
        "non_task_scoped": True,
        "quiet_agent_calls": 0,
        "resolved_tools": [],
        "resolved_toolsets": [],
    }
    assert Path(environment["HERMES_HOME"]).is_relative_to(tmp_path)


@pytest.mark.skipif(not HERMES_PYTHON.is_file(), reason="installed Hermes seam unavailable")
def test_wheel_contains_and_executes_disposable_hermes_seam_verifier(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    built = subprocess.run(  # noqa: S603 - fixed current interpreter and repository build
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
        cwd=REPO,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(dist.glob("skynet_cyclops-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        assert "skynet_cyclops/hermes_cron_seams.py" in names
        entry_points = archive.read(
            next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        ).decode()
        assert (
            "cyclops-verify-hermes-cron-seams = skynet_cyclops.hermes_cron_seams:main"
            in entry_points
        )

    installed = tmp_path / "installed"
    install = subprocess.run(  # noqa: S603 - fixed installed Hermes interpreter
        [
            str(HERMES_PYTHON),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(installed),
            str(wheel),
        ],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert install.returncode == 0, install.stderr
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(installed), str(HERMES_SOURCE))),
    }
    environment.pop("HERMES_HOME", None)
    completed = subprocess.run(  # noqa: S603 - fixed installed Hermes interpreter
        [str(HERMES_PYTHON), "-m", "skynet_cyclops.hermes_cron_seams"],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["protocol"] == "cyclops-hermes-seam-evidence/v1"
    assert report["cronjob_full_field_readback"] is True
    assert report["disposable_profile"] is True
    assert not (tmp_path / "home" / ".hermes").exists()
