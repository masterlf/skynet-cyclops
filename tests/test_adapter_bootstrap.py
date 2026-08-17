from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import manifest_data

from skynet_cyclops.adapter import (
    HermesAdapter,
    ReadOnlyCollector,
    normalize_diagnostics,
    normalize_run_rows,
    normalize_task_rows,
    sanitize_environment,
    validate_diagnostic_collection,
    validate_json_collection,
)
from skynet_cyclops.bootstrap import apply_bootstrap, plan_bootstrap
from skynet_cyclops.errors import AdapterError, ValidationError
from skynet_cyclops.ledger import Ledger
from skynet_cyclops.manifest import parse_manifest


def test_environment_strips_authority_and_secrets(tmp_path: Path) -> None:
    synthetic_home = str(tmp_path / "synthetic-home")
    env = sanitize_environment(
        {
            "PATH": "/usr/bin",
            "HOME": synthetic_home,
            "LANG": "C.UTF-8",
            "HERMES_DELEGATED_CHILD_CONTEXT": "1",
            "HERMES_KANBAN_TASK": "secret-task",
            "HERMES_KANBAN_BOARD": "private-board",
            "API_TOKEN": "secret",
        }
    )
    assert env == {"PATH": "/usr/bin", "HOME": synthetic_home, "LANG": "C.UTF-8"}


def test_adapter_argv_shell_false_timeout_and_output_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = HermesAdapter(binary="hermes", timeout_seconds=3, max_output_bytes=1024)
    assert adapter.run_json(["profile", "show", "builder"]) == {"ok": True}
    assert observed["argv"] == ["hermes", "profile", "show", "builder"]
    assert observed["kwargs"]["shell"] is False
    assert observed["kwargs"]["timeout"] == 3
    assert "HERMES_KANBAN_TASK" not in observed["kwargs"]["env"]

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="x" * 1025, stderr=""),
    )
    with pytest.raises(AdapterError, match="output limit"):
        adapter.run_json(["profile", "show", "builder"])

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(args[0], 3, output="sensitive")

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AdapterError, match="timed out") as caught:
        adapter.run_json(["profile", "show", "builder"])
    assert "sensitive" not in str(caught.value)


def test_adapter_rejects_bad_json_and_command_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = HermesAdapter()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, stdout="private", stderr="raw failure"
        ),
    )
    with pytest.raises(AdapterError, match="command failed") as caught:
        adapter.run_json(["profile", "show", "builder"])
    assert "raw failure" not in str(caught.value)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="not-json", stderr=""),
    )
    with pytest.raises(AdapterError, match="invalid JSON"):
        adapter.run_json(["profile", "show", "builder"])


def test_adapter_fails_closed_on_invalid_bounds_argv_and_unavailable_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="binary"):
        HermesAdapter(binary="")
    with pytest.raises(ValidationError, match="bounds"):
        HermesAdapter(timeout_seconds=0)
    adapter = HermesAdapter()
    with pytest.raises(AdapterError, match="arguments"):
        adapter.run_text([])

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("private executable detail")

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(AdapterError, match="unavailable") as caught:
        adapter.run_text(["profile", "show", "builder"])
    assert "private executable detail" not in str(caught.value)


def test_collection_deadline_and_read_only_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed["timeout"] = kwargs["timeout"]
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("skynet_cyclops.adapter.time.monotonic", lambda: 100.0)
    adapter = HermesAdapter(timeout_seconds=10, collection_timeout_seconds=30)
    assert adapter._run(["profile", "show", "builder"], deadline=105.0) == "{}"
    assert observed["timeout"] == 5.0
    with pytest.raises(AdapterError, match="deadline"):
        adapter._run(["profile", "show", "builder"], deadline=99.0)
    facade = ReadOnlyCollector(adapter)
    assert hasattr(facade, "collect")
    assert not any(hasattr(facade, name) for name in ("create_task", "link_tasks", "promote_task"))


def _raw_task(**changes: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": "task-1",
        "title": "Synthetic task",
        "assignee": "builder",
        "status": "blocked",
    }
    task.update(changes)
    return task


