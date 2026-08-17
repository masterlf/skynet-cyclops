from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
DASHBOARD = REPO / "integrations" / "hermes-dashboard" / "skynet-cyclops"


def load_dashboard_api() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "cyclops_dashboard_api", DASHBOARD / "plugin_api.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_status() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "projection_version": 1,
        "supervisor": {
            "mode": "observe",
            "state": "ok",
            "heartbeat_at": 1.0,
            "tick_seq": 1,
            "post_gap": False,
        },
        "missions": [
            {
                "id": "synthetic-release",
                "manifest_sha256": "a" * 64,
                "outcome": "running",
                "next_phase": "review",
                "phases": [{"key": "review", "state": "review", "evidence_present": []}],
                "workers": [
                    {
                        "task_id": "task-1",
                        "run_id": "run-1",
                        "assignee": "reviewer",
                        "status": "running",
                        "heartbeat_age_seconds": 4,
                        "retry_count": 0,
                    }
                ],
            }
        ],
        "incidents": [],
        "cost": {"classification": "unknown"},
    }


def test_dashboard_api_validates_status_and_never_leaks_paths(tmp_path: Path) -> None:
    module = load_dashboard_api()
    routes = [route for route in module.router.routes if getattr(route, "path", None) == "/status"]
    assert len(routes) == 1
    assert routes[0].methods == {"GET"}
    path = tmp_path / "status.json"
    path.write_text(json.dumps(valid_status()), encoding="utf-8")
    os.chmod(path, 0o600)
    response = module.read_status(path)
    assert response["supervisor"]["mode"] == "observe"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(module.StatusUnavailable) as caught:
        module.read_status(path)
    assert str(path) not in str(caught.value)
    path.write_text(json.dumps(valid_status()), encoding="utf-8")
    os.chmod(path, 0o644)
    with pytest.raises(module.StatusUnavailable, match="unavailable"):
        module.read_status(path)


def test_dashboard_api_rejects_symlink_oversize_and_unknown_fields(tmp_path: Path) -> None:
    module = load_dashboard_api()
    real = tmp_path / "real.json"
    real.write_text(json.dumps(valid_status()), encoding="utf-8")
    os.chmod(real, 0o600)
    link = tmp_path / "status.json"
    link.symlink_to(real)
    with pytest.raises(module.StatusUnavailable):
        module.read_status(link)
    real.write_bytes(b"x" * 300_000)
    with pytest.raises(module.StatusUnavailable):
        module.read_status(real)
    poisoned = valid_status()
    poisoned["raw_log"] = "secret"
    real.write_text(json.dumps(poisoned), encoding="utf-8")
    with pytest.raises(module.StatusUnavailable):
        module.read_status(real)


def test_dashboard_bundle_executes_with_sdk_and_is_read_only() -> None:
    bundle = DASHBOARD / "dist" / "index.js"
    source = bundle.read_text(encoding="utf-8")
    forbidden = [
        "fetch(",
        "XMLHttpRequest",
        "innerHTML",
        "eval(",
        "new Function",
        "setTimeout('",
        "setInterval('",
    ]
    assert not any(term in source for term in forbidden)
    assert "POST" not in source and "PUT" not in source and "DELETE" not in source
    assert "__HERMES_PLUGIN_SDK__" in source and "__HERMES_PLUGINS__" in source
    if shutil.which("node"):
        harness = r"""
let registered = null;
let calls = [];
const React = {
  createElement: (tag, props, ...children) => ({tag, props: props || {}, children}),
  useState: (initial) => [initial, () => {}],
  useEffect: (fn) => { fn(); },
};
global.window = {
  __HERMES_PLUGIN_SDK__: {
    React,
    fetchJSON: (url) => {
      calls.push(url);
      if (url !== "/api/plugins/skynet-cyclops/status") process.exit(3);
      return Promise.resolve({});
    },
  },
  __HERMES_PLUGINS__: {
    register: function(name, component) {
      if (arguments.length !== 2 || name !== "skynet-cyclops") process.exit(2);
      if (typeof component !== "function") process.exit(2);
      registered = component;
    },
  },
};
require(process.argv[1]);
if (!registered) process.exit(4);
const rendered = registered();
if (!rendered || rendered.tag !== "section") process.exit(5);
if (rendered.props["aria-label"] !== "Skynet-Cyclops read-only status") process.exit(5);
if (calls.length !== 1) process.exit(6);
"""
        node = shutil.which("node")
        assert node is not None
        completed = subprocess.run(  # noqa: S603 - fixed executable and test-owned argv
            [node, "-e", harness, str(bundle)], check=False, capture_output=True, text=True
        )
        assert completed.returncode == 0, completed.stderr


