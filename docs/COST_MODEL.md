# Cost model

## TL;DR

Healthy Cyclops supervision uses zero LLM calls. Cost controls are structural: short deterministic ticks, no duplicate dispatcher, no model-based routing and no hidden retry loop.

## Nominal path

| Operation | LLM calls |
|---|---:|
| Manifest validation | 0 |
| Bootstrap planning/application | 0 |
| Healthy supervisor tick | 0 |
| Status projection | 0 |
| Dashboard rendering | 0 |

The default timer interval is 120 seconds: 720 bounded ticks per day. Each tick performs local structured reads and one small atomic projection update.

## Worker cost

Hermes owns worker execution and retry budgets. Cyclops reads those counters and must never multiply them with a second retry policy.

Machine-verifiable phases should use `goal_mode: false`; per-turn semantic judges are reserved for genuinely open-ended tasks.

## Future manager proposals

Manager proposals are not enabled in the initial release. Any future implementation must enforce:

- no tools;
- one proposal plus one retry per stable incident;
- a per-mission daily cap;
- a global single-flight limit;
- explicit usage source tagging;
- dead-letter after budget exhaustion.

Actual or estimated token cost is telemetry, never an authorization input. Unknown cost is displayed as unknown, not zero.