def test_task_normalization_preserves_only_bounded_metadata_and_marker() -> None:
    normalized = normalize_task_rows(
        [
            _raw_task(
                body="[cyclops-idempotency:cyclops-safe-key]\nhostile prose",
                max_retries=3,
            )
        ]
    )
    assert normalized == [
        {
            "id": "task-1",
            "title": "Synthetic task",
            "assignee": "builder",
            "status": "blocked",
            "bootstrap_key": "cyclops-safe-key",
            "evidence": [],
            "retry_count": 0,
            "max_retries": 3,
        }
    ]


@pytest.mark.parametrize(
    "contract",
    [
        "",
        "{",
        "x" * 4097,
        json.dumps({"schema_version": 1}),
        json.dumps(
            {
                "schema_version": 1,
                "phase_key": "bad key",
                "kind": "implementation",
                "goal_mode": False,
                "max_runtime_seconds": 30,
                "max_retries": 1,
                "evidence_required": ["tests"],
            }
        ),
        json.dumps(
            {
                "schema_version": 2,
                "phase_key": "build",
                "kind": "implementation",
                "goal_mode": "no",
                "max_runtime_seconds": True,
                "max_retries": 1,
                "evidence_required": "tests",
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "phase_key": "build",
                "kind": "implementation",
                "goal_mode": False,
                "max_runtime_seconds": 30,
                "max_retries": 1,
                "evidence_required": ["tests", "tests"],
            }
        ),
        json.dumps(
            {
                "schema_version": 1,
                "phase_key": "build",
                "kind": "implementation",
                "goal_mode": False,
                "max_runtime_seconds": 30,
                "max_retries": 1,
                "evidence_required": ["bad evidence"],
            }
        ),
    ],
)
def test_task_normalization_rejects_malformed_bootstrap_contract(contract: str) -> None:
    row = normalize_task_rows(
        [_raw_task(body=f"[cyclops-idempotency:cyclops-safe-key]\n{contract}")]
    )[0]
    assert row["bootstrap_key"] == "cyclops-safe-key"
    assert "bootstrap_contract" not in row


@pytest.mark.parametrize(
    "rows",
    [
        {},
        [None],
        [{"id": "task-1", "title": "Synthetic task", "assignee": "builder"}],
        [_raw_task(id="../unsafe")],
        [_raw_task(title="bad\x00title")],
        [_raw_task(assignee="../unsafe")],
        [_raw_task(status="bad status")],
        [_raw_task(max_retries=True)],
        [_raw_task(body=b"not text")],
        [_raw_task(), _raw_task()],
    ],
)
def test_task_normalization_rejects_malformed_or_ambiguous_rows(rows: object) -> None:
    with pytest.raises(ValidationError):
        normalize_task_rows(rows)


def _raw_run(**changes: object) -> dict[str, object]:
    run: dict[str, object] = {"id": "run-1", "status": "running", "metadata": {}}
    run.update(changes)
    return run


def test_run_and_diagnostic_normalization_rejects_unbounded_or_ambiguous_data() -> None:
    normalized = normalize_run_rows([_raw_run(metadata={"evidence": "not-a-container"})], "task-1")
    assert normalized[0]["_evidence"] == []
    normalized = normalize_run_rows(
        [
            _raw_run(
                status="done",
                outcome="completed",
                ended_at=2,
                metadata={"evidence": ["tests", "tests", "../unsafe"]},
            )
        ],
        "task-1",
    )
    assert normalized[0]["_evidence"] == ["tests"]
    normalized = normalize_run_rows(
        [
            _raw_run(
                status="done",
                outcome="completed",
                ended_at=2,
                metadata={"evidence": {"commit": True}},
            )
        ],
        "task-1",
    )
    assert normalized[0]["_evidence"] == ["commit"]
    falsy = normalize_run_rows(
        [
            _raw_run(
                status="done",
                outcome="completed",
                ended_at=2,
                metadata={
                    "evidence": {
                        "none": None,
                        "false": False,
                        "empty_text": "",
                        "empty_list": [],
                        "empty_object": {},
                        "zero": 0,
                        "tests": True,
                    }
                },
            )
        ],
        "task-1",
    )
    assert falsy[0]["_evidence"] == ["tests"]
    failed = normalize_run_rows(
        [
            _raw_run(
                status="failed",
                outcome="crashed",
                ended_at=2,
                metadata={"evidence": {"stale": True}},
            )
        ],
        "task-1",
    )
    assert failed[0]["_evidence"] == []
    for rows in ({}, [None], [_raw_run(), _raw_run()], [_raw_run(metadata={"x": {1}})]):
        with pytest.raises(ValidationError):
            normalize_run_rows(rows, "task-1")

    raw_diagnostic = {
        "task_id": "task-1",
        "title": "Synthetic task",
        "status": "running",
        "assignee": "builder",
        "diagnostics": [{"kind": "bounded"}],
    }
    redacted = normalize_diagnostics([raw_diagnostic])
    assert redacted[0] == {
        "task_id": "task-1",
        "title_length": 14,
        "status": "running",
        "assignee": "builder",
        "diagnostic_count": 1,
    }
    assert validate_diagnostic_collection(redacted) == redacted
    with pytest.raises(ValidationError, match="duplicate"):
        validate_diagnostic_collection(redacted * 2)
    with pytest.raises(ValidationError):
        normalize_diagnostics([dict(raw_diagnostic, diagnostics="bad")])
    with pytest.raises(ValidationError):
        validate_diagnostic_collection([dict(redacted[0], diagnostic_count=True)])


