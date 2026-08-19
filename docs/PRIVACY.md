# Privacy

## TL;DR

Cyclops should not need task prose, logs, prompts or personal data. Its public examples are synthetic, and its status projection is designed for metadata minimization.

## Data minimization

Cyclops processes only the identifiers and counters required to derive workflow health. It does not copy:

- task bodies or comments;
- model prompts or responses;
- worker logs;
- credentials or environment variables;
- repository contents;
- personal profile data.

## Runtime data

The private incident ledger and projection may contain mission, task and run identifiers. Operators should treat these as operational metadata and apply local filesystem permissions appropriate to their environment.

Manager wake-up capabilities are private. Plaintext lease tokens exist only in the bounded cron
context and manager ACK artifact; the ledger stores a SHA-256 digest. Projection, Dashboard,
decision packets, logs, and errors exclude tokens and token hashes, prompts, manager JSON, cron
job IDs, output paths, task prose, comments, summaries, and free-form reasons. Per-attempt result
nonces are stored only as SHA-256 digests in the ledger and remain in bounded private cron output.

Human-required delivery contains only closed enums, validated identifiers, counters, severity,
generation, and a stable `dp:v1:` packet ID. Delivery is honestly at-least-once: an indeterminate
transport result may repeat the same packet ID. Resolved incidents remain visible but do not emit
a human packet.

## Public contributions

Never include real infrastructure, customer, employee or account data in issues, tests, screenshots or examples. Use:

- `example.invalid` for domains;
- documentation-reserved TEST-NET addresses when an IP is necessary;
- generic roles such as `builder`, `reviewer` and `release`;
- randomized synthetic IDs.

The repository scanner rejects common credential shapes, private paths, real email-like addresses and private network ranges.
