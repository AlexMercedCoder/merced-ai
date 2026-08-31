# Merced AI 0.5.1

Released on 2026-08-31.

Merced AI can now present AAIS 1.0 exact-action requests from MagAgent and Loro in its Web UI and
return digest-bound decisions over a dedicated bidirectional process channel. Policy remains owned
by the selected child harness; Merced AI persists only the pending presentation state.

## Validation

- Complete Python suite: 106 passed, 3 skipped, with 78.87% coverage against a 70% gate.
- AAIS relay, persistence, idempotency, Web API, and accessibility regression coverage.
- Wheel and source archive build with Twine metadata validation.