def test_run_evidence_parser_rejects_malformed_values() -> None:
    rows = [
        _raw_run(
            id="run-1", status="done", outcome="completed", ended_at=1, metadata={"evidence": None}
        ),
        _raw_run(
            id="run-2", status="done", outcome="completed", ended_at=1, metadata={"evidence": 1}
        ),
        _raw_run(
            id="run-3", status="done", outcome="completed", ended_at=1, metadata={"evidence": [1]}
        ),
        _raw_run(
            id="run-4",
            status="done",
            outcome="completed",
            ended_at=1,
            metadata={"evidence": ["bad key"]},
        ),
    ]
    assert all(not row["_evidence"] for row in normalize_run_rows(rows, "task-1"))


def test_adapter_matches_live_cli_shapes_and_exact_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task = {
        "id": "task-1",
        "title": "Synthetic task",
        "body": "hostile prose is discarded",
        "assignee": "builder",
        "status": "running",
        "priority": 1,
        "tenant": None,
        "workspace_kind": "shared",
        "workspace_path": "/synthetic/discarded",
        "branch_name": "feature/synthetic",
        "project_id": None,
        "created_by": "operator",
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:01:00Z",
        "completed_at": None,
        "result": "untrusted result prose",
        "skills": [],
        "max_retries": 2,
        "model_override": None,
        "provider_override": None,
        "session_id": None,
        "workflow_template_id": None,
        "current_step_key": None,
    }
    detail = {
        "task": task,
        "latest_summary": "discarded summary",
        "parents": [],
        "children": [],
        "comments": [{"body": "discarded comment"}],
        "events": [{"detail": "discarded event"}],
        "runs": [
            {
                "id": "run-1",
                "profile": "builder",
                "step_key": None,
                "status": "done",
                "outcome": "completed",
                "summary": "discarded run summary",
                "error": None,
                "metadata": {"evidence": {"commit": "discarded", "tests": True}},
                "worker_pid": 123,
                "started_at": "2026-01-01T00:01:00Z",
                "ended_at": 1767225720,
            }
        ],
    }
    diagnostics = [
        {
            "task_id": "task-1",
            "title": "Synthetic task",
            "status": "running",
            "assignee": "builder",
            "diagnostics": [{"kind": "bounded"}],
        }
    ]
    observed: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append(argv)
        arguments = argv[1:]
        if arguments == ["profile", "show", "builder"]:
            return subprocess.CompletedProcess(argv, 0, stdout="Profile builder\n", stderr="")
        if arguments[-3:] == ["show", "task-1", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(detail), stderr="")
        if arguments[-2:] == ["diagnostics", "--json"]:
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(diagnostics), stderr="")
        if "create" in arguments:
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps({"id": "task-2"}), stderr=""
            )
        if "link" in arguments:
            return subprocess.CompletedProcess(argv, 0, stdout="linked\n", stderr="")
        raise AssertionError(arguments)

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = HermesAdapter(
        environment={"PATH": "/usr/bin", "HOME": str(tmp_path / "synthetic-home")}
    )
    adapter.preflight_profile("builder")
    collection = adapter.collect("default", ["task-1"])
    created = adapter.create_task(
        "default",
        "Create synthetic task",
        "builder",
        [],
        "cyclops-key",
        kind="implementation",
        max_runtime_seconds=600,
        max_retries=2,
        goal_mode=True,
        evidence_required=["commit", "tests"],
        phase_key="build",
    )
    adapter.link_tasks("default", "task-1", "task-2")
    assert created == {"id": "task-2"}
    assert collection["tasks"][0]["evidence"] == ["commit", "tests"]  # type: ignore[index]
    assert "body" not in collection["tasks"][0]  # type: ignore[index]
    assert observed[0] == ["hermes", "profile", "show", "builder"]
    assert observed[1] == ["hermes", "kanban", "--board", "default", "show", "task-1", "--json"]
    assert observed[2] == ["hermes", "kanban", "--board", "default", "diagnostics", "--json"]
    assert observed[3][:5] == ["hermes", "kanban", "--board", "default", "create"]
    assert "--body" in observed[3]
    assert observed[3][observed[3].index("--idempotency-key") + 1] == "cyclops-key"
    assert observed[3][observed[3].index("--initial-status") + 1] == "blocked"
    assert observed[3][observed[3].index("--max-runtime") + 1] == "600"
    assert observed[3][observed[3].index("--max-retries") + 1] == "2"
    assert "--goal" in observed[3]
    body = observed[3][observed[3].index("--body") + 1]
    marker, contract = body.split("\n", 1)
    assert marker == "[cyclops-idempotency:cyclops-key]"
    assert json.loads(contract) == {
        "schema_version": 1,
        "phase_key": "build",
        "kind": "implementation",
        "goal_mode": True,
        "max_runtime_seconds": 600,
        "max_retries": 2,
        "evidence_required": ["commit", "tests"],
    }
    assert observed[4] == ["hermes", "kanban", "--board", "default", "link", "task-1", "task-2"]


