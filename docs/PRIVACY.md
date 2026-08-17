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

## Public contributions

Never include real infrastructure, customer, employee or account data in issues, tests, screenshots or examples. Use:

- `example.invalid` for domains;
- documentation-reserved TEST-NET addresses when an IP is necessary;
- generic roles such as `builder`, `reviewer` and `release`;
- randomized synthetic IDs.

The repository scanner rejects common credential shapes, private paths, real email-like addresses and private network ranges.
