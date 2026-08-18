"""Skynet-Cyclops command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from enum import IntEnum

from .adapter import HermesAdapter, ReadOnlyCollector
from .bootstrap import apply_bootstrap, plan_bootstrap
from .config import default_config_path, default_ledger_path, load_config
from .errors import AdapterError, CyclopsError, LedgerError, ProjectionError, ValidationError
from .ledger import Ledger
from .manifest import canonical_manifest_hash, load_manifest
from .projection import read_projection
from .tick import run_tick


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
    return parser


def _json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


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
    config = load_config(args.config)
    if args.command == "tick":
        manifest = load_manifest(config.manifest_path)
        payload = run_tick(
            manifest,
            config.ledger_path,
            config.status_path,
            ReadOnlyCollector(HermesAdapter(binary=config.hermes_binary)),
            debounce_ticks=config.incident_debounce_ticks,
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
