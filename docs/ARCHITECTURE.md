# Architecture

## TL;DR

Cyclops is a deterministic observer around Hermes Kanban. It keeps no mutable phase pointer, never duplicates worker state, and does not dispatch work. A manifest declares intent; every tick derives progress from Kanban, updates a small incident ledger, and atomically publishes a read-only status projection.

## Components

```text
release manifest ──► bootstrap ──► Hermes Kanban (canonical)
       │                                │
       └────────────► supervisor ◄──────┘
                           │
                incident ledger + status.json
                           │
                   read-only dashboard
```

### Manifest

The manifest is immutable runtime intent: mission ID, phases, dependencies, roles, evidence requirements, tick deadlines and budgets. It does not contain commands or runtime status.

### Bootstrap

Bootstrap validates the complete graph before creating cards. Creation uses deterministic idempotency keys and supported Hermes CLI calls. `--dry-run` is the default posture; `--apply` is explicit.

### Kanban

Hermes Kanban owns:

- tasks, assignees and dependencies;
- active claims, PIDs and heartbeats;
- task runs and retry counters;
- lifecycle transitions and audit events.

Cyclops never writes directly to the Kanban database.

### Supervisor

A short systemd oneshot executes on a timer. It:

1. reads a manifest and approved bindings;
2. collects supported Kanban JSON surfaces;
3. derives mission state as a pure function;
4. records stable incidents with tick-based debounce;
5. writes a minimal, redacted status projection;
6. exits.

The initial release is observe-only. It has no dispatcher, repair, model or publishing path.

### Ledger

The private ledger stores only supervisor-specific data:

- schema/mode/heartbeat/tick sequence;
- manifest hash and phase-to-task bindings;
- crash-consistent action intents;
- incidents and future wake budgets.

Deletion or corruption forces observe-only. The ledger is not a backup of Kanban.

### Dashboard

The optional Hermes Dashboard integration exposes one authenticated read-only endpoint and a read-only panel. It treats status JSON as hostile input and renders text through the host component model.

## State derivation

A phase is derived from its bound task and dependencies:

- `pending`: dependencies incomplete;
- `ready`: task ready or scheduled;
- `running`: active run;
- `review`: reviewer phase active;
- `blocked`: external or technical gate;
- `failed`: terminal unsuccessful condition;
- `done`: completed with required evidence metadata;
- `unknown`: malformed, missing or contradictory input.

Unknown fails closed. Mission success requires the final phase and final evidence contract.

## Authority boundaries

Cyclops may observe all configured mission metadata. In observe-only mode it performs no Kanban mutation and no model call. Future actuation must be introduced rule-by-rule with an immutable command allowlist, exact preconditions, write-ahead intent and independent review.

## Upstream compatibility

Cyclops uses supported CLI/API surfaces and an adapter boundary. Hermes workflow-template fields are reserved for future upstream use and are not written directly. When upstream provides equivalent deterministic routing, Cyclops rules can retire without migrating canonical state.