def test_dashboard_manifest_matches_host_discovery_contract_exactly() -> None:
    manifest = json.loads((DASHBOARD / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest) == {
        "name",
        "label",
        "description",
        "icon",
        "version",
        "tab",
        "entry",
        "css",
        "api",
    }
    assert manifest["name"] == "skynet-cyclops"
    assert manifest["icon"] == "Eye"
    assert manifest["tab"] == {"path": "/skynet-cyclops", "position": "after:kanban"}
    assert manifest["entry"] == "dist/index.js"
    assert manifest["css"] == "dist/style.css"
    assert manifest["api"] == "plugin_api.py"
    assert "default_enabled" not in manifest


def run_scanner(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - sys.executable and scanner path are trusted
        [sys.executable, str(REPO / "scripts" / "public_repo_scan.py"), str(path)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_public_scanner_accepts_safe_and_rejects_sensitive_fixtures(tmp_path: Path) -> None:
    (tmp_path / "safe.txt").write_text(
        "synthetic host example.invalid and 192.0.2.10\n", encoding="utf-8"
    )
    assert run_scanner(tmp_path).returncode == 0
    generated_path = "/ro" + "ot/generated/coverage-path\n"
    (tmp_path / ".coverage").write_text(generated_path, encoding="utf-8")
    (tmp_path / "coverage.json").write_text(generated_path, encoding="utf-8")
    assert run_scanner(tmp_path).returncode == 0
    cases = {
        "email.txt": "person" + "@" + "real-domain.test\n",
        "home.txt": "/ho" + "me/private-user/file\n",
        "ip.txt": "connect " + "10." + "2.3.4\n",
        "key.txt": "-----BEGIN " + "PRIVATE KEY-----\n",
        "token.txt": "AKIA" + "A" * 16 + "\n",
    }
    for name, value in cases.items():
        candidate = tmp_path / name
        candidate.write_text(value, encoding="utf-8")
        assert run_scanner(tmp_path).returncode != 0
        candidate.unlink()


def test_public_scanner_permissions_symlinks_and_size(tmp_path: Path) -> None:
    sensitive = tmp_path / "synthetic.key"
    sensitive.write_text("placeholder\n", encoding="utf-8")
    os.chmod(sensitive, 0o644)
    assert run_scanner(tmp_path).returncode != 0
    sensitive.unlink()
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("safe\n", encoding="utf-8")
    (tmp_path / "escape").symlink_to(outside)
    assert run_scanner(tmp_path).returncode != 0
    (tmp_path / "escape").unlink()
    (tmp_path / "large.bin").write_bytes(b"0" * 1_100_000)
    assert run_scanner(tmp_path).returncode != 0


def test_systemd_and_installer_static_contract() -> None:
    service = (REPO / "packaging/systemd/skynet-cyclops.service").read_text(encoding="utf-8")
    timer = (REPO / "packaging/systemd/skynet-cyclops.timer").read_text(encoding="utf-8")
    installer = (REPO / "scripts/install-user.sh").read_text(encoding="utf-8")
    assert "Type=oneshot" in service and "TimeoutStartSec=45" in service
    assert "ExecStart=/usr/bin/env skynet-cyclops" in service
    assert "NoNewPrivileges=true" in service and "ProtectSystem=strict" in service
    assert "OnUnitActiveSec=120s" in timer and "Persistent=false" in timer
    assert "--apply" in installer and "dry-run" in installer
    assert "enable --now" not in installer
    assert "HERMES" not in installer.upper()
    mode = stat.S_IMODE((REPO / "scripts/install-user.sh").stat().st_mode)
    assert mode & stat.S_IXUSR
