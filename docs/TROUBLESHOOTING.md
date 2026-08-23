# Troubleshooting

## Start with inventory

```bash
merced-ai harness list --json
merced-ai doctor
merced-ai status
```

- `not_installed`: no executable was found. Check PATH or set an explicit detection override.
- `probe_failed`: the executable was found but its bounded version command failed or timed out.
- `installed`: discovery is ready; authentication and live provider readiness are still unverified.

## Command exists in a shell but Merced cannot find it

GUI applications, services, and IDEs often inherit a different PATH. Pin the executable:

```bash
export MERCED_AI_OPENCLAW_PATH="$HOME/.openclaw/bin/openclaw"
```

Or add one or more directories with `MERCED_AI_HARNESS_PATHS`. Use `:` on Unix and `;` on Windows.

## Anton fails with `ModuleNotFoundError: httpx`

Repair the isolated uv tool environment:

```bash
uv tool install --force --with httpx anton-agent
anton version
```

Merced's Anton bridge sends the projected profile and request as one atomic REPL turn. Older Merced
builds sent multiline input as separate turns, which could apply instructions too late.

## DSH asks for `DEEPSEEK_API_KEY`

DSH is still using its default `deepseek-official` route. Configure `agent-default-model` and an
`llm-pi-ai` provider in `$DSH_HOME/settings.yaml`; see [configuration](CONFIGURATION.md).

## Gemini requires a Google Cloud project

A cached Google account is selected instead of API-key authentication. Either configure
`GOOGLE_CLOUD_PROJECT`, select Gemini API key authentication, or qualify with an isolated Gemini
home/configuration. If the default router reports a retired model, set a current model explicitly.

## Pi reports `Model is unavailable`

The selected default provider route is stale. List available models and put an explicit qualified
model in the OAP profile, for example `google/gemini-3.5-flash`.

## Kimi has no default model or provider

Create a Kimi config and point Merced at it with `MERCED_AI_KIMI_CONFIG_FILE`. Keep secrets in
standard provider environment variables. Merced supplies plan mode automatically.

## OpenClaw asks for a session target

Current Merced builds invoke `openclaw agent --local --agent main`. If an older adapter uses
`agent exec`, upgrade Merced. Confirm the configured default with `openclaw models status`.

## Safe diagnostic capture

Prefer metadata and redacted output:

```bash
merced-ai harness list --json > harness-inventory.json
merced-ai ask BOT 'Return exactly DIAGNOSTIC_OK. Do not use tools.' --dry-run --explain
```

Review files before sharing them. Harness logs may contain prompts, workspace paths, account IDs,
or provider-generated diagnostic metadata even when API keys are redacted.
