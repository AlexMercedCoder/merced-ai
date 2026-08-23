# MVP validation

Validated on 2026-08-23 with Python 3.14.5.

## Automated coverage

- Reference OAP parsing, validation, profile and spec digests, secret rejection, and discovery
  precedence.
- Minimal profile authoring and prompt assembly.
- Bot serialization, fallback metadata, and round trips.
- Atomic session creation, turn persistence, listing, transcript construction, and resume surface.
- Harness registry duplicate protection and missing-executable isolation.
- Shell-free executable probing and bounded version output.
- Codex, Claude, Gemini, MagAgent, and Loro command construction.
- Structured JSON result normalization and failure containment.
- Child termination on cancellation.
- Provider-aware model substitution.
- CLI profile creation, bot creation, JSON dry run, and invalid harness handling.

The release suite contains 21 tests and reports 72% coverage with branch measurement
enabled. CI enforces a 70% minimum.

Validation commands:

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest -q
python -m build
```

## Packaged installation

The built wheel was installed into a clean virtual environment. Its console entry point loaded the
published `open-agent-profile` dependency and independently discovered all seven locally installed
harness families.

## Live harness smoke

A packaged Merced AI build created an OAP-backed reviewer bot, projected it onto Codex, executed a
minimal no-tool prompt, normalized the result, and persisted the user and assistant turns with the
pinned OAP profile and spec digests.

Expected and observed response:

```text
MERCED_AI_SMOKE_OK
```

Harness-reported elapsed time was 4.752 seconds.

## MVP limitations

- Capability declarations remain explicitly unverified until transport-specific handshakes land.
- Authentication is tested when a real run begins rather than during discovery.
- OpenCode and Goose are inventory-only in this release.
- Codex and Gemini use a visibly degraded, delimited prompt projection because their selected MVP
  transports do not expose a portable OAP system-instruction slot.
- Merced AI resumes its own bounded transcript; it does not yet resume every harness-native session.
- Generic ACP streaming, tool-event normalization, and approval forwarding are post-MVP work.
- OAP Level 2 state delta generation and writeback are post-MVP work.
