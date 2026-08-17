# Threat model

## TL;DR

Cyclops assumes a trusted single-host operator but treats manifests, Kanban metadata, worker output, status files and subprocess output as hostile data. The highest risks are authority expansion, prompt injection, command injection, stale writes, retry amplification and public-data leakage.

## Assets

- integrity of Hermes Kanban workflow state;
- confidentiality of task prose, logs, paths and credentials;
- correctness of supervisor incidents and health projection;
- bounded compute and model usage;
- integrity of release decisions.

## Trust boundaries

1. **Manifest boundary:** operator-controlled regular-file configuration enters the supervisor.
2. **Hermes boundary:** JSON and text returned by the Hermes CLI are untrusted parsed input.
3. **Worker boundary:** comments, summaries and evidence may contain hostile instructions.
4. **Dashboard boundary:** browser-visible data must be strictly validated and rendered as text.
5. **Subprocess boundary:** environment and arguments must not inherit delegated-task authority.

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
