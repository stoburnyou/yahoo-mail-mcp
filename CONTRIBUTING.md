# Contributing

Contributions are welcome, especially synthetic regression tests, Yahoo IMAP
compatibility fixes, security hardening, and documentation improvements.

## Development

1. Fork and clone the repository.
2. Install dependencies with `uv sync`.
3. Create a focused branch.
4. Run:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src/yahoo_mail_mcp
uv run pytest -q
uv build
uv run twine check dist/*
```

Never use real mailbox headers, email addresses, credentials, app passwords,
unsubscribe tokens, database files, or Railway secrets in fixtures, issues, or
pull requests. Use synthetic data only.

For security vulnerabilities, follow [SECURITY.md](SECURITY.md) instead of
opening a public issue.
