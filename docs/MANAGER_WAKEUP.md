# Durable manager wake-up

## TL;DR

Cyclops v0.2 may wake the canonical `default` Hermes profile only after a typed incident persists, is deduplicated, passes a post-gap damper, and fits strict per-incident and global budgets. The nominal path is a profile-owned Hermes cron job whose deterministic pre-run script returns `wakeAgent=false`; therefore healthy and quiet ticks construct no agent and make no LLM call. An actionable incident is leased in Cyclops' private ledger before the script returns `wakeAgent=true`, causing one fresh cron session that Hermes marks as non-dispatcher-owned rather than a Kanban worker.

The first increment is diagnose/propose only. The manager has no tools and returns one schema-validated ACK. Cyclops then revalidates authoritative Hermes state. A stale incident resolves without action; an unresolved incident becomes `human_required`; missing or invalid ACKs receive one bounded retry and then become `dead_letter`. No manager wake can complete, unblock, dispatch, reclaim, publish, deploy, edit a repository, or otherwise mutate Kanban.

This document is the implementation contract for issue #4. It defines design decisions, invariants, state, interfaces, installation, tests, risks, and non-goals. It does not enable a live job or grant repair authority.

## Facts and compatibility baseline

The design was checked against the public Hermes documentation and the installed Hermes Agent v0.20.3 contracts:

- Cron is profile-scoped: a job executes with the owning profile's home, configuration, credentials, model, and skills.
- A cron pre-run script executes before agent construction. Its final `{"wakeAgent": false}` line suppresses the agent run.
- Cron creates a fresh session for every agent run.
- Cron explicitly enters a non-dispatcher-owned context, even when invoked from a Kanban worker, to prevent inherited `HERMES_KANBAN_TASK` from adding task-scoped tools or lifecycle instructions.
- `enabled_toolsets` can narrow a cron agent's tools. In the checked scheduler, a literal empty
  list falls back to the cron platform defaults; the non-empty `no_mcp` sentinel resolves to an
  empty native/MCP tool list. The installer must verify the resolved inventory rather than infer it
  from the stored job field.
- Cron saves each run output durably before delivery and keeps durable execution records.
- Cron scripts must live under the owning profile's `$HERMES_HOME/scripts/`, execute with fixed interpreter selection, and receive a sanitized subprocess environment.
- Kanban remains the durable authority for task, run, claim, PID, heartbeat, retry, dependency, and transition state. Dispatcher-spawned workers remain task-scoped for destructive lifecycle operations.

