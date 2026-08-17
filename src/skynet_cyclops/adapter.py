"""Bounded, shell-free adapter for supported Hermes CLI surfaces."""

from __future__ import annotations

import json
import os
import re
import subprocess  # Used only for validated argv with shell=False.  # nosec B404
from collections.abc import Mapping
from typing import Any

from .errors import AdapterError, ValidationError

_SAFE_ENV = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_BOOTSTRAP_MARKER = re.compile(r"^\[cyclops-idempotency:([A-Za-z0-9._-]{1,128})\](?:\n|$)")
MAX_COLLECTION_ITEMS = 2000
_RAW_TASK_KEYS = {
    "id",
    "title",
    "body",
    "assignee",
    "status",
    "priority",
    "tenant",
    "workspace_kind",
    "workspace_path",
    "branch_name",
    "project_id",
    "created_by",
    "created_at",
    "started_at",
    "completed_at",
    "result",
    "skills",
    "max_retries",
    "model_override",
    "provider_override",
    "session_id",
    "workflow_template_id",
    "current_step_key",
}
_RAW_RUN_KEYS = {
    "id",
    "profile",
    "step_key",
    "status",
    "outcome",
    "summary",
    "error",
    "metadata",
    "worker_pid",
    "started_at",
    "ended_at",
}
_NORMAL_TASK_KEYS = {
    "id",
    "title",
    "status",
    "assignee",
    "bootstrap_key",
    "evidence",
    "retry_count",
    "active_run_id",
    "max_retries",
}
_NORMAL_RUN_KEYS = {"id", "task_id", "status", "heartbeat_age_seconds", "retry_count"}


def sanitize_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    original = os.environ if source is None else source
    return {key: original[key] for key in _SAFE_ENV if key in original}


def _safe_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValidationError(f"{field} must be a safe identifier")
    return value


def _safe_string(value: object, field: str, maximum: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValidationError(f"{field} must be a bounded string")
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        raise ValidationError(f"{field} contains control characters")
    return value


def _bounded_integer(value: object, field: str, maximum: int = 10**9) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValidationError(f"{field} is invalid")
    return value


def _extract_evidence(value: object) -> list[str]:
    if value is None:
        return []
    parsed = value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 64 * 1024:
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, dict):
        return []
    evidence = parsed.get("evidence")
    if isinstance(evidence, dict):
        candidates = list(evidence)
    elif isinstance(evidence, list):
        candidates = evidence
    else:
        candidates = []
    result = []
    for item in candidates[:32]:
        if isinstance(item, str) and _SAFE_ID.fullmatch(item):
            result.append(item)
    return sorted(set(result))


def normalize_task_rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_COLLECTION_ITEMS:
        raise ValidationError("tasks collection has an invalid shape")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ValidationError("task row has an invalid shape")
        if not {"id", "title", "assignee", "status"}.issubset(raw) or set(raw) - _RAW_TASK_KEYS:
            raise ValidationError("task row has unexpected or missing fields")
        identifier = _safe_identifier(raw["id"], "task.id")
        if identifier in seen:
            raise ValidationError("tasks contains a duplicate id")
        seen.add(identifier)
        title = _safe_string(raw["title"], "task.title")
        assignee = _safe_identifier(raw["assignee"], "task.assignee")
        status = _safe_identifier(raw["status"], "task.status")
        max_retries = _bounded_integer(raw.get("max_retries", 0), "task.max_retries", 100)
        body = raw.get("body", "")
        if not isinstance(body, str) or len(body.encode("utf-8")) > 64 * 1024:
            raise ValidationError("task.body is invalid")
        marker = _BOOTSTRAP_MARKER.match(body)
        item: dict[str, Any] = {
            "id": identifier,
            "title": title,
            "status": status,
            "assignee": assignee,
            "evidence": [],
            "retry_count": 0,
            "max_retries": max_retries,
        }
        if marker:
            item["bootstrap_key"] = marker.group(1)
        result.append(item)
    return result