@pytest.mark.parametrize("value", [{}, [1], [{"id": "x", "body": "forbidden"}], [None] * 2001])
def test_json_collection_strict_shape(value: object) -> None:
    with pytest.raises(ValidationError):
        validate_json_collection(value, kind="tasks")


def _task_detail() -> dict[str, object]:
    return {
        "task": _raw_task(),
        "latest_summary": None,
        "parents": [],
        "children": [],
        "comments": [],
        "events": [],
        "runs": [],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("task", _raw_task(id="task-other")),
        lambda value: value.__setitem__("latest_summary", "x" * (64 * 1024 + 1)),
        lambda value: value.__setitem__("parents", "task-parent"),
        lambda value: value.__setitem__("parents", ["../unsafe"]),
        lambda value: value.__setitem__("comments", "private prose"),
    ],
)
def test_task_detail_rejects_schema_binding_and_unbounded_nested_data(mutate: object) -> None:
    detail = _task_detail()
    mutate(detail)  # type: ignore[operator]
    adapter = HermesAdapter()
    adapter.run_json = lambda _arguments: detail  # type: ignore[method-assign]
    with pytest.raises(ValidationError):
        adapter.show_task("default", "task-1")


def test_adapter_collection_and_mutation_responses_are_bound_and_fail_closed() -> None:
    adapter = HermesAdapter()
    responses: list[object] = [
        [_raw_task()],
        _task_detail(),
        [],
        {"unexpected": True},
        {"ok": True},
    ]
    adapter.run_json = lambda _arguments: responses.pop(0)  # type: ignore[method-assign]
    assert adapter.list_tasks("default")[0]["id"] == "task-1"
    assert adapter.task_parents("default", "task-1") == []
    assert adapter.diagnostics("default") == []
    with pytest.raises(AdapterError, match="create response"):
        adapter.create_task(
            "default",
            "Synthetic",
            "builder",
            ["task-parent"],
            "cyclops-key",
            phase_key="build",
            kind="implementation",
            goal_mode=False,
            max_runtime_seconds=600,
            max_retries=2,
            evidence_required=["tests"],
        )
    with pytest.raises(ValidationError, match="bound task"):
        adapter.collect("default", ["task-1", "task-1"])
    adapter.promote_task("default", "task-1")

    adapter.run_json = lambda _arguments: []  # type: ignore[method-assign]
    with pytest.raises(AdapterError, match="promote response"):
        adapter.promote_task("default", "task-1")


