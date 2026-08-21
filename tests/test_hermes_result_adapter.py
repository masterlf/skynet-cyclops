from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from skynet_cyclops.errors import ValidationError
from skynet_cyclops.hermes_results import HermesCronResultAdapter


def private_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir(mode=0o700)
    return home


def fake_hermes(tmp_path: Path) -> Path:
    path = tmp_path / "hermes-fake"
    path.write_text(
        """#!/usr/bin/env python3
import hashlib,json,os,sys
assert os.environ['HERMES_HOME'].endswith('/.hermes')
job='job-router'
response='{"packet":"synthetic"}'
claimed='2026-08-21T00:00:00Z'
started='2026-08-21T00:00:01Z'
finished='2026-08-21T00:00:02Z'
if sys.argv[1:] == ['cron','runs',job,'--limit','32','--json']:
    print(json.dumps({'protocol':'hermes-cron-runs/v1','job_id':job,'limit':32,'runs':[{'execution_id':'exec-1','job_id':job,'status':'completed','claimed_at':claimed,'started_at':started,'finished_at':finished,'result_available':True}]}))
elif sys.argv[1:] == ['cron','result','exec-1','--json']:
    raw=response.encode()
    print(json.dumps({'protocol':'hermes-cron-result/v1','execution_id':'exec-1','job_id':job,'status':'completed','claimed_at':claimed,'started_at':started,'finished_at':finished,'final_response':response,'final_response_sha256':hashlib.sha256(raw).hexdigest(),'final_response_bytes':len(raw),'delivery_outcome':'suppressed'}))
else:
    raise SystemExit(9)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_adapter_uses_fixed_protocol_argv_explicit_home_and_validates_hash(tmp_path: Path) -> None:
    response = '{"packet":"synthetic"}'
    adapter = HermesCronResultAdapter(
        binary=str(fake_hermes(tmp_path)),
        hermes_home=private_home(tmp_path),
        environment={"FAKE_RESPONSE": response, "PATH": os.environ["PATH"]},
    )
    collection = adapter.collect("job-router", lease_acquired_at=1787270401.5)
    assert collection.complete is True
    assert len(collection.results) == 1
    assert collection.results[0].final_response == response
    assert (
        collection.results[0].final_response_sha256 == hashlib.sha256(response.encode()).hexdigest()
    )
    assert collection.results[0].delivery_outcome == "suppressed"


def test_runs_parser_rejects_duplicate_rows_wrong_order_and_protocol_drift(tmp_path: Path) -> None:
    adapter = HermesCronResultAdapter(
        binary=str(fake_hermes(tmp_path)), hermes_home=private_home(tmp_path)
    )
    base = {
        "protocol": "hermes-cron-runs/v1",
        "job_id": "job-router",
        "limit": 32,
        "runs": [
            {
                "execution_id": "exec-2",
                "job_id": "job-router",
                "status": "completed",
                "claimed_at": "2026-08-21T00:00:02Z",
                "started_at": "2026-08-21T00:00:03Z",
                "finished_at": "2026-08-21T00:00:04Z",
                "result_available": True,
            },
            {
                "execution_id": "exec-1",
                "job_id": "job-router",
                "status": "failed",
                "claimed_at": "2026-08-21T00:00:00Z",
                "started_at": "2026-08-21T00:00:01Z",
                "finished_at": "2026-08-21T00:00:02Z",
                "result_available": False,
            },
        ],
    }
    assert len(adapter.parse_runs(json.dumps(base), expected_job_id="job-router")) == 2
    for poison in (
        {**base, "protocol": "future"},
        {**base, "runs": [base["runs"][0], base["runs"][0]]},
        {**base, "runs": list(reversed(base["runs"]))},
    ):
        with pytest.raises(ValidationError):
            adapter.parse_runs(json.dumps(poison), expected_job_id="job-router")


def test_result_parser_rejects_duplicate_members_hash_size_and_delivery_drift(
    tmp_path: Path,
) -> None:
    adapter = HermesCronResultAdapter(
        binary=str(fake_hermes(tmp_path)), hermes_home=private_home(tmp_path)
    )
    response = "synthetic"
    base = {
        "protocol": "hermes-cron-result/v1",
        "execution_id": "exec-1",
        "job_id": "job-router",
        "status": "completed",
        "claimed_at": "2026-08-21T00:00:00Z",
        "started_at": "2026-08-21T00:00:01Z",
        "finished_at": "2026-08-21T00:00:02Z",
        "final_response": response,
        "final_response_sha256": hashlib.sha256(response.encode()).hexdigest(),
        "final_response_bytes": len(response.encode()),
        "delivery_outcome": "delivered",
    }
    parsed = adapter.parse_result(
        json.dumps(base), expected_job_id="job-router", expected_execution_id="exec-1"
    )
    assert parsed.delivery_outcome == "delivered"
    for key, replacement in (
        ("final_response_sha256", "0" * 64),
        ("final_response_bytes", 99),
        ("delivery_outcome", "unknown"),
    ):
        poison = {**base, key: replacement}
        with pytest.raises(ValidationError):
            adapter.parse_result(
                json.dumps(poison), expected_job_id="job-router", expected_execution_id="exec-1"
            )
    duplicate = json.dumps(base)[:-1] + ',"job_id":"job-router"}'
    with pytest.raises(ValidationError, match="duplicate"):
        adapter.parse_result(
            duplicate, expected_job_id="job-router", expected_execution_id="exec-1"
        )


def test_collection_completeness_requires_oldest_run_to_cover_lease(tmp_path: Path) -> None:
    adapter = HermesCronResultAdapter(
        binary=str(fake_hermes(tmp_path)), hermes_home=private_home(tmp_path)
    )
    run = {
        "execution_id": "exec-1",
        "job_id": "job-router",
        "status": "failed",
        "claimed_at": "2026-08-21T00:00:00Z",
        "started_at": "2026-08-21T00:00:01Z",
        "finished_at": "2026-08-21T00:00:02Z",
        "result_available": False,
    }
    raw = json.dumps(
        {"protocol": "hermes-cron-runs/v1", "job_id": "job-router", "limit": 32, "runs": [run] * 32}
    )
    with pytest.raises(ValidationError):
        adapter.parse_runs(raw, expected_job_id="job-router")


def test_adapter_rejects_unsafe_home_and_invalid_binary(tmp_path: Path) -> None:
    home = private_home(tmp_path)
    home.chmod(0o755)
    with pytest.raises(ValidationError, match="home"):
        HermesCronResultAdapter(binary="hermes", hermes_home=home)
    with pytest.raises(ValidationError, match="binary"):
        HermesCronResultAdapter(binary="bad\x00binary", hermes_home=home)


def test_json_and_timestamp_boundaries_fail_closed(tmp_path: Path) -> None:
    adapter = HermesCronResultAdapter(
        binary=str(fake_hermes(tmp_path)), hermes_home=private_home(tmp_path)
    )
    for raw in ("{} {}", '{"limit":NaN}', "\x00"):
        with pytest.raises(ValidationError):
            adapter.parse_runs(raw, expected_job_id="job-router")
    base_run = {
        "execution_id": "exec-1",
        "job_id": "job-router",
        "status": "completed",
        "claimed_at": "2026-08-21T00:00:00Z",
        "started_at": "2026-08-21T00:00:01Z",
        "finished_at": "2026-08-21T00:00:02Z",
        "result_available": True,
    }
    for key, replacement in (
        ("status", "future"),
        ("job_id", "wrong"),
        ("result_available", False),
        ("claimed_at", "not-time"),
        ("claimed_at", "2026-08-21T00:00:00"),
        ("started_at", "2026-08-20T23:59:59Z"),
        ("finished_at", None),
    ):
        poisoned = {**base_run, key: replacement}
        raw = json.dumps(
            {
                "protocol": "hermes-cron-runs/v1",
                "job_id": "job-router",
                "limit": 32,
                "runs": [poisoned],
            }
        )
        with pytest.raises(ValidationError):
            adapter.parse_runs(raw, expected_job_id="job-router")


def test_result_response_and_identity_boundaries_fail_closed(tmp_path: Path) -> None:
    adapter = HermesCronResultAdapter(
        binary=str(fake_hermes(tmp_path)), hermes_home=private_home(tmp_path)
    )
    response = "x"
    base = {
        "protocol": "hermes-cron-result/v1",
        "execution_id": "exec-1",
        "job_id": "job-router",
        "status": "completed",
        "claimed_at": "2026-08-21T00:00:00Z",
        "started_at": "2026-08-21T00:00:01Z",
        "finished_at": "2026-08-21T00:00:02Z",
        "final_response": response,
        "final_response_sha256": hashlib.sha256(response.encode()).hexdigest(),
        "final_response_bytes": 1,
    }
    for key, replacement in (
        ("protocol", "future"),
        ("execution_id", "other"),
        ("job_id", "other"),
        ("status", "failed"),
        ("final_response", ""),
        ("final_response_bytes", True),
        ("started_at", "2026-08-20T23:59:59Z"),
    ):
        poisoned = {**base, key: replacement}
        with pytest.raises(ValidationError):
            adapter.parse_result(
                json.dumps(poisoned),
                expected_job_id="job-router",
                expected_execution_id="exec-1",
            )
    with pytest.raises(ValidationError, match="job id"):
        adapter.parse_result("{}", expected_job_id="bad id", expected_execution_id="exec-1")


def test_adapter_rejects_failed_command_and_lifecycle_mismatch(tmp_path: Path) -> None:
    home = private_home(tmp_path)
    failed = tmp_path / "failed-hermes"
    failed.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    failed.chmod(0o700)
    with pytest.raises(ValidationError, match="command failed"):
        HermesCronResultAdapter(binary=str(failed), hermes_home=home).collect("job-router")

    missing = tmp_path / "missing"
    with pytest.raises(ValidationError, match="unavailable"):
        HermesCronResultAdapter(binary=str(missing), hermes_home=home)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("import time; time.sleep(2)", "timed out"),
        ("import sys; sys.stdout.write('x' * 80000)", "output bound"),
        ("import sys; sys.stdout.buffer.write(b'\\xff')", "UTF-8"),
    ],
)
def test_subprocess_stream_limits_are_enforced_while_reading(
    tmp_path: Path, body: str, match: str
) -> None:
    executable = tmp_path / "bounded-hermes"
    executable.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8")
    executable.chmod(0o700)
    adapter = HermesCronResultAdapter(
        binary=str(executable),
        hermes_home=private_home(tmp_path),
        timeout_seconds=1,
    )
    with pytest.raises(ValidationError, match=match):
        adapter.collect("job-router")


def test_constructor_and_parser_schema_bounds_are_closed(tmp_path: Path) -> None:
    home = private_home(tmp_path)
    executable = fake_hermes(tmp_path)
    with pytest.raises(ValidationError, match="bounds"):
        HermesCronResultAdapter(binary=str(executable), hermes_home=home, timeout_seconds=11)
    unsafe = tmp_path / "unsafe"
    unsafe.write_text("not executable", encoding="utf-8")
    with pytest.raises(ValidationError, match="unsafe"):
        HermesCronResultAdapter(binary=str(unsafe), hermes_home=home)
    adapter = HermesCronResultAdapter(binary=str(executable), hermes_home=home)
    for raw in (
        "[]",
        json.dumps(
            {
                "protocol": "hermes-cron-runs/v1",
                "job_id": "job-router",
                "limit": 32,
                "runs": ["bad"],
            }
        ),
    ):
        with pytest.raises(ValidationError):
            adapter.parse_runs(raw, expected_job_id="job-router")


def test_remaining_malformed_deadline_and_home_boundaries(tmp_path: Path) -> None:
    home = private_home(tmp_path)
    executable = fake_hermes(tmp_path)
    adapter = HermesCronResultAdapter(binary=str(executable), hermes_home=home)
    with pytest.raises(ValidationError, match="malformed"):
        adapter.parse_runs("{", expected_job_id="job-router")
    malformed_time = {
        "protocol": "hermes-cron-runs/v1",
        "job_id": "job-router",
        "limit": 32,
        "runs": [
            {
                "execution_id": "exec-none",
                "job_id": "job-router",
                "status": "completed",
                "claimed_at": None,
                "started_at": None,
                "finished_at": None,
                "result_available": True,
            }
        ],
    }
    with pytest.raises(ValidationError, match="claimed_at"):
        adapter.parse_runs(json.dumps(malformed_time), expected_job_id="job-router")
    with pytest.raises(ValidationError, match="deadline"):
        adapter._run((), deadline=time.monotonic() - 1)
    with pytest.raises(ValidationError, match="unavailable"):
        HermesCronResultAdapter(binary=str(executable), hermes_home=tmp_path / "absent" / ".hermes")
