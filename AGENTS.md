# Skynet-Cyclops contributor instructions

Skynet-Cyclops is a public security-sensitive reliability project.

## Non-negotiable rules

1. Never commit secrets, credentials, tokens, private infrastructure data, personal data, private paths, real email addresses, private IP addresses, or production identifiers.
2. Use synthetic examples under `example.invalid`, TEST-NET address space, and generic profile names.
3. Kanban is canonical. Never write directly to Hermes internal databases.
4. Nominal supervision must execute with zero LLM calls.
5. The supervisor must never become a second dispatcher, retry engine, publisher, or deployment authority.
6. Dashboard integration is read-only.
7. All subprocesses use argument arrays with `shell=False` and a sanitized environment.
8. New behavior is test-first. Security, crash consistency, idempotency, and no-token invariants require regression tests.
9. Public documentation starts with a concise TL;DR and separates facts, design decisions, risks, and non-goals.
10. Do not claim support or release readiness without executed evidence.

## Required local gates

```bash
ruff check .
ruff format --check .
mypy src
pytest --cov=skynet_cyclops --cov-report=term-missing
bandit -r src -q
pip-audit
python scripts/public_repo_scan.py .
python -m build
```

## Scope discipline

The first public release is observe-first. Repairs and model wakes remain disabled unless their exact rule is explicitly implemented, tested, independently reviewed, and documented.
