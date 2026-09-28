# Harness compatibility

This matrix separates executable discovery, adapter contract tests, and authenticated live runs.
It was last updated on 2026-08-27. The 2026-09-27 smoke results for the stdin prompt delivery
added in 0.8.0 are in [validation](MVP_VALIDATION.md#results-2026-09-27-linux); they supersede the
live column below for Claude Code, Codex, Gemini CLI, Goose, OpenCode, MagAgent, and Loro.

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

## Agent Client Protocol (ACP)

When a harness's ACP launcher is installed, Merced AI runs it as an ACP agent instead of a
one-shot subprocess. Verified end to end on 2026-09-27 (initialize, session, a streamed "Reply
with OK", and a second turn):

| Harness | ACP launcher | Streaming | Approvals through Merced AI | Native resume |
| --- | --- | --- | --- | --- |
| Claude Code | `claude-agent-acp` 0.79 | yes | yes | yes (`session/load`) |
| Gemini CLI | `gemini --acp` 0.57 | yes | yes | no: `session/load` fails in a new process, so the transcript is replayed |
| Goose | `goose acp` 1.48 | yes | yes | yes |
| OpenCode | `opencode acp` 1.18 | yes | yes | yes |
| Codex | `codex-acp` | opt-in only | | not verified: it started sessions on a model this account cannot use (HTTP 400) |
| Kimi, Prime Agent | `kimi acp`, `prime-agent --mode acp` | opt-in only | | not verified here |

Opt in to an unverified launcher with `MERCED_AI_ACP_EXPERIMENTAL=codex,kimi`, or turn ACP off
entirely with `MERCED_AI_ACP=0` (every harness then uses its subprocess adapter). Under ACP:

- replies stream into the web UI and the terminal as the agent writes them;
- permission requests are shown in the web approval dialog (or asked on the terminal in
  `merced-ai ask`/`chat`) and the decision is mapped to the agent's own allow/reject options;
  without anyone to ask (`--json`, group CLI turns, no TTY) they are rejected;
- a profile that denies both editing and shell access selects the agent's read-only mode
  (`plan`/`chat`), and requests of a denied kind are rejected without asking;
- Merced AI never selects an auto-approve mode and refuses an agent that offers only one;
- the model requested by the profile is not passed; the harness's configured model is used.

## What Merced AI delivers per harness

"Supported" in this document means Merced AI can run the harness. It does not mean Merced AI uses
every feature the harness offers. The broker-implemented set is:

| Harness | Merced AI delivers | Harness offers but Merced AI does not use yet |
| --- | --- | --- |
| MagAgent, Loro | one-shot runs, native OAP profile by name, AAIS approval relay, inlined context files, WebMCP routing when verified | streaming, native session resume, model listing |
| Claude Code, Goose, OpenCode over ACP | streaming, approvals relayed, native session resume, inlined context files | model listing |
| Gemini CLI over ACP | streaming, approvals relayed, inlined context files | native session resume, model listing |
| All other adapters | one-shot runs, inlined context files | streaming, native session resume, native approvals, native attachments, model listing (as each harness documents) |

For the subprocess adapters, responses appear when the harness process exits and each turn starts
a fresh process that replays up to the last twenty turns as a transcript.

"Contract-tested" means Merced AI tests argv construction, OAP projection, permission narrowing,
bounded subprocess behavior, and structured output/error parsing using controlled executables. It
does not imply that provider credentials, quota, or every native capability is ready.

The 2026-08-23 live qualification used an empty disposable workspace, bounded noninteractive
invocations, exact-token responses, and instructions not to call tools. All fourteen harnesses
completed through Merced AI. Anton initially failed because its isolated uv environment omitted
`httpx`; after reinstalling with that dependency, its first multiline bridge run exposed that REPL
newlines became separate turns. The bridge was changed to one atomic prompt and requalified with a
clean normalized exact-token response and no intermediate tool activity.

## Prompt delivery

Merced AI sends the prompt through stdin or a private temporary file wherever the harness accepts
one, and keeps it on the command line only when there is no other input. Each mechanism below was
confirmed from the installed harness's own help output or source on 2026-09-27. The live smoke
suite (`MERCED_AI_LIVE_SMOKE=1`, see [validation](MVP_VALIDATION.md)) then ran the exact flags
end to end for Claude Code, Codex, Gemini CLI, Goose, OpenCode, and MagAgent; the Pi, Prime Agent,
OpenClaw, Kimi, DSH, and AGY mechanisms are confirmed from help or source only.

| Harness | Prompt | Profile or system prompt | Evidence |
| --- | --- | --- | --- |
| Codex 0.155 | stdin via `codex exec -` | prefixed into stdin | `codex exec --help`: "If not provided as an argument (or if `-` is used), instructions are read from stdin" |
| Claude Code 2.1 | stdin with `--print` | `--system-prompt-file` (private file) | `claude --help` names `--system-prompt[-file]`; `--print` with no prompt argument reads stdin |
| Gemini CLI 0.57 | stdin (headless when stdin is not a terminal) | prefixed into stdin | `gemini --help`: `-p` is "Appended to input on stdin" |
| OpenCode 1.18 | stdin | prefixed into stdin | piped stdin is accepted as the message by `opencode run` |
| Goose 1.48 | stdin via `--instructions -` | `--system` argument (no file variant) | `goose run --help`: "Use - for stdin" |
| Pi 0.85, Prime Agent 0.8 | piped stdin | `--append-system-prompt <file>` | source: `readPipedStdin()` and `resolvePromptInput()` read a path when it exists |
| OpenClaw 2026.9 | `--message-file` (private file, 4 MiB max) | prefixed into the file | `openclaw agent --help` |
| Kimi Code CLI 1.49 | stdin in print mode | prefixed into stdin | source: print mode reads stdin when `--prompt` is absent |
| Anton | stdin REPL turn | prefixed into stdin | existing REPL bridge |
| Loro 0.22.0+ (`run --prompt-file`) | private file | native profile by name, or prefixed | detected from `loro run --help` and cached per executable; `--json` output is used when present |
| MagAgent 1.4.0+ (`ask --prompt-file`) | private file | native profile by name, or prefixed | detected from `magent ask --help` and cached per executable |
| MagAgent before 1.4.0, Loro before 0.22.0 | argument | native profile by name, or prefixed | stdin carries AAIS approval envelopes; these versions take the task only as an argument |
| DSH 0.1.5 | argument | prefixed | the headless profile takes its task from the command line only |
| Antigravity (AGY) 1.1 | `--print=` argument | prefixed | print mode reads stdin only as `stream-json`; plain-text stdin is not confirmed |

For the argument-only routes, Merced AI measures the full command line before starting the process
and refuses anything over 100 KB on POSIX or 24 KB on Windows (where `.cmd` launchers go through
`cmd.exe`). The error names the limit and exits with status 7. Goose's system prompt is also an
argument, so an unusually long profile is subject to the same guard.

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
