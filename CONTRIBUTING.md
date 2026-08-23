# Contributing

## Development setup

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

Keep changes focused and preserve unrelated worktree modifications. Before submitting:

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest -q
python -m build
```

## Adapter contributions

An adapter change should include:

- authoritative CLI/version research;
- bounded executable discovery without filesystem-wide scanning;
- argv construction with `shell=False`;
- explicit workspace, timeout, cancellation, and output behavior;
- honest OAP field projection and degradation reporting;
- controlled contract tests for success, failure, and malformed output;
- a disposable no-tool live qualification when the harness is available; and
- compatibility and troubleshooting documentation.

Do not add provider secrets, personal paths, harness state, generated sessions, or live logs to the
repository. Never broaden permissions to make a test pass.

## Issues and security

Use the issue templates for bugs and feature requests. Report vulnerabilities through GitHub
Security Advisories as described in [SECURITY.md](SECURITY.md), not a public issue.
