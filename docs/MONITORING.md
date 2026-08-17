# Monitoring

## TL;DR

Cyclops provides one read-only health projection for the supervisor, missions, workers and incidents. Missing data is `unknown`, not green. The dashboard has no mutation controls.

## Status projection

The projection contains only bounded fields:

- schema and projection version;
- supervisor mode, heartbeat and tick sequence;
- configured mission ID and manifest hash;
- phase key, state and evidence-key presence;
- task/run IDs, assignee, status and retry counters;
- incident IDs, severity, age in ticks and disposition;
- cost telemetry classification (always `unknown` in v0.1).

It excludes task bodies, comments, summaries, logs, prompts, environment values, filesystem paths and credentials.

## Operator dashboard

The Hermes Dashboard integration displays:

- supervisor reporting state;
- mission progress and next expected phase;
- active worker/run metadata (heartbeat age remains unknown with the v0.1 Hermes CLI adapter);
- reviewer ownership;
- retry and incident counters;
- final evidence checklist;
- cost telemetry; the v0.1 producer emits `unknown` and performs no token accounting.

The panel is read-only. Operational actions remain in Hermes Kanban or systemd.

## Alerts

The initial release does not send messages. Future alerts must be deterministic and bounded:

- no healthy heartbeat spam;
- one actionable incident notification after debounce;
- one dead-letter/budget notification;
- optional recovery notification only for critical incidents.

## Supervising the supervisor

`status.json` is rewritten after each successful tick and on ledger-open failure or manifest-binding mismatch. Adapter/collection failures return a nonzero CLI result and leave the prior projection intact, so consumers must check heartbeat freshness. The bundled panel reports the file unavailable when it cannot read and validate it; an external monitor should enforce any missed-interval policy and detect full host failure.
