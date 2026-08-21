# Changelog

All notable changes to this project are documented here. The project follows Semantic Versioning after public releases.

## [0.3.1] - 2026-08-21

### Fixed

- Stage bootstrap cards as `todo`, revalidate exact card identity and the complete dependency graph,
  then promote only roots so current Hermes cannot expose a parentless card during staging.
- Normalize administrative `run.metadata=null` as an empty bounded mapping while rejecting every
  other non-mapping shape.
- Add strict paused v0.3.0 to v0.3.1 manager-job upgrade validation with update-only rollback.

## [0.3.0] - 2026-08-21

- Add a bounded fixed-argv Hermes cron runs/result adapter with explicit canonical `HERMES_HOME`.
- Import exact manager ACK results before lease reconciliation; current incidents become
  `human_required` with one atomic intent, while cleared results resolve without notification.
- Import exact courier delivery results and mark notifications sent only for `delivered` outcomes;
  failures remain bounded same-packet retries.
- Add exact paused v0.2.2 to v0.3.0 manager-job upgrade validation and no-delete rollback.

## [0.2.2] - 2026-08-20

### Fixed

- Model the canonical Hermes `default` home as the explicit owner-private `<home>/.hermes` root;
  reject the nonexistent `profiles/default` topology, named-profile homes, symbolic links, shared
  modes, wrong ownership, and ambient fallback.
- Set `HERMES_HOME` to that exact validated root for every fresh Hermes version, definition, and
  disposable compatibility-verifier subprocess while preserving same-ID isolation from named
  profiles.

### Security

- Default-profile activation and staging now fail closed before readback or writes when the explicit
  home is missing, noncanonical, aliased, shared, or owned by another user.

## [0.2.1] - 2026-08-20

### Added

- Owner-private, atomic `cyclops-manager-activation/v1` attestation and dry-run-first
  `manager activate` / `manager deactivate` commands.
- One fail-closed validator shared by router authorization and status projection, binding the
  Cyclops release, staged install spec/scripts, Hermes version, canonical profile, exact full job
  definitions and installed-Hermes seam evidence.

### Security

- Every router and status tick recollects the current Hermes version and both exact full definitions
  through bounded `hermes cron show EXACT_JOB_ID --json` calls immediately before validation.
- Missing commands, failures, malformed/oversized output, wrong protocol/ID/state, or drift deny
  before manager lifecycle, lease, or budget mutation; static evidence freshness cannot authorize.
- Activation apply repeats the live readback and requires both exact jobs paused; prompt previews,
  replayed envelopes, desired install specifications, and private Hermes stores cannot authorize.
- Deactivation commits an immediate durable deny without mutating Hermes. Rollback requires that
  deny plus supported proof that both jobs are paused before any downgrade.

## [0.2.0] - 2026-08-19

### Added

- Durable schema-v2 incident generations, wake leases, daily budgets, bounded retry/dead-letter,
  notification intents, and crash-safe v1 migration with a retained private backup.
- Deterministic manager router and no-agent courier CLI boundaries with task-scope denial.
- Strict duplicate-key-rejecting manager ACK protocol, constant-time capability validation,
  typed revalidation, and visible `resolved`/`human_required`/`dead_letter` outcomes.
- Projection v2 and Dashboard-compatible validation for public-safe manager lifecycle metadata.
- Strict machine-readable manager installation/upgrade/rollback spec for the supported
  profile-local `cronjob` tool, with private profile staging and paused-job verification.
- Disposable installed-Hermes checks for `no_mcp` zero-tool resolution, non-task scope, and the
  quiet no-agent courier path.

### Security

- Stable incident IDs exclude mutable severity, state, and fingerprints; recurrence is fenced by
  generation and delayed ACKs cannot close a newer occurrence.
- Lease and budget commits precede every positive wake gate. Clock rollback, task scope, corrupt
  state, cap exhaustion, and unresolved compatibility all fail closed without inference.
- Private manager output reconciliation uses a random 256-bit per-attempt nonce and accepts
  exactly one bounded job/time/incident/generation/attempt/fingerprint/capability match.

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
