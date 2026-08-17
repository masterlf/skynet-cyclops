# Skynet-Cyclops

> Deterministic, token-efficient supervision for Hermes Kanban workflows.

## TL;DR

Skynet-Cyclops watches durable Hermes Kanban missions without becoming another dispatcher. Kanban remains authoritative; Cyclops derives health from supported CLI/API output, records bounded incidents, and publishes a read-only status projection. Healthy ticks use **zero LLM calls**.

The v0.1 candidate is pre-1.0 and observe-first. Automatic repairs and manager wakes are not implemented.

## Why

Long-running agent workflows can fail silently even when individual workers have heartbeats and retries: a review may never start, a ready task may remain unclaimed, or a workflow may have no actionable successor. Cyclops adds a deterministic reconciliation and monitoring layer without duplicating Hermes task, run, lease, retry, or audit state.

## Core principles

- **Kanban is canonical.** The manifest declares intent, not mutable progress.
- **Zero nominal inference.** Monitoring and no-change checks never call a model.
- **No second dispatcher.** Runtime ticks never mutate Kanban. Explicit `bootstrap --apply` creates,
  links, verifies, and promotes only dependency roots; Cyclops never dispatches, reclaims, publishes,
  or deploys.
- **Observe before actuation.** Monitoring ships before any repair capability.
- **Bounded authority.** Optional future manager runs are propose-only and tool-free.
- **Public-safe by default.** Examples are synthetic and status output excludes prose, logs, paths, secrets, and PII.

## v0.1 operator experience

```bash
cyclops manifest validate examples/release-observe.yaml
cyclops bootstrap examples/release-observe.yaml --dry-run
cyclops bootstrap examples/release-observe.yaml --apply --config examples/config.yaml
cyclops tick --config examples/config.yaml --json
cyclops status --config examples/config.yaml --json
```

See:

- [Architecture](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Operations](docs/OPERATIONS.md)
- [Monitoring](docs/MONITORING.md)
- [Privacy](docs/PRIVACY.md)
- [Cost model](docs/COST_MODEL.md)
- [Roadmap](docs/ROADMAP.md)

## Status

The v0.1 candidate implements strict manifest validation, explicit idempotent bootstrap, observe-only ticks, a private SQLite ledger, an atomic redacted projection, and an optional read-only dashboard plugin. Bootstrap is the only Kanban-mutating path and requires `--apply`; `tick` and `status` never mutate Kanban. No production support claim is made.

## License

Apache-2.0.
