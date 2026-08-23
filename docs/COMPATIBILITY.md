# Harness compatibility

This matrix separates executable discovery, adapter contract tests, and authenticated live runs.
It was last updated on 2026-08-23.

| Harness | Adapter | Contract-tested | Installed here | Live-qualified |
| --- | --- | --- | --- | --- |
| Codex | yes | yes | yes | yes |
| Claude Code | yes | yes | yes | pending unsandboxed run |
| Gemini CLI | yes | yes | yes | pending unsandboxed run |
| OpenCode | yes | yes | yes | pending unsandboxed run |
| Goose | yes | yes | yes | pending writable log state |
| Loro | yes | yes | yes | pending writable audit state |
| MagAgent | yes | yes | yes | pending writable config/log state |
| Anton | yes | yes | yes | pending a stable noninteractive upstream interface |
| DeepSeek Harness (DSH) | yes | yes | yes | missing DeepSeek credential |
| Antigravity CLI (AGY) | yes | yes | yes | pending local socket permission |
| Pi Coding Agent | yes | yes | yes | pending outbound network permission |
| Prime Agent | yes | yes | yes | pending daemon/socket permission |
| OpenClaw | yes | yes | no | pending installation and authentication |
| Kimi Code CLI | yes | yes | no | pending installation and authentication |

"Contract-tested" means Merced AI tests argv construction, OAP projection, permission narrowing,
bounded subprocess behavior, and structured output/error parsing using controlled executables. It
does not imply that provider credentials, quota, or every native capability is ready.

The installed MagAgent release currently validates some permission values against its own older
dialect (for example, network `none/read/full`) rather than the OAP reference schema values. Basic
profiles work, but portable profiles containing those permission fields require an upstream schema
alignment or a future translation layer before they can be called native-compatible.

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
- [Z.AI OpenCode integration](https://docs.z.ai/devpack/tool/opencode)
- [Z.AI Claude Code integration](https://docs.z.ai/devpack/tool/claude)
- [Z.AI supported coding-tool helper](https://docs.z.ai/devpack/extension/coding-tool-helper)
