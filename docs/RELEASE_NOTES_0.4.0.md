# Merced AI 0.4.0

Merced AI 0.4.0 closes the local collaboration UI's workspace-context and run-observability gaps
while retaining the broker's deliberately small authority boundary.

## Highlights

- Select up to twenty project files or bounded browser uploads as explicit context for a run.
  Text-like files are inlined within per-file and aggregate budgets; large, binary, and image files
  are stored beneath `.merced-ai/attachments/` and sent as confined workspace references.
- Inspect durable run records independently of conversation transcripts, including participants,
  context manifests, normalized events, elapsed time, completion state, and partial failures.
- Opt into browser completion notifications and copy a shell-quoted handoff command for the active
  harness and workspace.
- Preserve loopback/token/origin protections and add traversal, internal-state, upload-size,
  inline-context, and run-record durability coverage.

## Validation

The Python formatting, lint, test, and coverage gate passes. Static UI behavior is covered by asset
and live API tests, and the wheel/source distributions pass Twine metadata validation. The browser
context surface does not bypass the selected harness's own sandbox, permissions, credentials, or
approval model.