def test_collection_never_uses_stale_or_failed_run_evidence() -> None:
    detail = _task_detail()
    detail["task"] = _raw_task(status="done")
    detail["runs"] = [
        _raw_run(
            id=1,
            status="done",
            outcome="completed",
            ended_at=2,
            metadata={"evidence": {"stale": True}},
        ),
        _raw_run(
            id=2,
            status="failed",
            outcome="crashed",
            ended_at=3,
            metadata={"evidence": {"failed": True}},
        ),
    ]
    responses = [detail, []]
    adapter = HermesAdapter()
    adapter._run_json = lambda *_args, **_kwargs: responses.pop(0)  # type: ignore[method-assign]
    collection = adapter.collect("default", ["task-1"])
    assert collection["tasks"][0]["evidence"] == []  # type: ignore[index]
    assert [run["id"] for run in collection["runs"]] == ["1", "2"]  # type: ignore[index]


class FakeAdapter:
    def __init__(self, *, lose_first_response: bool = False) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.calls: list[tuple[str, ...]] = []
        self.lose_first_response = lose_first_response
        self.parents: dict[str, list[str]] = {}
        self.promoted: list[str] = []

    def preflight_profile(self, name: str) -> None:
        self.calls.append(("profile", name))

    def list_tasks(self, board: str) -> list[dict[str, Any]]:
        self.calls.append(("list", board))
        return list(self.tasks)

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
    ) -> dict[str, Any]:
        self.calls.append(("create", board, idempotency_key, *parents))
        assert parents == []
        task = {
            "id": f"task-{len(self.tasks) + 1}",
            "title": title,
            "assignee": assignee,
            "status": "blocked",
            "bootstrap_key": idempotency_key,
            "bootstrap_contract": {
                "schema_version": 1,
                "phase_key": phase_key,
                "kind": kind,
                "goal_mode": goal_mode,
                "max_runtime_seconds": max_runtime_seconds,
                "max_retries": max_retries,
                "evidence_required": sorted(evidence_required),
            },
            "evidence": [],
            "retry_count": 0,
        }
        self.tasks.append(task)
        self.parents[task["id"]] = []
        if self.lose_first_response:
            self.lose_first_response = False
            raise AdapterError("response unavailable")
        return task

    def link_tasks(self, board: str, parent_id: str, child_id: str) -> None:
        assert all(task["status"] == "blocked" for task in self.tasks)
        self.calls.append(("link", board, parent_id, child_id))
        if parent_id not in self.parents[child_id]:
            self.parents[child_id].append(parent_id)

    def task_parents(self, board: str, task_id: str) -> list[str]:
        self.calls.append(("parents", board, task_id))
        return list(self.parents[task_id])

    def task_status(self, board: str, task_id: str) -> str:
        return next(str(task["status"]) for task in self.tasks if task["id"] == task_id)

    def promote_task(self, board: str, task_id: str) -> None:
        assert self.parents["task-2"] == ["task-1"]
        assert self.parents["task-3"] == ["task-2"]
        self.promoted.append(task_id)
        next(task for task in self.tasks if task["id"] == task_id)["status"] = "ready"


def _item_contract(item: Any) -> dict[str, object]:
    return {
        "schema_version": 1,
        "phase_key": item.phase_key,
        "kind": item.kind,
        "goal_mode": item.goal_mode,
        "max_runtime_seconds": item.max_runtime_seconds,
        "max_retries": item.max_retries,
        "evidence_required": list(item.evidence_required),
    }


def test_bootstrap_dry_run_default_has_no_calls() -> None:
    adapter = FakeAdapter()
    manifest = parse_manifest(manifest_data())
    plan = plan_bootstrap(manifest)
    assert [item.phase_key for item in plan] == ["build", "review", "verify"]
    assert adapter.calls == []
    assert len({item.idempotency_key for item in plan}) == 3
    assert plan[0].to_dict() | {} == {
        "phase_key": "build",
        "title": "Build synthetic candidate",
        "assignee": "builder",
        "dependency_phases": [],
        "idempotency_key": plan[0].idempotency_key,
        "kind": "implementation",
        "goal_mode": False,
        "max_runtime_seconds": 600,
        "max_retries": 2,
        "evidence_required": ["commit", "tests"],
    }