These are external compatibility assumptions, not Cyclops-owned guarantees. The installer MUST run the seam checks in [Compatibility and fail-closed behavior](#compatibility-and-fail-closed-behavior) and refuse `--apply` if any required behavior is absent. Unsupported Hermes versions remain observe-only.

## Goals

1. Preserve zero LLM calls for healthy, transient, post-gap, budget-exhausted, and duplicate conditions.
2. Wake exactly one fresh, non-task-scoped `default` manager run per eligible attempt.
3. Persist incident lifecycle independently of Kanban without duplicating Kanban runtime state.
4. Survive process death, host restart, duplicate timers, lost responses, and stale leases without wake amplification.
5. Expose terminal `resolved`, `human_required`, and `dead_letter` outcomes in the status projection and Dashboard.
6. Deliver one compact decision packet only for `human_required` or manager failure.
7. Keep the first increment diagnose/propose only; deterministic repair authority remains separately reviewed and allowlisted rule by rule.

## Non-goals

Cyclops v0.2 is not:

- a second dispatcher, worker retry engine, or general message broker;
- an inbox/task assigned to `default`;
- a replacement for Hermes cron fire claims or Kanban task claims;
- an exactly-once distributed delivery system;
- a broad autonomous repair agent;
- a publisher, deployer, release authority, repository editor, or credential broker;
- a multi-host consensus or disaster-recovery system.

## Hard invariants

1. **Kanban is canonical.** Cyclops stores references and observations, never a competing copy of task/run lifecycle.
2. **No default inbox card.** The manager wake is a cron-owned fresh session, not a Kanban task.
3. **No task scope.** Router execution carrying Kanban task/run/workspace or delegated-child
   ownership markers is rejected before leasing. A normal scheduler-owned wake must also prove
   non-dispatcher-owned cron context and receive no Kanban worker lifecycle authority.
4. **No nominal inference.** Healthy or ineligible script runs end with `wakeAgent=false` before agent construction.
5. **Stable identity.** A changing observation fingerprint never creates a new incident or bypasses a wake budget.
6. **Lease before wake.** An attempt is durably claimed and fsynced before `wakeAgent=true` is emitted.
7. **Fence every result.** ACKs bind to the exact incident generation, attempt, random result
   nonce, random lease capability, manager job, bounded time window, and observation fingerprint.
8. **Revalidate before disposition or action.** The current typed Hermes snapshot is collected again after ACK and before any state transition that relies on it.
9. **No direct repair in v0.2.** Manager output is data. It cannot directly invoke tools or authorize a mutation.
10. **Bounded retries.** One initial wake plus one retry per incident generation; a global mission/day cap is enforced independently of incident fingerprints.
11. **Fail closed.** Missing/corrupt/unsafe ledger, incompatible Hermes seam, stale current-run identity, clock rollback, or ambiguous state prevents wake and mutation.
12. **Visible terminal state.** Acknowledgement never hides `resolved`, `human_required`, or `dead_letter` from projection/history.
13. **Private by construction.** No task prose, comments, summaries, prompts, logs, filesystem paths, credentials, PII, or free-form worker text enters wake or decision payloads.

## Component model

```text
Hermes typed CLI/API snapshot
          |
          v
Cyclops detector -> private incident ledger -> public-safe projection -> Dashboard
                          |
                          v
              default-profile cron router
              deterministic pre-run script
                  |               |
        wakeAgent=false     wakeAgent=true
          zero LLM                |
                                  v
                    fresh tool-free default manager
                                  |
                       exact JSON ACK in private
                         cron output artifact
                                  |
                                  v
                  Cyclops importer + revalidation
                     |           |             |
                  resolved  human_required  dead_letter
                                  |             |
                                  v             v
                    no-agent decision courier
                    configured home delivery
```

There are two pre-created, paused-by-default jobs owned by the `default` profile:

- **manager router:** recurring LLM-capable job with a deterministic pre-run script,
  `deliver=local`, `continuity=false`, and a compatibility-verified zero-tool configuration;
- **decision courier:** recurring `no_agent=true` script job that emits only a bounded public-safe packet for a leased `human_required` or manager-failure notification, otherwise empty stdout.

The router is the only incident path allowed to create a manager session. The courier cannot invoke a model. Neither job mutates Kanban.

## Stable incident identity

### Canonical identity tuple

The detector builds a versioned canonical JSON object with sorted keys and UTF-8 encoding:

```json
{
  "identity_version": 1,
  "kind": "phase_failed",
  "mission_id": "synthetic-release",
  "phase_key": "verify",
  "subject_task_id": "t_example",
  "subject_run_id": null
}
```

Allowed keys are fixed by incident `kind`. Values are validated identifiers or `null`; optional keys are present as `null`, not omitted. The stable key is:

```text
incident_id = "inc:v1:" + sha256(canonical_identity_json).hexdigest()
```

`severity`, current status, PID/liveness, retry count, timestamps, human text, and an observed-state hash MUST NOT participate in identity. They change over an incident's lifetime and would let churn reset budgets.

### Observation fingerprint

A separate `observation_sha256` hashes the bounded typed facts used for stale-result detection. It may change while `incident_id` remains stable. Each detection updates the latest fingerprint and typed observation, but does not reset attempts or notification state.

### Generations and recurrence

A terminal incident remains terminal while the condition is absent. If the same stable identity reappears after at least one committed clean tick, Cyclops increments `generation` and resets only generation-scoped counters. The global daily budget is not reset. Every lease and ACK includes both `incident_id` and `generation`, so a delayed prior result cannot close a recurrence.

## Detection and eligibility

A candidate is eligible only when all checks pass in one ledger transaction:

1. the approved manifest hash and bindings match;
2. the ledger and projection parent have safe owner, file type, and permissions;
3. the typed collector snapshot validates with no duplicate or contradictory identities;
4. the incident was observed on at least the configured consecutive-tick threshold;
5. the current tick is not the first tick after a gap or restart;
6. the condition is not a normal dependency wait or a valid already-delivered human gate;
7. no exact enabled deterministic rule applies;
8. no unexpired attempt lease exists for this incident;
9. per-generation attempts are below two;
10. the mission/day wake count and single-global-manager limits remain;
11. `next_attempt_at` has passed under a monotonic-safe persisted clock policy;
12. the Hermes compatibility seam is currently healthy.

Eligibility ordering is deterministic: severity rank, first detection tick, then `incident_id`. At most one lease may be acquired globally per router fire.

## Durable state

The existing private SQLite ledger advances to schema version 2 through an explicit, transactional migration with backup and rollback. It adds supervisor-owned state only.

### `incidents`

Required logical fields:

| Field | Purpose |
|---|---|
| `incident_id` | Full stable typed identity hash; primary key with `generation` |
| `generation` | Recurrence fence |
| `identity_version` | Canonical identity schema version |
| `mission_id`, `phase_key`, `kind` | Bounded typed identity fields |
| `subject_task_id`, `subject_run_id` | Optional authoritative references |
| `observation_sha256` | Latest typed observation fingerprint |
| `first_tick`, `last_tick`, `observed_ticks` | Deterministic persistence evidence |
| `lifecycle` | `detected`, `wake_sent`, `claimed`, `resolved`, `human_required`, or `dead_letter` |
| `terminal_reason` | Closed reason enum, never prose |
| `attempt_count` | Generation-scoped wake count, maximum 2 |
| `next_attempt_at` | Persisted backoff deadline |
| `acknowledged_at` | Orthogonal acknowledgement marker |
| `created_at`, `updated_at`, `terminal_at` | Audit timestamps |

### `wake_attempts`

| Field | Purpose |
|---|---|
| `attempt_id` | Random 128-bit identifier encoded as lowercase hex |
| `incident_id`, `generation` | Parent fence |
| `attempt_no` | `1` or `2`, unique per generation |
| `lease_token_sha256` | Hash of a random 256-bit capability; plaintext is never stored after handoff construction |
| `lease_owner` | Fixed router job identifier from installed manifest |
| `lease_acquired_at`, `lease_expires_at` | Lease interval |
| `observation_sha256` | Snapshot fence at claim time |
| `result_nonce_sha256` | Digest of the random 256-bit output-correlation nonce |
| `cron_execution_id` | Optional audit reference; never an authorization or correlation fence |
| `state` | `leased`, `output_seen`, `ack_valid`, `ack_invalid`, `expired`, or `superseded` |
| `error_code` | Closed error enum |

### `wake_budgets`

Stores mission and UTC-day counters with a unique `(mission_id, day)` key. The counter increments in the same transaction that creates an attempt. Clock rollback or an invalid time source closes the wake gate; it never creates a fresh budget bucket.

### `notification_intents`

Stores one durable intent per `(incident_id, generation, terminal_kind)` with a delivery lease, courier execution reference, attempt count, terminal delivery status, and a stable `decision_packet_id`. This is an outbox, not a message broker. Payload is reconstructed from typed incident fields and never stored as unrestricted prose.

SQLite remains `foreign_keys=ON`, `synchronous=FULL`, bounded `busy_timeout`, and a crash-safe journal mode selected and tested by the implementation. Every transition that consumes budget or grants a lease commits before external execution.

## Lifecycle and transition rules

```text
                      condition disappears
                 +------------------------------+
                 |                              v
 detected --threshold--> wake_sent --ACK--> claimed --> resolved
    ^                       |            |          \-> human_required
    |                       |            |
    |             lease expiry           +-- stale ACK -> revalidate/no action
    |                       |
    +---- retry/backoff ----+
                            |
                     attempts exhausted
                            v
                       dead_letter

resolved | human_required | dead_letter --receipt/manager ACK--> acknowledged_at set
```

`acknowledged_at` is not a lifecycle value. It is metadata on a visible terminal row.

Transition preconditions:

- `detected -> wake_sent`: eligibility passes and an attempt lease plus budget increment commits.
- `wake_sent -> claimed`: a complete ACK validates against exact schema, lease, generation, attempt, job, execution, and observation fence.
- `wake_sent -> detected`: lease expires, attempts remain, and deterministic backoff is scheduled.
- `wake_sent -> dead_letter`: lease expires or ACK is invalid after the second attempt, or a permanent manager/runtime incompatibility is detected.
- `claimed -> resolved`: authoritative revalidation proves the condition absent, or a future separately allowlisted deterministic rule is executed and verified.
- `claimed -> human_required`: the condition persists and v0.2 has no permitted deterministic action, or the ACK requests a genuine human decision.
- any nonterminal state -> `resolved`: a clean authoritative tick proves the condition absent; a later manager output becomes `superseded`.
- terminal -> recurrence: condition reappears after a committed clean tick; increment generation and return to `detected`.

Transitions use compare-and-set predicates on current lifecycle, generation, attempt, and observation fingerprint. A stale write affects zero rows and is recorded as superseded; it is never retried blindly.

## Wake protocol

### Router script behavior

The default-profile router script performs bounded deterministic work only:

1. open the Cyclops ledger through the public Cyclops CLI/library boundary;
2. reject invocation if task/run/workspace/board ownership or delegated-child markers are present;
3. import any unprocessed private cron result from the prior router execution;
4. reconcile expired leases and apply bounded backoff;
5. select and lease at most one eligible incident transactionally;
6. print exactly one final JSON gate line.

No eligible incident:

```json
{"wakeAgent":false}
```

Eligible incident:

```json
{
  "wakeAgent": true,
  "context": {
    "protocol": "cyclops-manager-ack/v1",
    "incident_id": "inc:v1:<sha256>",
    "generation": 1,
    "attempt_id": "<128-bit-hex>",
    "attempt_no": 1,
    "result_nonce": "<256-bit-hex>",
    "lease_token": "<256-bit-hex>",
    "lease_expires_at": "<RFC3339-UTC>",
    "observation_sha256": "<sha256>",
    "kind": "phase_failed",
    "mission_id": "synthetic-release",
    "phase_key": "verify",
    "subject_task_id": "t_example",
    "subject_run_id": null,
    "expected_state": "done",
    "observed_state": "failed",
    "allowed_recommendations": ["NOOP", "ESCALATE"]
  }
}
```

The serialized context has a strict byte limit, identifier limits, exact keys, and no free-form
text. The lease token is a short-lived capability used only to bind the ACK. The ledger stores
only its hash; plaintext may exist only in the private cron prompt/output artifact until bounded
retention removes it. Logs, projection, Dashboard, errors, and human packets must redact it.

### Manager session

The installed job is owned by `default` and MUST have:

- a self-contained immutable prompt describing the exact output schema;
- `continuity=false` so each attempt is a fresh session;
- a resolved zero-tool inventory. On Hermes Agent v0.20.3 the stored per-job value is
  `enabled_toolsets=["no_mcp"]`; a literal `[]` is forbidden because it falls back to cron
  platform defaults;
- `deliver=local` so routine ACK data never reaches a human channel;
- a pinned reviewed model/provider or a deliberate cron-fleet default;
- bounded turns and inactivity timeout;
- no attached skill that can add tools, mutable procedures, or free-form external data.

The prompt treats `context` as typed hostile data, not instructions. The manager may classify and propose only. It returns exactly one JSON object and no Markdown:

```json
{
  "protocol": "cyclops-manager-ack/v1",
  "incident_id": "inc:v1:<sha256>",
  "generation": 1,
  "attempt_id": "<128-bit-hex>",
  "result_nonce": "<256-bit-hex>",
  "lease_token": "<256-bit-hex>",
  "observation_sha256": "<sha256>",
  "ack": true,
  "recommendation": "ESCALATE",
  "reason_code": "NO_ALLOWLISTED_ACTION",
  "human_question_code": "REVIEW_INCIDENT"
}
```

Closed enums for v0.2:

- `recommendation`: `NOOP`, `ESCALATE`;
- `reason_code`: `CONDITION_MAY_HAVE_CLEARED`, `NO_ALLOWLISTED_ACTION`, `AMBIGUOUS_STATE`, `POLICY_DECISION`, `CREDENTIAL_REQUIRED`, `MATERIAL_RISK`;
- `human_question_code`: `NONE`, `REVIEW_INCIDENT`, `AUTHORIZE_FUTURE_RULE`, `PROVIDE_CREDENTIAL`, `CHOOSE_POLICY`.

Unknown keys, Unicode control characters, malformed JSON, oversize output, enum drift, duplicate JSON members, or a mismatched fence invalidate the ACK.

### Result import and revalidation

Cyclops never parses arbitrary session history or Kanban prose. A version-gated adapter supplies a
bounded window of private outputs for the stable manager job without treating execution IDs as a
correlation fence. The importer MUST:

1. inspect only a bounded private output window for the stable manager job;
2. require exactly one output matching the random result nonce and every incident, generation,
   attempt, fingerprint, job, time, and capability fence;
3. extract only that final response under a strict maximum size;
4. parse JSON with duplicate-key rejection and exact schema validation;
5. compare the plaintext lease token in constant time to the stored hash;
6. immediately recollect authoritative typed Hermes state;
7. verify incident identity, generation, current task/run identity, and observation fence;
8. mark stale or late output `superseded` with no action;
9. transition to `resolved` if the condition disappeared, otherwise to `human_required` in v0.2.

The adapter MUST NOT write Hermes cron files or databases. If the installed Hermes version does not expose a verifiable private result seam, wake mode is unsupported and remains disabled rather than falling back to session or Kanban database access.

## Human-required and manager-failure delivery

The public-safe decision packet contains only:

```json
{
  "packet_version": 1,
  "decision_packet_id": "dp:v1:<sha256>",
  "incident_id": "inc:v1:<sha256>",
  "generation": 1,
  "kind": "phase_failed",
  "severity": "critical",
  "mission_id": "synthetic-release",
  "phase_key": "verify",
  "terminal": "human_required",
  "reason_code": "NO_ALLOWLISTED_ACTION",
  "human_question_code": "REVIEW_INCIDENT",
  "observed_ticks": 3,
  "attempt_count": 1
}
```

The no-agent courier leases one notification intent and emits a compact rendered form to the configured home channel. Empty queue means empty stdout and no delivery. Manager runtime failure, invalid ACK exhaustion, or cap exhaustion uses terminal `dead_letter` with a closed failure code.

Delivery is **at-least-once**, because an external chat transport cannot provide atomic exactly-once commit with the local ledger. The stable `decision_packet_id` is the dedupe key. Cyclops suppresses repeat emissions after a verified successful Hermes courier execution; an indeterminate delivery may be retried once with the same packet ID. The UI must not claim exactly-once delivery.

`acknowledged_at` is set only by an explicit operator acknowledgement surface or a verified manager ACK, never merely because output was emitted. Human acknowledgement does not auto-repair, complete, or unblock any Kanban task.

## Retry, lease, and budget policy

Default v0.2 policy:

- persistence threshold: 2 consecutive committed ticks;
- initial attempt lease: 10 minutes;
- retry backoff: 5 minutes after lease expiry;
- maximum attempts: 2 per incident generation;
- maximum manager wakes: 4 per mission per UTC day;
- maximum concurrent manager leases: 1 globally;
- first post-gap tick: observe-only;
- no retry for a stale/superseded incident;
- one courier retry for an indeterminate delivery, with the same packet ID.

Values are bounded configuration, not manifest-defined executable policy. Lower limits are allowed; higher limits require a schema/version change and review. A model failure never delegates recursively or creates a Kanban card.

## Compatibility and fail-closed behavior

Before enabling wake mode, the installer verifies on a disposable paused job and synthetic ledger:

1. job ownership resolves to the canonical `default` profile;
2. quiet script output causes no agent construction and no inference usage;
3. each positive gate creates a fresh cron session;
4. task-scoped or delegated-child invocation is rejected before lease acquisition and makes no
   model call;
5. a normal scheduler-owned cron session is marked non-dispatcher-owned;
6. the configured `no_mcp`/toolset seam resolves to no terminal, file, web, Kanban, messaging,
   cron, delegation, or MCP tools; a literal empty list is tested to ensure it is not mistaken for
   zero tools;
7. the final response is saved privately before execution completion is recorded;
8. exactly one bounded private output matches the nonce plus every protocol fence;
9. normal scheduler script subprocesses receive the expected sanitized environment, and the
   script itself denies any unexpected inherited ownership markers;
10. delivery-local router output is not sent to a home channel;
11. no-agent courier empty stdout is silent and non-zero exit is visible as failure.

Any failed or ambiguous check sets compatibility state `unsupported`, leaves jobs paused, and preserves observe-only operation. Runtime version drift invalidates cached seam approval until checks pass again.

v0.2.1 persists that approval only as an owner-private atomic activation attestation. The router and
projection call the same side-effect-free validator against fresh supported full-definition evidence.
Absent evidence maps to `unchecked,false`; a current disabled record maps to `supported,false`; a
current enabled record maps to `supported,true`; every malformed, stale, unverifiable, or drifted
case maps to `unsupported,false`. Private paths, identifiers, hashes, versions and reason codes never
enter projection. The validator runs after task-scope denial but before lifecycle reconciliation,
lease acquisition, budget consumption, or any model construction.

## Installation, dry-run, and rollback

The installer is dry-run first. Apply stages private profile artifacts but delegates every job
mutation to the supported profile-local `cronjob` tool through a strict machine-readable spec.

### Dry-run plan

Dry-run prints a redacted deterministic plan containing:

- target profile name (`default`), resolved through supported profile discovery;
- scripts and their content hashes, never private absolute paths;
- paused router and courier job specifications;
- model/tool/delivery constraints;
- ledger migration version and backup action;
- compatibility checks;
- exact rollback actions.

It rejects task-scoped execution, an unknown/noncanonical manager profile, unsafe ownership/modes, existing name collisions, incompatible Hermes, missing configured home delivery, or any plan that exposes a tool to the manager.

### Apply transaction

The explicit `--apply` staging transaction and its tool consumer MUST:

1. obtain the complete tool-visible default-profile job snapshot immediately before planning and
   embed that snapshot plus its canonical hash in the private spec;
2. reject stable-name conflicts for an install, or require one paused owned job and the exact prior
   private spec for each upgrade target;
3. hold the profile-local installer lock while writing scripts/config atomically under the explicit
   default profile home with mode `0700` directories / `0600` files, restoring the exact prior
   content and mode (or removing attempt-created files) if any write or readback fails;
4. verify hashes and script path containment without opening Hermes cron storage;
5. emit create/update operations consumed by the supported profile-local `cronjob` tool and pause
   every newly created job before proceeding;
6. run the wheel-installed, synthetic-temporary-profile compatibility verifier;
7. after every create, update, and pause, read back the complete tool-visible job list and compare
   every security-relevant field of each present stable job with its snapshot-bound expected value;
8. leave jobs paused and print the operator enable plan.

No step enables, resumes, starts, or restarts a live job, timer, gateway, or service automatically.

### Rollback

On any verification failure, rollback in reverse order:

1. keep or return created jobs to paused;
2. restore prior job definitions through `cronjob.update`, or remove only job IDs returned by
   creates in this transaction;
3. restore prior scripts from the transaction backup or remove only newly created scripts;
4. restore the pre-migration ledger after integrity verification;
5. fsync restored files/directories;
6. report a redacted rollback result and remain observe-only.

Rollback uses snapshot-bound identifiers and content hashes, not names alone. The strict consumer
rejects the complete spec before its first tool call if any profile, identity, argument, snapshot,
operation, or rollback field is missing, extra, or inconsistent. It never deletes an operator-owned
job or file whose hash changed after the transaction began.

## Projection and Dashboard contract

Projection schema version 2 exposes bounded incident lifecycle without lease capabilities or private cron details:

- stable `incident_id` and `generation`;
- `kind`, severity, mission/phase and optional task/run IDs;
- `observed_ticks`, lifecycle, terminal reason code;
- attempt count and retry/deadline state as coarse counters;
- `manager_state`: `idle`, `leased`, `ack_valid`, `retry_wait`, `failed`;
- `notification_state`: `none`, `pending`, `sent`, `failed`, `acknowledged`;
- `acknowledged_at` and terminal timestamp when present;
- global budget used/limit and reset day;
- compatibility state: `supported`, `unsupported`, `unchecked`.

Never expose lease tokens or hashes, prompts, final manager JSON, output paths, cron job IDs, execution IDs, free-form reasons, task prose, comments, logs, credentials, PII, or exact private filesystem locations. The Dashboard remains authenticated and read-only.

Resolved rows remain visible for a bounded retention window and in durable history; resolution is not represented by silently deleting the incident.

## RED-first acceptance design

Implementation starts with failing tests. Tests use synthetic IDs, isolated profile homes, fake clocks, disposable SQLite files, and a fake Hermes adapter unless explicitly marked as an installed-Hermes seam test.

### Identity and persistence

1. Identical typed observations produce the same full incident ID across processes and key ordering.
2. Severity/status/fingerprint changes do not change identity or reset budgets.
3. A clean tick followed by recurrence increments generation and fences old ACKs.
4. First/transient detection remains `detected` and emits `wakeAgent=false`.
5. Threshold detection leases exactly once; 20 duplicate ticks do not create another live attempt.
6. Normal dependency waits and already-delivered human gates never wake.
7. First post-gap tick never wakes or repairs.

### Crash consistency and concurrency

8. Kill before lease commit: no budget consumed and no wake gate emitted.
9. Kill after lease commit/before output: lease expires, one retry occurs after backoff.
10. Kill after manager output/before import: next tick imports once and does not re-wake.
11. Two routers race: one compare-and-set lease wins globally.
12. Clock rollback closes the gate and does not reset daily budget.
13. Corrupt/missing/unsafe ledger remains observe-only with zero manager calls.
14. Lost/ambiguous cron result never produces a fabricated ACK.

### Task-scope and authority denial

15. Router invoked with synthetic task, board, run, workspace, or delegated-child ownership
    markers rejects the fire, acquires no lease, and creates no agent.
16. A normal scheduler fire creates a non-dispatcher-owned manager whose resolved schema contains
    zero tools, including Kanban, terminal, file, web, cron, messaging, delegation, and MCP.
17. A stored literal `enabled_toolsets=[]` fails the compatibility gate rather than silently
    inheriting cron platform tools.
18. Manager prompt contains no worker prose or instruction-bearing external text.
19. Spoofed inbox/task data cannot select `default`, close a task, or satisfy an ACK.
20. Attempts to return repair/complete/unblock/publish/deploy recommendations fail schema validation and execute zero mutation.
21. No runtime code writes directly to Hermes Kanban or cron databases.

### ACK, revalidation, and retry

22. Valid ACK with exact fences transitions `wake_sent -> claimed`.
23. Wrong token, generation, attempt, fingerprint, job, or execution is rejected.
24. Duplicate JSON keys, unknown fields, oversize output, and invalid Unicode are rejected.
25. Condition absent on immediate revalidation becomes visible `resolved` with no action.
26. Condition persistent after valid v0.2 ACK becomes `human_required`.
27. Stale current run/task identity supersedes the ACK and suppresses action.
28. Missing/invalid first ACK retries once; second failure becomes visible `dead_letter`.
29. Twenty ticks after exhaustion produce zero further wakes.
30. Fingerprint churn cannot bypass the mission/day cap.

### Human delivery and privacy

31. Healthy courier tick has empty stdout, zero messages, and zero LLM calls.
32. `human_required` emits one bounded packet with stable packet ID and no private fields.
33. Verified delivery suppresses repeats; indeterminate delivery retries once with the same packet ID.
34. Manager failure/dead-letter emits a packet; normal resolved ACK does not notify.
35. Public projection excludes tokens, prompts, paths, cron identifiers, execution identifiers, prose, logs, secrets, and PII.
36. Dashboard validates schema v2 and remains read-only.
37. Public repository/history scan passes with synthetic fixtures only.

### Installer and compatibility

38. Installer defaults to dry-run and changes no files, jobs, services, or ledger.
39. Apply creates/updates only hash-owned paused jobs and scripts.
40. Any read-back or seam failure rolls back all transaction-owned changes.
41. Concurrent operator modification prevents destructive rollback of that object.
42. Unsupported Hermes version or missing result seam leaves wake mode disabled.
43. No install/test path enables or resumes a live job or restarts a service.

### Cost and regression gates

44. Quiet router and courier paths instantiate no agent and record zero model/API/token usage.
45. One eligible attempt creates exactly one fresh manager session.
46. Full Python 3.11/3.12, lint, type, coverage, Bandit, dependency audit, public scan, and package build gates pass.
47. Existing observe-only behavior remains the default unless wake mode is explicitly configured and compatibility-approved.

## Verification evidence required from implementation

A future implementation handoff must include:

- exact commit and changed files;
- schema migration and rollback evidence;
- installed-Hermes seam test output;
- executed no-token assertions for quiet/transient/post-gap/budget paths;
- crash and concurrency test results;
- manager zero-tool inventory;
- public projection privacy tests and repository/history scan;
- proof that created jobs remain paused;
- exact residual risks and any unsupported Hermes versions.

## Threat-driven residual risks

- Hermes private output shape is an external compatibility seam. Zero or multiple nonce-fenced
  matches disable disposition; Cyclops never guesses by recency.
- A compromised local account that can modify both Cyclops and the default profile defeats same-host controls.
- At-least-once chat delivery can duplicate a packet after an indeterminate transport result; packet IDs support dedupe but cannot force every platform to deduplicate.
- A tool-free manager can still classify incorrectly. Cyclops therefore treats output as non-authoritative data and escalates unresolved v0.2 incidents instead of acting.
- Local host loss can remove monitoring and delivery together; external uptime monitoring remains required for host-level assurance.
- A global daily cap limits spend but can delay later legitimate incidents; cap exhaustion is visible and human-delivered once.

## Public references

- [Cyclops issue #4](https://github.com/masterlf/skynet-cyclops/issues/4)
- [Hermes scheduled tasks (Cron)](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- [Hermes Kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban)

The installed-code compatibility review covered `cron/jobs.py`, `cron/scheduler.py`,
`model_tools.py`, and `tools/environments/local.py` from Hermes Agent v0.20.3. Those source-level
observations are deliberately protected by runtime seam tests because they may change in later
Hermes releases.
