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
- service state and token telemetry classification.

It excludes task bodies, comments, summaries, logs, prompts, environment values, filesystem paths and credentials.

## Operator dashboard

The Hermes Dashboard integration displays:

- supervisor reporting state;
- gateway and dashboard service health when supplied;
- mission progress and next expected phase;
- active worker/run/heartbeat age;
- reviewer ownership;
- retry and incident counters;
- final evidence checklist;
- cost telemetry labeled `actual`, `estimated` or `unknown`.

The panel is read-only. Operational actions remain in Hermes Kanban or systemd.

## Alerts

The initial release does not send messages. Future alerts must be deterministic and bounded:

- no healthy heartbeat spam;
- one actionable incident notification after debounce;
- one dead-letter/budget notification;
- optional recovery notification only for critical incidents.

## Supervising the supervisor

`status.json` is rewritten each tick. A consumer marks Cyclops unavailable after three missed intervals. For full host failure, use an external no-LLM health check.