def test_bootstrap_apply_is_idempotent_and_reconciles_response_loss(tmp_path: Path) -> None:
    adapter = FakeAdapter(lose_first_response=True)
    manifest = parse_manifest(manifest_data())
    with Ledger.create(tmp_path / "ledger.db") as ledger:
        first = apply_bootstrap(manifest, adapter, ledger)
        second = apply_bootstrap(manifest, adapter, ledger)
        assert first == second
        assert len(adapter.tasks) == 3
        assert ledger.bindings(manifest.mission.id) == first
        assert adapter.promoted == ["task-1"]
    assert os.stat(tmp_path / "ledger.db").st_mode & 0o777 == 0o600


def test_invalid_graph_causes_no_profile_or_create_calls(tmp_path: Path) -> None:
    data = manifest_data()
    data["phases"][0]["depends_on"] = ["missing"]
    adapter = FakeAdapter()
    with pytest.raises(ValidationError):
        manifest = parse_manifest(data)
        with Ledger.create(tmp_path / "ledger.db") as ledger:
            apply_bootstrap(manifest, adapter, ledger)
    assert adapter.calls == []


def test_reversed_manifest_order_bootstraps_topologically(tmp_path: Path) -> None:
    data = manifest_data()
    data["phases"] = list(reversed(data["phases"]))
    manifest = parse_manifest(data)
    assert [item.phase_key for item in plan_bootstrap(manifest)] == ["build", "review", "verify"]
    adapter = FakeAdapter()
    with Ledger.create(tmp_path / "ledger.db") as ledger:
        apply_bootstrap(manifest, adapter, ledger)
    assert adapter.promoted == ["task-1"]


def test_graph_verification_failure_never_promotes(tmp_path: Path) -> None:
    class BrokenGraphAdapter(FakeAdapter):
        def task_parents(self, board: str, task_id: str) -> list[str]:
            actual = super().task_parents(board, task_id)
            if task_id == "task-3" and actual:
                return []
            return actual

    adapter = BrokenGraphAdapter()
    manifest = parse_manifest(manifest_data())
    with Ledger.create(tmp_path / "ledger.db") as ledger, pytest.raises(AdapterError):
        apply_bootstrap(manifest, adapter, ledger)
    assert adapter.promoted == []
    assert all(task["status"] == "blocked" for task in adapter.tasks)


