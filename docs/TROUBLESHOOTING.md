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

## "The prompt is too large to pass to ... on the command line" (exit status 7)

MagAgent versions without `ask --prompt-file`, Loro versions without `run --prompt-file`, DSH,
and Antigravity accept the prompt only
as a command-line argument, so Merced
AI refuses to build a command line over 100 KB (24 KB on Windows) rather than let the operating
system reject it. Long conversations replay up to twenty turns, and attached context files can add
up to 750 KB, so this usually means the context is too large for that harness. Remove attached
files, start a new conversation, or route the bot to a harness that reads prompts from stdin
(`merced-ai harness show HARNESS` prints its prompt delivery).

## Gemini says the directory is not trusted

Gemini CLI refuses headless runs in a folder it has not been told to trust. Merced AI does not
bypass that check. Trust the project once in an interactive `gemini` session, or set
`GEMINI_CLI_TRUST_WORKSPACE=true` in the environment you start Merced AI from if you accept that
for every folder.

## "Approval state was reset" banner

`.merced-ai/aais-presenter.json` could not be parsed, so Merced AI moved it to
`aais-presenter.corrupt-<timestamp>.json` next to it and started with empty approval state. Nothing
was approved; a harness that was waiting asks again or times out. Inspect or delete the kept file
once you no longer need it. If this repeats, check for another tool writing into `.merced-ai/`.

## Safe diagnostic capture

Prefer metadata and redacted output:

```bash
merced-ai harness list --json > harness-inventory.json
merced-ai ask BOT 'Return exactly DIAGNOSTIC_OK. Do not use tools.' --dry-run --explain
```

Review files before sharing them. Harness logs may contain prompts, workspace paths, account IDs,
or provider-generated diagnostic metadata even when API keys are redacted.
