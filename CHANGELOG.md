# Changelog

All notable changes to this project are documented here. The project follows Semantic Versioning after public releases.

## [Unreleased]

### Added

- v0.1 observe-first candidate with strict manifest parsing and graph validation.
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

### Changed

- Documentation now distinguishes implemented v0.1 behavior from deferred repairs, model wakes, alerts, token accounting, publishing, and deployment.
- Dashboard metadata now matches the Hermes host discovery contract and declares an explicit read-only tab.
