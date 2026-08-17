from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


def test_ci_uses_only_exact_sha_pinned_github_owned_actions() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*uses:\s*([^@\s]+)@([^\s]+)", source, flags=re.MULTILINE)
    assert uses == [
        ("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1"),
        ("actions/setup-python", "5fda3b95a4ea91299a34e894583c3862153e4b97"),
    ]
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _action, revision in uses)


def test_ci_matrix_and_required_release_gates_are_explicit() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    quality = workflow["jobs"]["quality"]
    checkout = quality["steps"][0]
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 0}
    assert quality["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
    commands = "\n".join(step.get("run", "") for step in quality["steps"])
    required = [
        'python -m pip install --upgrade "pip>=26.1.2"',
        "ruff check .",
        "ruff format --check .",
        "mypy src integrations/hermes-dashboard/skynet-cyclops/plugin_api.py "
        "scripts/public_repo_scan.py",
        "pytest --cov=skynet_cyclops --cov-report=term-missing",
        "bandit -r src integrations scripts -q",
        "pip-audit",
        "python scripts/public_repo_scan.py .",
        "python -m build",
    ]
    assert all(command in commands for command in required)
    assert workflow["permissions"] == {"contents": "read"}
