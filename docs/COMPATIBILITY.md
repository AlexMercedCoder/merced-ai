# Harness compatibility

This matrix separates executable discovery, adapter contract tests, and authenticated live runs.
It was last updated on 2026-08-27.

| Harness | Adapter | Contract-tested | Installed here | Live-qualified |
| --- | --- | --- | --- | --- |
| Codex | yes | yes | yes | yes |
| Claude Code | yes | yes | yes | yes |
| Gemini CLI | yes | yes | yes | yes, API-key auth + `gemini-3.5-flash` |
| OpenCode | yes | yes | yes | yes |
| Goose | yes | yes | yes | yes |
| Loro | yes | yes | yes | yes |
| MagAgent | yes | yes | yes | yes |
| Anton | yes | yes | yes | yes, repaired uv environment + atomic REPL bridge |
| DeepSeek Harness (DSH) | yes | yes | yes | yes, OpenAI via `llm-pi-ai` |
| Antigravity CLI (AGY) | yes | yes | yes | yes |
| Pi Coding Agent | yes | yes | yes | yes, explicit Google model |
| Prime Agent | yes | yes | yes | yes |
| OpenClaw | yes | yes | yes | yes, embedded OpenAI agent |
| Kimi Code CLI | yes | yes | yes | yes, OpenAI Responses provider |

"Contract-tested" means Merced AI tests argv construction, OAP projection, permission narrowing,
bounded subprocess behavior, and structured output/error parsing using controlled executables. It
does not imply that provider credentials, quota, or every native capability is ready.

The 2026-08-23 live qualification used an empty disposable workspace, bounded noninteractive
invocations, exact-token responses, and instructions not to call tools. All fourteen harnesses
completed through Merced AI. Anton initially failed because its isolated uv environment omitted
`httpx`; after reinstalling with that dependency, its first multiline bridge run exposed that REPL
newlines became separate turns. The bridge was changed to one atomic prompt and requalified with a
clean normalized exact-token response and no intermediate tool activity.

## DSH provider routing

DSH does not require its default DeepSeek provider. Its bundled `llm-pi-ai` adapter can expose
catalog providers and OpenAI-compatible gateways. The following `$DSH_HOME/settings.yaml` selects
OpenAI without storing the key:

```yaml
agent-default-model:
  provider: openai
  model: gpt-5.4

llm-pi-ai:
  providers:
    openai:
      apiKeyEnv: OPENAI_API_KEY
```

The same mechanism can reference other provider-specific environment variables supported by the
installed pi-ai catalog. A configured `apiKeyEnv` is resolved for every request and fails clearly
when the named variable is absent.

## Kimi custom providers

Kimi Code CLI supports OpenAI, Anthropic, Google GenAI, and OpenAI-compatible provider
configurations in addition to Kimi services. Merced AI accepts an alternate config path through
`MERCED_AI_KIMI_CONFIG_FILE`; provider secrets can remain blank in that file and be supplied by the
provider's standard environment variable at runtime. Qualification used OpenAI Responses with
`OPENAI_API_KEY` and forced plan mode.

Loro 0.17.0 and MagAgent 0.99.0 consume canonical OAP 1.0 documents through the 1.0.1 support
library. Merced AI still treats their profile-name handoff as native only when the selected profile
is discoverable in the target project; it does not infer that either harness granted every
requested capability.

## Model-family routing

GLM is not modeled as a harness. Z.AI documents GLM Coding Plan support through existing coding
tools, including Claude Code and OpenCode. Merced AI therefore routes GLM profiles through those
harness adapters (or another configured multi-provider harness) instead of pretending a distinct
GLM CLI exists.

Kimi has both a model family and a dedicated harness. Multi-provider harnesses can route to Kimi
models, while the `kimi` adapter targets Kimi Code CLI print mode directly. The MVP forces the
dedicated Kimi adapter into plan mode because Kimi print mode otherwise uses unattended approval
semantics.

## Upstream references

- [OpenClaw headless agent execution](https://docs.openclaw.ai/cli/agent)
- [Kimi Code CLI print mode](https://moonshotai.github.io/kimi-cli/en/customization/print-mode.html)
- [Kimi CLI command reference](https://moonshotai.github.io/kimi-cli/en/reference/kimi-command.html)
- [Kimi provider configuration](https://moonshotai.github.io/kimi-cli/en/configuration/providers.html)
- [Z.AI OpenCode integration](https://docs.z.ai/devpack/tool/opencode)
- [Z.AI Claude Code integration](https://docs.z.ai/devpack/tool/claude)
- [Z.AI supported coding-tool helper](https://docs.z.ai/devpack/extension/coding-tool-helper)
