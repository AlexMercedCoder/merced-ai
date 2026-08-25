# Configuration

## Workspace layout

```text
.agents/                    Open Agent Profile documents
.merced-ai/bots/            profile-to-harness bindings
.merced-ai/sessions/        normalized local conversation sessions
```

Sessions may contain one bot or an ordered group. Group records pin profile/spec digests and the
routed harness separately for every participant; assistant turns record their `bot_name` and
`harness_id`. Existing single-bot session files are upgraded in memory when read and remain
compatible. See [Group conversations](GROUP_CHAT.md).

Optional `title` labels a conversation. Optional `derived_from` links a new participant selection
to its source session without copying or mutating the source transcript.

User-global Merced AI data follows the platform configuration directory. Set `MERCED_AI_HOME` to
an explicit directory for automation, tests, or portable installations.

## Profiles and bots

Create and validate a profile:

```bash
merced-ai profile create reviewer \
  --description 'Reviews code without editing it.' \
  --instructions 'Report verified defects. Do not modify files.'
merced-ai profile validate .agents/reviewer.agent.yaml
```

Bind it to a preferred harness and fallback:

```bash
merced-ai bot create reviewer --profile reviewer --harness codex --fallback claude
merced-ai ask reviewer 'Review the current diff' --dry-run --explain
```

Fallback is attempted only when a harness is unavailable. Merced AI does not replay a failed paid
or potentially mutating request through another harness.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `MERCED_AI_HOME` | Override user-global Merced AI storage. |
| `MERCED_AI_HARNESS_PATHS` | Add OS-path-separated executable search directories. |
| `MERCED_AI_<HARNESS>_PATH` | Pin one harness executable, for example `MERCED_AI_PRIME_AGENT_PATH`. |
| `MERCED_AI_KIMI_CONFIG_FILE` | Pass an alternate Kimi TOML/JSON configuration file. |
| `OPENCLAW_WORKSPACE_DIR` | Set by the OpenClaw adapter to the selected Merced workspace. |

Provider keys remain owned by harnesses. Merced AI inherits the process environment but never
writes provider keys into profiles, bot bindings, or session documents.

## DSH with another provider

DSH's bundled `llm-pi-ai` adapter can route to catalog providers or declared OpenAI-compatible
gateways. Example `$DSH_HOME/settings.yaml`:

```yaml
agent-default-model:
  provider: openai
  model: gpt-5.4

llm-pi-ai:
  providers:
    openai:
      apiKeyEnv: OPENAI_API_KEY
```

`apiKeyEnv` stores only the variable name. Replace `openai`, the model, and the referenced variable
with another route supported by the installed pi-ai catalog.

## Kimi with an existing provider

Kimi Code supports multiple provider types. Keep the secret blank in a test config and let its
standard provider variable override it at runtime:

```toml
default_model = "gpt-5.4"
default_plan_mode = true
telemetry = false

[providers.openai]
type = "openai_responses"
base_url = "https://api.openai.com/v1"
api_key = ""

[models."gpt-5.4"]
provider = "openai"
model = "gpt-5.4"
max_context_size = 400000
capabilities = ["thinking"]
```

```bash
export MERCED_AI_KIMI_CONFIG_FILE=/path/to/kimi-openai.toml
export OPENAI_API_KEY='...'
```

The Kimi adapter always adds plan mode for the MVP because Kimi print mode otherwise auto-approves
tool calls.

## OpenClaw

Merced uses OpenClaw's embedded local agent and pins its workspace through
`OPENCLAW_WORKSPACE_DIR`. Provider/model configuration remains in OpenClaw. A bot profile model is
passed as `provider/model` when present; otherwise OpenClaw's configured default is used.

## Gemini and Pi

Gemini cached-account authentication can take precedence over `GEMINI_API_KEY`. Select API-key
authentication in Gemini or use an isolated configuration for automation. Explicitly select a
currently available model in the OAP profile when a harness default has been retired.

Pi accepts qualified multi-provider models such as `google/gemini-3.5-flash`. Its user state can be
isolated with `PI_CODING_AGENT_DIR` during qualification.