def test_bootstrap_reuses_preexisting_idempotent_card_without_duplicate_creation(
    tmp_path: Path,
) -> None:
    manifest = parse_manifest(manifest_data())
    first_item = plan_bootstrap(manifest)[0]
    adapter = FakeAdapter()
    adapter.tasks.append(
        {
            "id": "task-1",
            "title": first_item.title,
            "assignee": first_item.assignee,
            "status": "blocked",
            "bootstrap_key": first_item.idempotency_key,
            "bootstrap_contract": first_item.contract(),
        }
    )
    adapter.parents["task-1"] = []
    with Ledger.create(tmp_path / "ledger.db") as ledger:
        bindings = apply_bootstrap(manifest, adapter, ledger)
    assert bindings["build"] == "task-1"
    assert len([call for call in adapter.calls if call[0] == "create"]) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "Wrong title"},
        {"assignee": "reviewer"},
        {"status": "ready"},
        {"bootstrap_contract": {"schema_version": 1}},
    ],
)
def test_bootstrap_never_adopts_or_promotes_mismatched_preseeded_card(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    manifest = parse_manifest(manifest_data())
    item = plan_bootstrap(manifest)[0]
    adapter = FakeAdapter()
    task = {
        "id": "task-seeded",
        "title": item.title,
        "assignee": item.assignee,
        "status": "blocked",
        "bootstrap_key": item.idempotency_key,
        "bootstrap_contract": _item_contract(item),
    }
    task.update(changes)
    adapter.tasks.append(task)
    adapter.parents["task-seeded"] = []
    with (
        Ledger.create(tmp_path / "ledger.db") as ledger,
        pytest.raises(AdapterError, match="match"),
    ):
        apply_bootstrap(manifest, adapter, ledger)
    assert adapter.promoted == []
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        assert ledger.bindings(manifest.mission.id) == {}


def test_bootstrap_rejects_ambiguous_preseeded_candidates_before_binding(tmp_path: Path) -> None:
    manifest = parse_manifest(manifest_data())
    item = plan_bootstrap(manifest)[0]
    adapter = FakeAdapter()
    for task_id in ("task-seeded-1", "task-seeded-2"):
        adapter.tasks.append(
            {
                "id": task_id,
                "title": item.title,
                "assignee": item.assignee,
                "status": "blocked",
                "bootstrap_key": item.idempotency_key,
                "bootstrap_contract": _item_contract(item),
            }
        )
        adapter.parents[task_id] = []
    with (
        Ledger.create(tmp_path / "ledger.db") as ledger,
        pytest.raises(AdapterError, match="ambiguous"),
    ):
        apply_bootstrap(manifest, adapter, ledger)
    assert adapter.promoted == []


def test_response_loss_reconciliation_rejects_mismatched_card(tmp_path: Path) -> None:
    class MismatchingLostResponse(FakeAdapter):
        def create_task(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            task = super().create_task(*args, **kwargs)
            task["assignee"] = "reviewer"
            raise AdapterError("response unavailable")

    manifest = parse_manifest(manifest_data())
    adapter = MismatchingLostResponse()
    with (
        Ledger.create(tmp_path / "ledger.db") as ledger,
        pytest.raises(AdapterError, match="match"),
    ):
        apply_bootstrap(manifest, adapter, ledger)
    assert adapter.promoted == []


def test_bootstrap_rejects_ambiguous_create_reconciliation(tmp_path: Path) -> None:
    class AmbiguousAdapter(FakeAdapter):
        def list_tasks(self, board: str) -> list[dict[str, Any]]:
            rows = super().list_tasks(board)
            return rows if not rows else [*rows, dict(rows[0], id="task-duplicate")]

    adapter = AmbiguousAdapter(lose_first_response=True)
    manifest = parse_manifest(manifest_data())
    with (
        Ledger.create(tmp_path / "ledger.db") as ledger,
        pytest.raises(AdapterError, match="ambiguous"),
    ):
        apply_bootstrap(manifest, adapter, ledger)
    assert len(adapter.tasks) == 1
    assert adapter.promoted == []


def test_bootstrap_reconciles_lost_link_and_promote_responses(tmp_path: Path) -> None:
    class LostResponsesAdapter(FakeAdapter):
        def link_tasks(self, board: str, parent_id: str, child_id: str) -> None:
            super().link_tasks(board, parent_id, child_id)
            raise AdapterError("link response unavailable")

        def promote_task(self, board: str, task_id: str) -> None:
            super().promote_task(board, task_id)
            raise AdapterError("promote response unavailable")

    adapter = LostResponsesAdapter()
    manifest = parse_manifest(manifest_data())
    with Ledger.create(tmp_path / "ledger.db") as ledger:
        bindings = apply_bootstrap(manifest, adapter, ledger)
    assert bindings == {"build": "task-1", "review": "task-2", "verify": "task-3"}
    assert adapter.promoted == ["task-1"]


def test_bootstrap_hard_link_failure_and_unexpected_edge_never_promote(tmp_path: Path) -> None:
    class HardLinkFailure(FakeAdapter):
        def link_tasks(self, board: str, parent_id: str, child_id: str) -> None:
            raise AdapterError("link failed")

    manifest = parse_manifest(manifest_data())
    adapter = HardLinkFailure()
    with Ledger.create(tmp_path / "first.db") as ledger, pytest.raises(AdapterError):
        apply_bootstrap(manifest, adapter, ledger)
    assert adapter.promoted == []

    class UnexpectedEdge(FakeAdapter):
        def task_parents(self, board: str, task_id: str) -> list[str]:
            parents = super().task_parents(board, task_id)
            return ["alien-task"] if task_id == "task-2" else parents

    adapter = UnexpectedEdge()
    with (
        Ledger.create(tmp_path / "second.db") as ledger,
        pytest.raises(AdapterError, match="unexpected dependencies"),
    ):
        apply_bootstrap(manifest, adapter, ledger)
    assert adapter.promoted == []
