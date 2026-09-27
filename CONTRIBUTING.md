# Contributing

## Development setup

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
```

The OAP and AGS conformance tests read the upstream fixture repositories. Clone
[open-agent-profile](https://github.com/alexmerced-oss/open-agent-profile) and
[agentic-graph-spec](https://github.com/AlexMercedCoder/agentic-graph-spec) next to this checkout,
or point `OAP_FIXTURE_REPO` and `AGS_FIXTURE_REPO` at them. Without either, those tests are skipped
and pytest prints the reason.

Keep changes focused and preserve unrelated worktree modifications. Before submitting:

```bash
python -m ruff format --check .
python -m ruff check .
python -m mypy
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
- a disposable no-tool live qualification when the harness is available (the opt-in
  `MERCED_AI_LIVE_SMOKE=1` suite in `tests/test_live_smoke.py`); and
- compatibility and troubleshooting documentation.

Do not add provider secrets, personal paths, harness state, generated sessions, or live logs to the
repository. Never broaden permissions to make a test pass.

## Issues and security

Use the issue templates for bugs and feature requests. Report vulnerabilities through GitHub
Security Advisories as described in [SECURITY.md](SECURITY.md), not a public issue.
