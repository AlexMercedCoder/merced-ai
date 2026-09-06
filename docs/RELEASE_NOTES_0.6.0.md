# Merced AI 0.6.0

Release candidate prepared on 2026-09-06.

Merced AI now models WebMCP as an explicit harness capability. A bot can require WebMCP in its
portable binding, and routing will select only a native MagAgent or Loro adapter that satisfies the
requirement. The CLI and local Web UI show capability readiness and preserve the requirement when
bot bindings are created, loaded, or edited.

Merced AI remains a broker: live page discovery, browser credentials, exact-origin policy,
registry-revision checks, user approval, and audit records stay within the selected harness. This
keeps the trust boundary clear and avoids duplicating browser authority in the broker.

## Validation

- Complete Python suite: 107 passed, 3 skipped, with 78.99% coverage against a 70% gate.
- Bot persistence, capability inventory, routing, Web API, and browser UI regression coverage.
- Ruff lint and formatting gates.
- Wheel and source archive build with Twine metadata validation.

See [WebMCP routing](WEBMCP.md) for configuration and fallback behavior.
