# Skynet-Cyclops

> Deterministic, token-efficient supervision for Hermes Kanban workflows.

## TL;DR

Skynet-Cyclops watches durable Hermes Kanban missions without becoming another dispatcher. Kanban remains authoritative; Cyclops derives health from supported CLI/API output, records bounded incidents, and publishes a read-only status projection. Healthy ticks use **zero LLM calls**.

Version 0.2.0 adds a disabled-by-default, diagnose/propose-only manager
wake path. It persists stable incident generations, leases at most one fresh `default`-profile
cron session, validates a fenced JSON ACK, revalidates typed state, and exposes `resolved`,
`human_required`, or `dead_letter`. Healthy and ineligible router/courier ticks remain model-free.
Cyclops can stage private profile artifacts and emit a strict spec for the supported profile-local
`cronjob` tool; it never writes Hermes cron stores or enables a job. No automatic repair authority
is granted.

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

## Operator experience

```bash
cyclops manifest validate examples/release-observe.yaml
cyclops bootstrap examples/release-observe.yaml --dry-run
cyclops bootstrap examples/release-observe.yaml --apply --config examples/config.yaml
cyclops tick --config examples/config.yaml --json
cyclops status --config examples/config.yaml --json
cyclops manager install --profile default --home-delivery telegram
```

See:

- [Architecture](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Durable manager wake-up design](docs/MANAGER_WAKEUP.md)
- [Operations](docs/OPERATIONS.md)
- [Monitoring](docs/MONITORING.md)
- [Privacy](docs/PRIVACY.md)
- [Cost model](docs/COST_MODEL.md)
- [Roadmap](docs/ROADMAP.md)

## Status

Version 0.2.0 adds the durable manager lifecycle and a staged, tool-mediated installation contract
to the v0.1 observe-first baseline. Manager jobs remain paused until an operator executes the
disposable compatibility checks and explicitly resumes them. No production support claim is made.

## License

Apache-2.0.
