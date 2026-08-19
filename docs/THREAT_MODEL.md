# Threat model

## TL;DR

Cyclops assumes a trusted single-host operator but treats manifests, Kanban metadata, worker output, status files and subprocess output as hostile data. The highest risks are authority expansion, prompt injection, command injection, stale writes, retry amplification and public-data leakage.

## Assets

- integrity of Hermes Kanban workflow state;
- confidentiality of task prose, logs, paths and credentials;
- correctness of supervisor incidents and health projection;
- bounded compute and model usage;
- integrity of release decisions.
- integrity and confidentiality of manager leases, ACK fences and wake budgets;
- reliable visibility of `resolved`, `human_required` and `dead_letter` outcomes.

## Trust boundaries

1. **Manifest boundary:** operator-controlled regular-file configuration enters the supervisor.
2. **Hermes boundary:** JSON and text returned by the Hermes CLI are untrusted parsed input.
3. **Worker boundary:** comments, summaries and evidence may contain hostile instructions.
4. **Dashboard boundary:** browser-visible data must be strictly validated and rendered as text.
5. **Subprocess boundary:** environment and arguments must not inherit delegated-task authority.
6. **Cron/profile boundary:** the manager must execute in the canonical `default` profile as a
   fresh non-dispatcher-owned cron session, never as a task-scoped Kanban worker.
7. **Manager-result boundary:** model output and private cron artifacts are untrusted until exact
   schema, lease, generation, execution and current-state revalidation succeeds.
8. **Human-delivery boundary:** only bounded typed decision packets may cross from private state
   to a configured home channel.

## Threats and controls

| Threat | Primary controls |
|---|---|
| Direct corruption of Kanban state | no direct DB writes; supported CLI/API only; exclusive bootstrap apply lock |
| Command injection | `shell=False`; fixed argv; strict identifiers |
| Prompt injection | no LLM in nominal path; prose excluded from status; future proposer receives typed quoted evidence only |
| Duplicate workers | Cyclops never dispatches/reclaims; Hermes remains sole dispatcher |
| Retry/token storm | no nominal model calls; tick debounce; hard future wake budgets; Kanban is sole worker retry authority |
| Stale supervisor mutation | observe-only initial release; future CAS-like revalidation and intent log |
| Human gate bypass | no block-category conversion; unknown/needs-input fail closed |
| Self-review | distinct reviewer required by manifest validation |
| Secret/PII leakage | shared deeply validated projection contract; private ownership/modes; public repository and history scanner; synthetic fixtures |
| Environment authority leak | sanitized subprocess environment; delegated/Kanban ownership markers removed |
| Reboot repair storm | non-persistent timer; first post-gap tick observe-only |
| Supervisor compromise | minimal authority; systemd hardening; no publisher/deployer path |
| Task-scope confusion / spoofed inbox | never assign `default` a card; cron non-dispatcher-owned context; explicit task-scope denial tests; manager has zero Kanban tools |
| Duplicate or replayed manager result | stable incident generation; random lease capability stored only as a hash; exact attempt/execution fences; constant-time token comparison; compare-and-set transitions |
| Wake-budget bypass by state churn | stable identity excludes severity/status/fingerprint; observation hash is a separate stale-result fence; mission/day cap is independent of incident generation |
| Crash between wake decision and model run | lease and budget commit before `wakeAgent=true`; expiry plus one bounded retry; no blind retry after ambiguous output |
| Prompt injection through workflow data | wake payload contains exact bounded typed fields only; no prose/comments/logs; manager is tool-free; exact JSON output schema |
| Manager overreach or bad classification | diagnose/propose only; closed enums; output is non-authoritative data; immediate typed revalidation; unresolved v0.2 incidents become `human_required` |
| Lease/capability disclosure | restrictive ledger/output permissions; token redaction; no token in projection, Dashboard, logs or human packet; short expiry |
| Silent manager or compatibility failure | one retry then visible `dead_letter`; version-gated Hermes seam; unsupported behavior disables wake mode |
| Duplicate/missing human notification | durable notification outbox and stable packet ID; verified delivery suppresses repeats; indeterminate delivery retries once; no exactly-once claim |
| Installer confused deputy / rollback clobber | dry-run default; canonical profile discovery; paused jobs; hash ownership; exact read-back; reverse rollback that refuses changed operator objects |

## Explicit non-goals

Cyclops is not:

- a sandbox for untrusted code;
- a multi-host consensus system;
- an authorization service;
- a secret manager;
- a replacement for Hermes task/run persistence;
- a release publisher or deployment controller.

## Residual risk

A privileged local administrator can alter Cyclops, Hermes or their data. Single-host failure can remove both workflow and monitoring availability. External uptime monitoring is required to detect complete host loss.

Hermes cron result correlation is a versioned external seam; runtime drift can reduce availability
by disabling wake mode. Chat delivery is at-least-once, so an indeterminate transport outcome may
duplicate a packet with the same dedupe ID. A tool-free manager can still classify incorrectly,
but cannot directly act; Cyclops revalidates and escalates unresolved incidents.
