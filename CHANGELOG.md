# Changelog

All notable changes to this project are documented here. The project follows Semantic Versioning after public releases.

## [Unreleased]

### Added

- Durable schema-v2 incident generations, wake leases, daily budgets, bounded retry/dead-letter,
  notification intents, and crash-safe v1 migration with a retained private backup.
- Deterministic manager router and no-agent courier CLI boundaries with task-scope denial.
- Strict duplicate-key-rejecting manager ACK protocol, constant-time capability validation,
  typed revalidation, and visible `resolved`/`human_required`/`dead_letter` outcomes.
- Projection v2 and Dashboard-compatible validation for public-safe manager lifecycle metadata.
- Dry-run manager installation plan with paused jobs, zero-tool seam checks, and hash-fenced
  rollback instructions. Live apply and automatic repair remain disabled.

### Security

- Stable incident IDs exclude mutable severity, state, and fingerprints; recurrence is fenced by
  generation and delayed ACKs cannot close a newer occurrence.
- Lease and budget commits precede every positive wake gate. Clock rollback, task scope, corrupt
  state, cap exhaustion, and unresolved compatibility all fail closed without inference.

## [0.1.0] - 2026-08-17

### Added

- v0.1 observe-first release with strict manifest parsing and graph validation.
- Dry-run-default, explicit-apply, idempotent Kanban bootstrap with crash reconciliation.
- Sanitized shell-free Hermes CLI adapter and deterministic mission-state derivation.
- Private SQLite ledger, debounced incidents, and atomic redacted status projection.
- Read-only CLI status and optional Hermes Dashboard tab/API integration.
- Hardened user-service templates, dry-run installer helper, and public repository scanner.
- Python 3.11/3.12 CI gates for lint, format, typing, branch coverage, security audit, dependency audit, public scan, and package build.
- Dependabot configuration plus public-safe pull request and issue templates.

### Security

- Fail-closed missing bindings no longer rely on an optimizable production assertion.
- Bandit suppressions are limited to the validated `shell=False` subprocess boundary and the custom no-alias `SafeLoader` call.
- Development CI upgrades pip to a non-vulnerable release and requires pytest 9.0.3 or newer.
- Bootstrap rejects ambiguous or mismatched preseeded cards and serializes concurrent apply runs.
- State derivation is dependency-ordered; failed collections cannot advance heartbeat, debounce, or gap state.
- Evidence requires truthy metadata from the latest successful terminal run.
- Producer and Dashboard share one deeply validated, permission-hardened projection contract.

### Changed

- Documentation now distinguishes implemented v0.1 behavior from deferred repairs, model wakes, alerts, token accounting, publishing, and deployment.
- Dashboard metadata now matches the Hermes host discovery contract and declares an explicit read-only tab.
