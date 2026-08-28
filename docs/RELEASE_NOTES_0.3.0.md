# Merced AI 0.3.0

Merced AI 0.3.0 aligns its specification integrations with the published OAP and AGS 1.0.1 Python
support libraries.

## Highlights

- OAP 1.0 Level 1 broker evidence pinned to upstream commit
  `7fb633a1a59dd7636ffb0030d254f2f58934f74a`.
- AGS 1.0 Level 0 validation and deterministic read-only planning pinned to upstream commit
  `f180a4dbd07911f90dd0821f531d7ccd51bb0764`.
- Dynamic CI coverage for every upstream valid example and invalid fixture.
- Graph digests, dependency ordering, reachability, worst-case work, cost/tier summaries, and
  explicit unsupported-feature reporting.
- OAP trust assignment and discovery-collision reporting remain derived from local roots rather
  than profile-authored metadata.
- The profile management UI now surfaces those trust and collision warnings instead of leaving
  them visible only in API responses.

## Boundaries

Merced AI remains a broker. It does not execute AGS graphs or claim AGS Levels 1–3, and it does not
replace a selected harness's effective permission, sandbox, credential, or approval enforcement.
The conformance files describe Merced AI's own parser, projection, and planning behavior.

## Upgrade

```bash
python -m pip install --upgrade "merced-ai==0.3.0"
merced-ai --version
merced-ai harness list
```

See [Agentic Graphs](AGENTIC_GRAPHS.md), [oap-conformance.json](oap-conformance.json), and
[ags-conformance.json](ags-conformance.json) for the exact specification boundary.
