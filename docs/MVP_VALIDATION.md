# MVP validation

Last validated on 2026-08-23. The local automated run used CPython 3.13.3; the supported package
range is Python 3.11–3.14.

## Automated suite

The suite contains 46 tests and reports 75.27% branch-aware coverage. CI enforces a 70% minimum and
is configured for Python 3.11 and 3.14 on Ubuntu plus Python 3.13 on macOS and Windows.

Covered behavior includes:

- OAP parsing, validation, profile/spec digests, secret rejection, discovery precedence, minimal
  authoring, and prompt assembly.
- Bot serialization, fallback metadata, atomic sessions, transcript construction, and resume.
- Registry isolation, per-harness executable overrides, OS-separated search paths, Windows
  environment-backed bins, private rootless prefixes, PATHEXT-preserving fallback lookup, bounded
  Unicode-safe version probes, and shell-free execution.
- Argv construction and provider-aware model projection for all fourteen harness adapters.
- Structured JSON/JSONL parsing, embedded failure detection, cancellation, timeout, output bounds,
  Kimi config override, OpenClaw workspace routing, AGY print syntax, and atomic Anton REPL input.
- CLI profile/bot workflows, dry runs, JSON automation, UI API behavior, and invalid-harness errors.

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
