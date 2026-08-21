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

The manifest is immutable runtime intent: mission ID, phases, dependencies, roles, evidence requirements, tick cadence, gap damper, and phase runtime/retry/goal declarations. Bootstrap forwards those declarations to Hermes card creation; Hermes enforces runtime and retry behavior and remains the sole retry authority. The manifest does not contain commands or runtime status.

### Bootstrap

Bootstrap validates the complete graph before creating cards. Creation uses deterministic idempotency keys, a bounded machine-readable card contract, and supported Hermes CLI calls. Apply holds a private cross-process exclusive lock, rejects ambiguous or mismatched cards, creates every card as `todo`, verifies every card identity and the complete dependency graph, and then promotes only roots. `--dry-run` is the default posture; `--apply` is explicit.

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

### Successor manager wake-up

The v0.2 design adds an explicitly enabled wake path without changing Kanban authority. A
profile-owned Hermes cron router runs a deterministic pre-check script. Quiet, transient,
post-gap, duplicate and budget-exhausted ticks return `wakeAgent=false` before Hermes constructs
an agent. An eligible incident is durably leased before `wakeAgent=true` starts one fresh
non-dispatcher-owned `default` cron session; it is never represented as a Kanban inbox task.

The first manager is tool-free and diagnose/propose only. Its exact JSON ACK is treated as
untrusted data, fenced to the incident generation and lease, and followed by a fresh authoritative
Kanban revalidation. The result is a visible `resolved`, `human_required` or `dead_letter`
disposition. A separate no-agent courier emits only bounded `human_required` or manager-failure
packets to the configured home channel. See [Durable manager wake-up](MANAGER_WAKEUP.md).

### Ledger

The private ledger stores only supervisor-specific data:

- schema/mode/heartbeat/tick sequence;
- manifest hash and phase-to-task bindings;
- bootstrap idempotency intents;
- debounced incidents.

The v0.2 schema extends incidents with stable typed identity, recurrence generations, wake
attempt leases, ACK fences, bounded wake budgets and notification intents. These remain
supervisor metadata: task status, run claims, PIDs, heartbeats and worker retry counters stay in
Kanban. Losing or corrupting the ledger disables wakes rather than resetting their budgets.

Deletion, corruption, an unsafe file type/owner/mode, or a manifest-hash mismatch produces a critical fail-closed projection and skips collection. The ledger is not a backup of Kanban.

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
- `done`: completed with required truthy evidence metadata from the latest successful terminal run;
- `unknown`: malformed, missing or contradictory input.

Unknown fails closed. Mission success requires the final phase and final evidence contract.

## Authority boundaries

Cyclops may observe all configured mission metadata. In observe-only mode it performs no Kanban mutation and no model call. Future actuation must be introduced rule-by-rule with an immutable command allowlist, exact preconditions, write-ahead intent and independent review.

The v0.2 manager wake is not repair authority. The manager has no tools and its closed proposal
vocabulary cannot directly mutate Kanban, a repository, delivery, publication or deployment.
Deterministic repairs remain disabled until each exact rule is independently implemented, tested,
reviewed and enabled.

## Upstream compatibility

Cyclops uses supported CLI/API surfaces and an adapter boundary. Hermes workflow-template fields are reserved for future upstream use and are not written directly. When upstream provides equivalent deterministic routing, Cyclops rules can retire without migrating canonical state.
