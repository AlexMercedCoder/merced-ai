# MVP validation

Last validated on 2026-09-27 (0.8.0 work on the `claude/next-release` branch). The local automated
run used CPython 3.13.3; the supported package range is Python 3.11–3.14.

## Automated suite

The 0.8.0 branch suite completed with 264 passing tests and 36 environment-gated skips (opt-in live
smoke tests, browser tests that run in their own CI step, and a Windows-only process-tree test) at
79.85% branch-aware coverage; CI enforces 75%. The three browser end-to-end tests pass locally in
Chromium. CI runs Python 3.11 and 3.14 on Ubuntu plus Python 3.13 on macOS and Windows, with ruff,
mypy (Ubuntu), the release-metadata check, and the package build. Earlier release evidence (0.3.0:
95 tests, 80.69%) is kept below for history.

Covered behavior includes:

- OAP parsing, validation, profile/spec digests, secret rejection, discovery precedence, minimal
  authoring, and prompt assembly.
- AGS 1.0 validation, RFC 8785 digests, deterministic ordering, reachability, work/cost summaries,
  and expected-code rejection of every immutable upstream invalid fixture.
- Bot serialization, fallback metadata, atomic sessions, transcript construction, resume, and
  transparent loading of legacy one-bot session JSON.
- Registry isolation, per-harness executable overrides, OS-separated search paths, Windows
  environment-backed bins, private rootless prefixes, PATHEXT-preserving fallback lookup, bounded
  Unicode-safe version probes, and shell-free execution.
- Argv construction and provider-aware model projection for all fourteen harness adapters.
- Structured JSON/JSONL parsing, embedded failure detection, cancellation, timeout, output bounds,
  Kimi config override, OpenClaw workspace routing, AGY print syntax, and atomic Anton REPL input.
- CLI profile/bot workflows, dry runs, JSON automation, and invalid-harness errors.
- UI cookie authentication, security headers, cross-origin rejection, profile/model/permission
  editing, bot creation, session/export workflows, approval preflight, SSE lifecycle/tool/error
  events, subprocess cancellation, immediate bootstrap, progressive/cached harness detection,
  corrupt-cache fallback, explicit refresh, responsive contracts, and accessibility structure.
- Multi-bot mention/all/named/round-robin selection, deterministic response ordering, per-bot
  attribution, progressive lifecycle events, exact failed-bot retry, approval aggregation,
  naming/derivation, partial failures, group export, and CLI JSON automation.
- Real Chromium coverage for searchable ordered group creation, progressive responses, stable
  attribution, completion synchronization, mention completion, derived-room controls, error-safe
  dialog behavior, stale-status clearing, and desktop/mobile screenshots.

Validation commands:

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

## Live harness qualification

All fourteen installed harnesses completed bounded requests through Merced AI from an empty
disposable workspace. Prompts requested a unique exact token and prohibited tool calls. Provider
keys were inherited from the environment and never written to project configuration.

| Harness | Live result | Qualification detail |
| --- | --- | --- |
| Codex | pass | noninteractive exec; arbitrary disposable directory supported |
| Claude Code | pass | structured print mode |
| Gemini CLI | pass | API-key auth; explicit `gemini-3.5-flash` |
| OpenCode | pass | transient provider stream failure recovered on rerun |
| Goose | pass | structured JSON run |
| Loro | pass | native OAP profile |
| MagAgent | pass | native profile with compatible minimal schema |
| Anton | pass | repaired `httpx`; atomic one-turn REPL bridge; clean normalized output |
| DSH | pass | OpenAI `gpt-5.4` through `llm-pi-ai` and `OPENAI_API_KEY` reference |
| AGY | pass | `--print=<prompt>` JSON mode |
| Pi | pass | explicit `google/gemini-3.5-flash` |
| Prime Agent | pass | structured print mode |
| OpenClaw | pass | rootless 2026.7.1-2; embedded OpenAI local agent |
| Kimi Code CLI | pass | 1.49.0; OpenAI Responses provider; forced plan mode |

Anton deserves a specific note: its initial repaired run received multiline profile text as
separate REPL turns and used inspection tools before the prohibition arrived. That run was rejected
as qualification evidence. The adapter now collapses the profile and request into one atomic turn;
the successful rerun returned only `ANTON_ATOMIC_OK` in 4.132 seconds with no intermediate tool
activity.

## Package validation

The source distribution and wheel build successfully, and Twine accepts their metadata. The final
wheel was installed with resolved dependencies into a fresh Python 3.14 virtual environment. Its
console entry point reported `merced-ai 0.1.0` and independently discovered all fourteen installed
harnesses, including Anton's repaired environment and rootless OpenClaw. Optional UI dependencies
remain covered by the automated UI suite and require a separate extra installation at release time.
The real Uvicorn server also completed an authenticated profile, bot, session, and streamed-message
workflow against a deterministic fake Codex executable.

Group behavior is validated with deterministic fake adapters rather than paid providers: each
participant traverses the same already-qualified adapter contract. The suite verifies isolated
per-bot prompts/routes and concurrent fan-out code paths without sending model traffic.

