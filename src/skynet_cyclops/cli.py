"""Skynet-Cyclops command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import IntEnum
from pathlib import Path

from .activation import (
    ActivationInputs,
    activate_manager,
    activation_verdict,
    deactivate_manager,
    load_activation_inputs,
)
from .adapter import HermesAdapter, ReadOnlyCollector
from .bootstrap import apply_bootstrap, plan_bootstrap
from .config import Config, default_config_path, default_ledger_path, load_config
from .errors import AdapterError, CyclopsError, LedgerError, ProjectionError, ValidationError
from .hermes_results import HermesCronResultAdapter
from .ledger import Ledger
from .manager import (
    IncidentObservation,
    manager_router_gate,
    manager_scope_denied,
    notification_courier,
    stable_incident_id,
)
from .manager_install import build_cron_install_spec, stage_cron_install
from .manifest import canonical_manifest_hash, load_manifest
from .projection import read_projection
from .tick import incident_observations, run_tick


class ExitCode(IntEnum):
    OK = 0
    INVALID_INPUT = 2
    EXTERNAL_FAILURE = 3
    STATE_UNAVAILABLE = 4
    INTERNAL_ERROR = 70


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cyclops")
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("manifest")
    manifest_commands = manifest.add_subparsers(dest="manifest_command", required=True)
    validate = manifest_commands.add_parser("validate")
    validate.add_argument("path")
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("path")
    posture = bootstrap.add_mutually_exclusive_group()
    posture.add_argument("--dry-run", action="store_true")
    posture.add_argument("--apply", action="store_true")
    bootstrap.add_argument("--config", default=str(default_config_path()))
    tick = commands.add_parser("tick")
    tick.add_argument("--config", default=str(default_config_path()))
    tick.add_argument("--json", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("--config", default=str(default_config_path()))
    status.add_argument("--json", action="store_true")
    manager = commands.add_parser("manager")
    manager_commands = manager.add_subparsers(dest="manager_command", required=True)
    router = manager_commands.add_parser("router")
    router.add_argument("--config", default=str(default_config_path()))
    courier = manager_commands.add_parser("courier")
    courier.add_argument("--config", default=str(default_config_path()))
    for command in (router, courier):
        command.add_argument("--evidence")
        command.add_argument("--hermes-home")
    activate = manager_commands.add_parser("activate")
    activate.add_argument("--config", default=str(default_config_path()))
    activate.add_argument("--evidence", required=True)
    activate.add_argument("--hermes-home", required=True)
    activate.add_argument("--apply", action="store_true")
    deactivate = manager_commands.add_parser("deactivate")
    deactivate.add_argument("--config", default=str(default_config_path()))
    deactivate.add_argument("--apply", action="store_true")
    install = manager_commands.add_parser("install")
    install.add_argument("--profile", default="default")
    install.add_argument("--home-delivery", required=True)
    install.add_argument("--operation", choices=("install", "upgrade"), default="install")
    install.add_argument("--snapshot")
    install.add_argument("--previous-spec")
    install.add_argument("--hermes-home")
    install.add_argument("--apply", action="store_true")
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _activation_paths(
    config: Config, evidence: str | None, hermes_home: str | None
) -> tuple[Path, Path, Path]:
    ledger_path = config.ledger_path
    activation_path = ledger_path.parent / "manager-activation.json"
    evidence_path = (
        Path(evidence) if evidence else ledger_path.parent / "manager-current-evidence.json"
    )
    profile_home = Path(hermes_home) if hermes_home is not None else config.hermes_home
    return activation_path, evidence_path, profile_home


def _read_install_json(path: str | None, *, required: bool) -> object:
    if path is None:
        if required:
            raise ValidationError("manager install requires a tool-visible snapshot")
        return None
    candidate = Path(path)
    try:
        info = candidate.lstat()
        if candidate.is_symlink() or not candidate.is_file() or info.st_size > 256 * 1024:
            raise ValidationError("manager install input is unsafe")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ValidationError("manager install input ownership is unsafe")
        return json.loads(candidate.read_text(encoding="utf-8"))
    except ValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("manager install input is unavailable") from exc


def _bootstrap(args: argparse.Namespace) -> ExitCode:
    manifest = load_manifest(args.path)
    plan = plan_bootstrap(manifest)
    if not args.apply:
        _json({"mode": "dry-run", "plan": [item.to_dict() for item in plan]})
        return ExitCode.OK
    config = load_config(args.config)
    adapter = HermesAdapter(binary=config.hermes_binary)
    ledger_path = config.ledger_path if config.ledger_path else default_ledger_path()
    try:
        ledger = Ledger.open(ledger_path)
    except LedgerError as exc:
        if "missing" not in str(exc):
            raise
        ledger = Ledger.create(ledger_path)
    with ledger:
        bindings = apply_bootstrap(manifest, adapter, ledger)
    _json({"mode": "applied", "bindings": bindings})
    return ExitCode.OK


def _execute(args: argparse.Namespace) -> ExitCode:
    if args.command == "manifest":
        manifest = load_manifest(args.path)
        _json({"valid": True, "manifest_sha256": canonical_manifest_hash(manifest)})
        return ExitCode.OK
    if args.command == "bootstrap":
        return _bootstrap(args)
    if args.command == "manager":
        if args.manager_command == "install":
            snapshot_value = _read_install_json(
                args.snapshot, required=args.apply or args.operation == "upgrade"
            )
            if snapshot_value is None:
                snapshot_value = []
            if not isinstance(snapshot_value, list):
                raise ValidationError("manager install snapshot must be a job list")
            previous = _read_install_json(args.previous_spec, required=args.operation == "upgrade")
            if previous is not None and not isinstance(previous, dict):
                raise ValidationError("manager previous spec must be an object")
            spec = build_cron_install_spec(
                profile=args.profile,
                home_delivery=args.home_delivery,
                operation=args.operation,
                visible_jobs=snapshot_value,
                previous_spec=previous,
            )
            if args.apply:
                if not args.hermes_home:
                    raise ValidationError("manager install apply requires --hermes-home")
                stage_cron_install(spec, Path(args.hermes_home))
            _json(spec)
            return ExitCode.OK
        config = load_config(args.config)
        activation_path, evidence_path, profile_home = _activation_paths(
            config,
            getattr(args, "evidence", None),
            getattr(args, "hermes_home", None),
        )
        if args.manager_command == "activate":

            def collect_activation_inputs() -> ActivationInputs:
                return load_activation_inputs(
                    activation_path=activation_path,
                    hermes_home=profile_home,
                    evidence_path=evidence_path,
                    hermes_binary=config.hermes_binary,
                )

            inputs = collect_activation_inputs()
            _json(
                activate_manager(
                    inputs,
                    now=_utc_now(),
                    apply=args.apply,
                    environment=os.environ,
                    refresh=collect_activation_inputs,
                )
            )
            return ExitCode.OK
        if args.manager_command == "deactivate":
            _json(
                deactivate_manager(
                    activation_path, now=_utc_now(), apply=args.apply, environment=os.environ
                )
            )
            return ExitCode.OK

        with Ledger.open(config.ledger_path) as ledger:
            if manager_scope_denied(os.environ):
                if args.manager_command == "router":
                    _json({"wakeAgent": False})
                return ExitCode.OK
            inputs = load_activation_inputs(
                activation_path=activation_path,
                hermes_home=profile_home,
                evidence_path=evidence_path,
                hermes_binary=config.hermes_binary,
            )
            verdict = activation_verdict(inputs)
            role = "router" if args.manager_command == "router" else "courier"
            job_id = str(inputs.jobs[role]["job_id"])
            result_adapter = HermesCronResultAdapter(
                binary=config.hermes_binary,
                hermes_home=profile_home,
                environment=os.environ,
            )

            def current_incident(stored: dict[str, object]) -> IncidentObservation | None:
                manifest = load_manifest(config.manifest_path)
                bindings = ledger.bindings(str(stored["mission_id"]))
                raw = ReadOnlyCollector(HermesAdapter(binary=config.hermes_binary)).collect(
                    manifest.mission.board, list(bindings.values())
                )
                matches = [
                    item
                    for item in incident_observations(manifest, bindings, raw)
                    if stable_incident_id(item) == stored["incident_id"]
                ]
                if len(matches) > 1:
                    raise ValidationError("manager incident revalidation is ambiguous")
                return None if not matches else matches[0]

            if args.manager_command == "router":
                _json(
                    manager_router_gate(
                        ledger,
                        now=time.time(),
                        environment=os.environ,
                        activation_check=lambda: verdict,
                        router_job_id=job_id,
                        result_collection=lambda exact_job, acquired: result_adapter.collect(
                            exact_job, lease_acquired_at=acquired
                        ),
                        current_incident=current_incident,
                    )
                )
            else:
                output = notification_courier(
                    ledger,
                    now=time.time(),
                    environment=os.environ,
                    activation_check=lambda: verdict,
                    courier_job_id=job_id,
                    result_collection=lambda exact_job, acquired: result_adapter.collect(
                        exact_job, lease_acquired_at=acquired
                    ),
                )
                if output:
                    print(output)
        return ExitCode.OK
    config = load_config(args.config)
    if args.command == "tick":
        manifest = load_manifest(config.manifest_path)
        activation_path, evidence_path, profile_home = _activation_paths(config, None, None)
        payload = run_tick(
            manifest,
            config.ledger_path,
            config.status_path,
            ReadOnlyCollector(HermesAdapter(binary=config.hermes_binary)),
            debounce_ticks=config.incident_debounce_ticks,
            activation_check=lambda: activation_verdict(
                load_activation_inputs(
                    activation_path=activation_path,
                    hermes_home=profile_home,
                    evidence_path=evidence_path,
                    hermes_binary=config.hermes_binary,
                )
            ),
        )
        if args.json:
            _json(payload)
        return ExitCode.OK
    if args.command == "status":
        payload = read_projection(config.status_path)
        if args.json:
            _json(payload)
        else:
            supervisor = payload["supervisor"]
            print(
                f"mode={supervisor['mode']} state={supervisor['state']} "
                f"tick={supervisor['tick_seq']} incidents={len(payload['incidents'])}"
            )
        return ExitCode.OK
    raise ValidationError("command is invalid")


def main(argv: Sequence[str] | None = None) -> ExitCode:
    try:
        args = _parser().parse_args(argv)
        return _execute(args)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ExitCode.INVALID_INPUT
    except AdapterError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ExitCode.EXTERNAL_FAILURE
    except (LedgerError, ProjectionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ExitCode.STATE_UNAVAILABLE
    except CyclopsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return ExitCode.INTERNAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
