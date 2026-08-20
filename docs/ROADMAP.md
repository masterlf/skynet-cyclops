# Roadmap

## TL;DR

The project ships observation before actuation. Each later capability requires an exact regression suite and independent review; no broad autonomous repair mode is planned.

## v0.1 — Observe-first MVP

- manifest parser and graph validator;
- idempotent bootstrap dry-run and explicit apply;
- Hermes CLI adapter with sanitized environment;
- pure mission-state derivation;
- supervisor ledger and atomic status projection;
- systemd oneshot/timer templates;
- read-only status CLI and Hermes Dashboard integration;
- public repository privacy/secret scan;
- zero-LLM nominal-path tests.

Version 0.1 has no repair, manager wake, alert delivery, token accounting, publishing, or deployment path. Bootstrap forwards phase runtime, retry, and goal declarations to Hermes; Cyclops does not implement a second runtime or retry engine.

## v0.2 — Durable manager wake-up

- stable typed incident identity and recurrence generations;
- persistence threshold, dedupe, post-gap damper and hard wake budgets;
- private lease/ACK/retry/dead-letter lifecycle;
- fresh non-task-scoped `default` manager through supported Hermes cron/script primitives;
- tool-free diagnose/propose manager with typed revalidation;
- visible `resolved` and `human_required` outcomes;
- no-agent human decision courier;
- dry-run-first installer with verified rollback.

No direct repair authority ships in this increment. See
[Durable manager wake-up](MANAGER_WAKEUP.md).

## v0.3 — Bounded deterministic repairs

Candidate rules, enabled individually after dry-run evidence and independent review:

- idempotent incident comment;
- assign the manifest-declared reviewer to an unclaimed review card;
- recreate a missing bootstrap card with the same idempotency key.

No dispatch, promote, reclaim, complete, unblock, link/unlink, archive, merge, publish or deploy authority.

Manager proposals never expand the repair vocabulary. The supervisor owns exact preconditions,
revalidation, execution and verification for every separately allowlisted rule.

## Upstream retirement

Cyclops tracks Hermes workflow templates, progress watchdogs and lifecycle improvements. When upstream provides equivalent durable reconciliation, duplicate Cyclops functionality should be removed rather than maintained in parallel.

## Stop-loss

Return all actuation to observe-only if any repair requires human undo, token wakes exceed budget, the third new repair rule is requested within one release, or production code exceeds the bounded supervisor scope.
