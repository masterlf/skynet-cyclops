# Roadmap

## TL;DR

The project ships observation before actuation. Each later capability requires an exact regression suite and independent review; no broad autonomous repair mode is planned.

## v0.1 — Observe-first MVP candidate

- manifest parser and graph validator;
- idempotent bootstrap dry-run and explicit apply;
- Hermes CLI adapter with sanitized environment;
- pure mission-state derivation;
- supervisor ledger and atomic status projection;
- systemd oneshot/timer templates;
- read-only status CLI and Hermes Dashboard integration;
- public repository privacy/secret scan;
- zero-LLM nominal-path tests.

The candidate has no repair, manager wake, alert delivery, token accounting, publishing, or deployment path. Bootstrap forwards phase runtime, retry, and goal declarations to Hermes; Cyclops does not implement a second runtime or retry engine.

## v0.2 — Bounded deterministic repairs

Candidate rules, enabled individually after dry-run evidence:

- idempotent incident comment;
- assign the manifest-declared reviewer to an unclaimed review card;
- recreate a missing bootstrap card with the same idempotency key.

No dispatch, promote, reclaim, complete, unblock, link/unlink, archive, merge, publish or deploy authority.

## v0.3 — Tool-free manager proposals

- self-contained typed incident bundle;
- tool-free propose-only manager;
- closed proposal vocabulary;
- hard wake budgets and dead-letter;
- supervisor revalidation and allowlisted execution.

## Upstream retirement

Cyclops tracks Hermes workflow templates, progress watchdogs and lifecycle improvements. When upstream provides equivalent durable reconciliation, duplicate Cyclops functionality should be removed rather than maintained in parallel.

## Stop-loss

Return all actuation to observe-only if any repair requires human undo, token wakes exceed budget, the third new repair rule is requested within one release, or production code exceeds the bounded supervisor scope.