def normalize_run_rows(value: object, task_id: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValidationError("runs collection has an invalid shape")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ValidationError("run row has an invalid shape")
        if not {"id", "status", "metadata"}.issubset(raw) or set(raw) - _RAW_RUN_KEYS:
            raise ValidationError("run row has unexpected or missing fields")
        identifier = _safe_identifier(raw["id"], "run.id")
        if identifier in seen:
            raise ValidationError("runs contains a duplicate id")
        seen.add(identifier)
        status = _safe_identifier(raw["status"], "run.status")
        metadata = raw["metadata"]
        if not isinstance(metadata, dict) or len(metadata) > 64:
            raise ValidationError("run metadata has an invalid shape")
        try:
            if len(json.dumps(metadata, ensure_ascii=True).encode()) > 64 * 1024:
                raise ValidationError("run metadata exceeds its bound")
        except (TypeError, ValueError) as exc:
            raise ValidationError("run metadata has an invalid shape") from exc
        result.append(
            {
                "id": identifier,
                "task_id": task_id,
                "status": status,
                "heartbeat_age_seconds": None,
                "retry_count": 0,
                "_evidence": _extract_evidence(metadata),
            }
        )
    return result


def validate_json_collection(value: object, *, kind: str) -> list[dict[str, Any]]:
    allowed = _NORMAL_TASK_KEYS if kind == "tasks" else _NORMAL_RUN_KEYS if kind == "runs" else None
    required = {"id", "status"} if kind == "tasks" else {"id", "task_id", "status"}
    if allowed is None or not isinstance(value, list) or len(value) > MAX_COLLECTION_ITEMS:
        raise ValidationError(f"{kind} collection has an invalid shape")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
            raise ValidationError(f"{kind} item has an invalid shape")
        if not required.issubset(raw) or set(raw) - allowed:
            raise ValidationError(f"{kind} item has unexpected or missing fields")
        identifier = _safe_identifier(raw["id"], f"{kind}.id")
        if identifier in seen:
            raise ValidationError(f"{kind} contains a duplicate id")
        seen.add(identifier)
        result.append(dict(raw))
    return result


def normalize_diagnostics(value: object) -> list[dict[str, str | int]]:
    if not isinstance(value, list) or len(value) > MAX_COLLECTION_ITEMS:
        raise ValidationError("diagnostics collection has an invalid shape")
    result: list[dict[str, str | int]] = []
    expected = {"task_id", "title", "status", "assignee", "diagnostics"}
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValidationError("diagnostic row has an invalid shape")
        task_id = _safe_identifier(raw["task_id"], "diagnostics.task_id")
        title = _safe_string(raw["title"], "diagnostics.title")
        status = _safe_identifier(raw["status"], "diagnostics.status")
        assignee = _safe_identifier(raw["assignee"], "diagnostics.assignee")
        entries = raw["diagnostics"]
        if not isinstance(entries, list) or len(entries) > 64:
            raise ValidationError("diagnostics entries have an invalid shape")
        try:
            if len(json.dumps(entries, ensure_ascii=True).encode()) > 64 * 1024:
                raise ValidationError("diagnostics entries exceed their bound")
        except (TypeError, ValueError) as exc:
            raise ValidationError("diagnostics entries have an invalid shape") from exc
        result.append(
            {
                "task_id": task_id,
                "title_length": len(title),
                "status": status,
                "assignee": assignee,
                "diagnostic_count": len(entries),
            }
        )
    return result


def validate_diagnostic_collection(value: object) -> list[dict[str, str | int]]:
    """Validate the already-redacted diagnostic rows returned by ``HermesAdapter.collect``."""
    if not isinstance(value, list) or len(value) > MAX_COLLECTION_ITEMS:
        raise ValidationError("diagnostics collection has an invalid shape")
    expected = {"task_id", "title_length", "status", "assignee", "diagnostic_count"}
    result: list[dict[str, str | int]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValidationError("diagnostic item has an invalid shape")
        task_id = _safe_identifier(raw["task_id"], "diagnostics.task_id")
        if task_id in seen:
            raise ValidationError("diagnostics contains a duplicate task id")
        seen.add(task_id)
        result.append(
            {
                "task_id": task_id,
                "title_length": _bounded_integer(
                    raw["title_length"], "diagnostics.title_length", 200
                ),
                "status": _safe_identifier(raw["status"], "diagnostics.status"),
                "assignee": _safe_identifier(raw["assignee"], "diagnostics.assignee"),
                "diagnostic_count": _bounded_integer(
                    raw["diagnostic_count"], "diagnostics.diagnostic_count", 64
                ),
            }
        )
    return result


class HermesAdapter:
    def __init__(
        self,
        binary: str = "hermes",
        *,
        timeout_seconds: int = 10,
        max_output_bytes: int = 1024 * 1024,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not binary or len(binary) > 4096 or "\x00" in binary:
            raise ValidationError("Hermes binary is invalid")
        if not 1 <= timeout_seconds <= 45 or not 1024 <= max_output_bytes <= 4 * 1024 * 1024:
            raise ValidationError("adapter bounds are invalid")
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.environment = sanitize_environment(environment)

    def _run(self, arguments: list[str]) -> str:
        if (
            not arguments
            or len(arguments) > 128
            or any(not isinstance(item, str) or len(item) > 4096 for item in arguments)
        ):
            raise AdapterError("Hermes command arguments are invalid")
        argv = [self.binary, *arguments]
        try:
            completed = subprocess.run(  # noqa: S603
                argv,
                shell=False,  # Validated bounded argv; never a shell.  # nosec B603
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdapterError("Hermes command timed out") from exc
        except OSError as exc:
            raise AdapterError("Hermes command is unavailable") from exc
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        if len(stdout.encode()) + len(stderr.encode()) > self.max_output_bytes:
            raise AdapterError("Hermes command exceeded the output limit")
        if completed.returncode != 0:
            raise AdapterError("Hermes command failed")
        return stdout

    def run_text(self, arguments: list[str]) -> str:
        return self._run(arguments)

    def run_json(self, arguments: list[str]) -> object:
        try:
            return json.loads(self._run(arguments))
        except json.JSONDecodeError as exc:
            raise AdapterError("Hermes command returned invalid JSON") from exc

    def preflight_profile(self, name: str) -> None:
        profile = _safe_identifier(name, "profile")
        self.run_text(["profile", "show", profile])

    def list_tasks(self, board: str) -> list[dict[str, Any]]:
        value = self.run_json(
            ["kanban", "--board", _safe_identifier(board, "board"), "list", "--json"]
        )
        return normalize_task_rows(value)

    def show_task(
        self, board: str, task_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        safe_task = _safe_identifier(task_id, "task_id")
        value = self.run_json(
            ["kanban", "--board", _safe_identifier(board, "board"), "show", safe_task, "--json"]
        )
        expected = {"task", "latest_summary", "parents", "children", "comments", "events", "runs"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValidationError("task detail has an invalid shape")
        tasks = normalize_task_rows([value["task"]])
        if tasks[0]["id"] != safe_task:
            raise ValidationError("task detail binding is invalid")
        summary = value["latest_summary"]
        if summary is not None and (
            not isinstance(summary, str) or len(summary.encode()) > 64 * 1024
        ):
            raise ValidationError("task summary exceeds its bound")
        for key in ("parents", "children"):
            identifiers = value[key]
            if not isinstance(identifiers, list) or len(identifiers) > 64:
                raise ValidationError("task relationship list is invalid")
            for identifier in identifiers:
                _safe_identifier(identifier, f"task.{key}")
        for key in ("comments", "events"):
            entries = value[key]
            if not isinstance(entries, list) or len(entries) > 2000:
                raise ValidationError(f"task {key} collection is invalid")
        return tasks[0], normalize_run_rows(value["runs"], safe_task), list(value["parents"])

    def task_parents(self, board: str, task_id: str) -> list[str]:
        _task, _runs, parents = self.show_task(board, task_id)
        return parents

    def task_status(self, board: str, task_id: str) -> str:
        task, _runs, _parents = self.show_task(board, task_id)
        return str(task["status"])

    def diagnostics(self, board: str) -> list[dict[str, str | int]]:
        value = self.run_json(
            ["kanban", "--board", _safe_identifier(board, "board"), "diagnostics", "--json"]
        )
        return normalize_diagnostics(value)

    def collect(self, board: str, task_ids: list[str]) -> dict[str, object]:
        if len(task_ids) > 64 or len(set(task_ids)) != len(task_ids):
            raise ValidationError("bound task identifiers are invalid")
        tasks: list[dict[str, Any]] = []
        all_runs: list[dict[str, Any]] = []
        for task_id in sorted(task_ids):
            task, task_runs, _parents = self.show_task(board, task_id)
            failed = sum(
                run["status"] in {"crashed", "timed_out", "spawn_failed", "gave_up", "failed"}
                for run in task_runs
            )
            task["retry_count"] = failed
            evidence: set[str] = set()
            for run in task_runs:
                evidence.update(run.pop("_evidence", []))
            task["evidence"] = sorted(evidence)
            active = [run for run in task_runs if run["status"] in {"running", "claimed"}]
            if active:
                task["active_run_id"] = active[-1]["id"]
            tasks.append(task)
            all_runs.extend(task_runs)
        return {"tasks": tasks, "runs": all_runs, "diagnostics": self.diagnostics(board)}

    def create_task(
        self,
        board: str,
        title: str,
        assignee: str,
        parents: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        safe_key = _safe_identifier(idempotency_key, "idempotency_key")
        arguments = [
            "kanban",
            "--board",
            _safe_identifier(board, "board"),
            "create",
            _safe_string(title, "title"),
            "--assignee",
            _safe_identifier(assignee, "assignee"),
            "--body",
            f"[cyclops-idempotency:{safe_key}]",
            "--idempotency-key",
            safe_key,
            "--initial-status",
            "blocked",
            "--json",
        ]
        for parent in parents:
            arguments.extend(("--parent", _safe_identifier(parent, "parent")))
        value = self.run_json(arguments)
        if not isinstance(value, dict) or "id" not in value:
            raise AdapterError("Hermes create response is invalid")
        return {"id": _safe_identifier(value["id"], "created task id")}

    def link_tasks(self, board: str, parent_id: str, child_id: str) -> None:
        self.run_text(
            [
                "kanban",
                "--board",
                _safe_identifier(board, "board"),
                "link",
                _safe_identifier(parent_id, "parent_id"),
                _safe_identifier(child_id, "child_id"),
            ]
        )

    def promote_task(self, board: str, task_id: str) -> None:
        value = self.run_json(
            [
                "kanban",
                "--board",
                _safe_identifier(board, "board"),
                "promote",
                _safe_identifier(task_id, "task_id"),
                "--json",
            ]
        )
        if not isinstance(value, dict):
            raise AdapterError("Hermes promote response is invalid")