The 0.2.0 release candidate was also built into a clean temporary output directory and installed,
with the `webui` extra, into a fresh Python 3.14 virtual environment outside the checkout. Twine
accepted both artifacts; the installed entry point reported `merced-ai 0.2.0`; init, profile
creation, bot binding, dry-run projection, status, and all fourteen bounded version probes
completed. The packaged loopback server started successfully and served its embedded UI assets.
Anton required restoring its declared `httpx` runtime dependency in the external uv tool
environment; no Merced AI package change was required.

The 0.3.0 wheel and source distribution pass Twine metadata checks. A clean wheel installation
reports `merced-ai 0.3.0` and the CLI smoke path succeeds with the OAP and AGS 1.0.1 dependencies
resolved from the built artifact. Final hosted release evidence must still come from the tagged
commit and supported-platform CI matrix.

## Live harness smoke suite (opt-in)

`tests/test_live_smoke.py` runs only with `MERCED_AI_LIVE_SMOKE=1`. It has two parts:

- **Flag check (no model call):** builds each adapter's exact argv and checks every flag appears in
  the installed harness's own `--help` (or subcommand help).
- **Answer check (one tiny call per harness):** sends "Reply with OK" through the real adapter with
  edit and shell denied, and requires the reply to be exactly `OK` (so an echoed prompt fails).

```bash
MERCED_AI_LIVE_SMOKE=1 MERCED_AI_LIVE_SMOKE_HARNESSES=claude,codex \
  MERCED_AI_LIVE_SMOKE_REPORT=/tmp/merced-smoke.jsonl python -m pytest --no-cov tests/test_live_smoke.py
```

Loro and MagAgent run only with `MERCED_AI_LIVE_SMOKE_NOUS=1`, against Nous Portal
`deepseek/deepseek-v4-flash` in a throwaway `HOME` (and, for MagAgent, a throwaway user).
`MERCED_AI_LIVE_SMOKE_GEMINI_API_KEY=1` runs Gemini with a throwaway `HOME` so it uses
`GEMINI_API_KEY`. The user's real harness configuration is never read or changed by those runs.
The report file redacts anything that looks like a key.

### Results, 2026-09-27 (Linux)

Flag check: all 13 working installed harnesses passed; Anton was skipped because its installed
tool environment fails to import (`anton version` raises), not because of an adapter flag.

| Harness | Version | Prompt delivery | Result |
| --- | --- | --- | --- |
| Claude Code | 2.1.282 | stdin + system-prompt file | `OK` |
| Codex | 0.155.0 | stdin (`exec -`) | `OK` |
| Gemini CLI | 0.57.0 | stdin | `OK` with API-key auth. The machine's cached Google login fails with "client no longer supported for Gemini Code Assist for individuals", an account issue outside Merced AI. |
| Goose | 1.48.0 | stdin (`--instructions -`) | `OK` after a fix: the reply had been the echoed user message |
| OpenCode | 1.18.31 | stdin | `OK` after a fix: the reply had been empty |
| MagAgent | 1.3.0 (repo build) | argument | `OK` via Nous after a fix: the reply had been empty; the globally installed 1.1.2 needs `magent user create` first |
| Loro | 0.21.0 (repo build) and 0.19.2 | argument | Not passing. Merced AI delivered the run, but `loro run` reported `provider_error` (HTTP 401 with an OpenAI-style "Incorrect API key" message) while `loro providers smoke` with the same config and key returned `ok`. This is a Loro issue; Merced AI now reports it as a failed run instead of returning the summary as the reply. |

Model calls made: about 17, all one-line prompts (9 through the suite across four runs while fixing the issues above, 8 manual reproductions); the Loro attempts were rejected with HTTP 401.

### ACP and eval results, 2026-09-27 (Linux)

`tests/test_live_smoke.py::test_acp_streams_and_resumes_natively` ran two turns over ACP for each
verified launcher: Claude Code (`claude-agent-acp` 0.79), Goose 1.48, and OpenCode 1.18 streamed
`OK` and resumed their own session on the second turn; Gemini CLI 0.57 (API-key auth) streamed
`OK` but cannot load a session in a new process, so it starts a new session each turn.
`codex-acp` started sessions on a model this account cannot use (HTTP 400), so Codex stays on its
subprocess adapter unless opted in.

A live `merced-ai eval run` with a read-only profile, "Reply with OK", Codex, Claude Code, Goose,
and OpenCode, `--contains OK --max-chars 5`, and Claude Code as judge: all four passed 2/2 checks
and the judge scored each 10/10.

Additional model calls for 0.8.0 work: about 30, all one-line prompts (ACP probes and two-turn
resume checks, the eval above with its judge, and reproductions while fixing Gemini's ACP output).

## Known boundaries

- A version probe verifies executable readiness only, not authentication, provider quota, model
  availability, ACP conformance, or effective tool permissions.
- Capability declarations remain provisional until transport-specific runtime handshakes and
  effective-policy reporting are implemented.
- Native OAP currently means profile-name handoff to Loro or MagAgent; their schemas and runtime
  policies remain authoritative.
- Merced resumes its normalized transcript but does not yet resume every harness-native session.
- Generic ACP streaming, tool-event normalization, approval forwarding, and OAP Level 2 state
  writeback remain post-MVP work.
