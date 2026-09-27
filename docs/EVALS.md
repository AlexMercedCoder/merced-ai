# Comparing harnesses

The same OAP profile can run on any installed harness. An eval sends one prompt, with one
profile, to several harnesses and puts the replies side by side with scores.

```bash
merced-ai eval run -p tester --prompt "Reply with OK" -H codex -H claude -H goose \
  --contains OK --max-chars 5
merced-ai eval run -f eval.yaml --json
merced-ai eval list
merced-ai eval show EVAL_ID
```

In the web UI, open **Compare harnesses**, choose **New eval**, pick a profile, the harnesses,
and any checks, and the results appear as one column per harness, best first.

## Scoring

- **Deterministic checks** decide the main score: `contains`, `not_contains`, `regex`, `exact`
  (all case-insensitive unless `case_sensitive: true`), `max_chars`, and `json` (the reply parses
  as JSON). A harness's check score is the fraction of checks that passed.
- **An optional judge** (`--judge HARNESS --rubric "..."`) is another harness that grades each
  reply 0 to 10 against your rubric, with a one-sentence reason. It is an opinion, reported next to
  the check score and used only to break ties, never blended into it. The judge runs read-only.
- **Ranking**: harnesses that answered, by check score, then judge score, then speed. Harnesses
  that failed (for example not logged in) or are not installed are listed with the reason and
  never stop the others.

## Safety and cost

- Every run uses the normal adapters: projection, prompt delivery, the command-line size guard,
  and approvals. A profile that may edit files or run commands runs one harness at a time so the
  runs cannot collide in your workspace; read-only profiles run concurrently.
- Each harness is a real model call. Keep prompts small, and prefer a read-only profile.

## Spec file

```yaml
profile: tester
prompt: Reply with OK
harnesses: [codex, claude, goose]
checks:
  - type: contains
    value: OK
  - type: max_chars
    value: 5
judge:
  harness: claude
  rubric: The best reply is exactly OK with nothing else.
timeout_seconds: 600
```

Results are saved in `.merced-ai/evals/<eval-id>.json` with the profile revision and digest they
ran against.
