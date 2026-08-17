# Contributing

## TL;DR

Open an issue before large changes. Keep pull requests narrow, test-first, synthetic, and free of personal or private data.

## Development

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade 'pip>=26.1.2'
python -m pip install -e '.[dev]'
pytest --cov=skynet_cyclops --cov-report=term-missing
```

Follow `AGENTS.md`. Every pull request must include:

- problem and non-goals;
- exact tests and commands executed;
- security/privacy impact;
- rollback or disable path;
- documentation changes.

Use conventional commit subjects such as `feat:`, `fix:`, `docs:`, `test:`, and `chore:`.

## Review

Behavioral changes need independent review. Review summaries are claims until reproduced from the exact commit.
