"""The fourteen built-in harness adapters, each expressed through the public plugin API.

Every prompt-delivery choice below is documented with the harness help or source that confirms
it in docs/COMPATIBILITY.md ("Prompt delivery").
"""

from __future__ import annotations

import os

from merced_ai.harnesses.api import (
    HarnessInvocation,
    HarnessSpec,
    InvocationContext,
    profile_provider,
    qualified_model,
)
from merced_ai.harnesses.output import normalize_loro, normalize_repl
from merced_ai.models import (
    HarnessCapabilities,
    HarnessDescriptor,
    PromptDelivery,
    TransportKind,
)

STDIN, FILE, ARGV = PromptDelivery.STDIN, PromptDelivery.FILE, PromptDelivery.ARGV

# What the harnesses document for their own interactive or protocol surfaces. Merced AI records
# these for reference only; it does not use them yet.
NATIVE_SESSION = HarnessCapabilities(
    streaming=True, resume=True, approvals=True, attachments=True, model_listing=True
)
NATIVE_OAP = NATIVE_SESSION.model_copy(update={"native_oap": True, "webmcp": True})

# What Merced AI delivers end to end through a noninteractive subprocess adapter: output arrives
# when the run completes, each turn starts a fresh harness process with a bounded transcript, and
# selected workspace files are inlined into the prompt as context.
BROKER_SUBPROCESS = HarnessCapabilities(attachments=True)

# MagAgent and Loro additionally relay AAIS approvals over stdio, receive project OAP profiles
# natively, and can satisfy WebMCP routing once their readiness report is verified.
BROKER_AAIS_NATIVE = BROKER_SUBPROCESS.model_copy(
    update={"approvals": True, "native_oap": True, "webmcp": True}
)


def descriptor(
    harness_id: str,
    name: str,
    executable: str,
    *,
    delivery: PromptDelivery = STDIN,
    transports: tuple[TransportKind, ...] = (TransportKind.STRUCTURED_SUBPROCESS,),
    harness_supports: HarnessCapabilities = NATIVE_SESSION,
    broker_implements: HarnessCapabilities = BROKER_SUBPROCESS,
    version_args: tuple[str, ...] = ("--version",),
) -> HarnessDescriptor:
    return HarnessDescriptor(
        id=harness_id,
        name=name,
        executable_names=(executable,),
        transports=transports,
        version_args=version_args,
        harness_supports=harness_supports,
        broker_implements=broker_implements,
        prompt_delivery=delivery,
    )


def _with_model(command: list[str], model: str | None, flag: str = "--model") -> list[str]:
    if model:
        command.extend((flag, model))
    return command


def build_codex(ctx: InvocationContext) -> HarnessInvocation:
    sandbox = "read-only" if ctx.edit_denied else "workspace-write"
    command = [str(ctx.executable), "exec", "--color", "never", "--skip-git-repo-check"]
    command += ["-C", str(ctx.workspace), "-s", sandbox]
    _with_model(command, ctx.model)
    # `codex exec -` reads the whole prompt from stdin.
    command.append("-")
    return HarnessInvocation(command, STDIN, stdin=ctx.prefixed_prompt)


def build_claude(ctx: InvocationContext) -> HarnessInvocation:
    mode = "plan" if ctx.edit_denied or ctx.shell_denied else "manual"
    system_file = ctx.private_file("system-prompt.md", ctx.system_prompt)
    command = [str(ctx.executable), "--print", "--output-format", "json"]
    command += ["--permission-mode", mode, "--system-prompt-file", str(system_file)]
    _with_model(command, ctx.model)
    # `claude --print` reads the prompt from stdin when no prompt argument is given.
    return HarnessInvocation(command, STDIN, stdin=ctx.prompt)


def build_gemini(ctx: InvocationContext) -> HarnessInvocation:
    command = [str(ctx.executable), "--output-format", "json", "--approval-mode", "default"]
    _with_model(command, ctx.model)
    # Gemini runs headless when stdin is not a terminal and uses stdin as the prompt.
    return HarnessInvocation(command, STDIN, stdin=ctx.prefixed_prompt)


def build_magagent(ctx: InvocationContext) -> HarnessInvocation:
    mode = "paranoid" if ctx.edit_denied or ctx.shell_denied else "balanced"
    # stdin is the AAIS approval channel and `magent ask` takes the task only as an argument,
    # so this route stays on argv and is bounded by the argv guard.
    command = [
        str(ctx.executable),
        "ask",
        ctx.prompt if ctx.native_profile else ctx.prefixed_prompt,
        "--project",
        str(ctx.workspace),
        "--permission-mode",
        mode,
        "--json",
        "--events",
        "--approval-stdio",
    ]
    if ctx.native_profile:
        command.extend(("--agent", ctx.profile.name))
    return HarnessInvocation(command, ARGV)


def build_loro(ctx: InvocationContext) -> HarnessInvocation:
    # Same constraint as MagAgent: stdin carries AAIS envelopes.
    command = [
        str(ctx.executable),
        "run",
        ctx.prompt if ctx.native_profile else ctx.prefixed_prompt,
    ]
    if ctx.native_profile:
        command.extend(("--agent", ctx.profile.name))
    command.append("--approval-stdio")
    return HarnessInvocation(command, ARGV)


def build_opencode(ctx: InvocationContext) -> HarnessInvocation:
    command = [str(ctx.executable), "run", "--format", "json", "--dir", str(ctx.workspace)]
    if ctx.model:
        command.extend(("--model", qualified_model(ctx.profile, ctx.model)))
    # `opencode run` uses piped stdin as the message when no positional is given.
    return HarnessInvocation(command, STDIN, stdin=ctx.prefixed_prompt)


def build_goose(ctx: InvocationContext) -> HarnessInvocation:
    # `--instructions -` reads the request from stdin. The system prompt has no file variant, so
    # it stays on argv and is covered by the argv guard.
    command = [str(ctx.executable), "run", "--instructions", "-", "--system", ctx.system_prompt]
    command += ["--quiet", "--output-format", "json", "--no-session"]
    if provider := profile_provider(ctx.profile):
        command.extend(("--provider", provider))
    _with_model(command, ctx.model)
    if ctx.edit_denied and ctx.shell_denied:
        command.append("--no-profile")
    return HarnessInvocation(command, STDIN, stdin=ctx.prompt)


def build_dsh(ctx: InvocationContext) -> HarnessInvocation:
    # The headless profile reads its task only from the command line.
    return HarnessInvocation(
        [str(ctx.executable), "--profile", "headless", ctx.prefixed_prompt], ARGV
    )


def build_agy(ctx: InvocationContext) -> HarnessInvocation:
    # Print mode reads stdin only as stream-json input, which this adapter does not speak yet, so
    # the prompt stays on argv and is bounded by the argv guard.
    command = [str(ctx.executable), f"--print={ctx.prefixed_prompt}", "--output-format", "json"]
    command.append("--disable-slash-commands")
    if ctx.edit_denied or ctx.shell_denied:
        command.extend(("--mode", "plan"))
    _with_model(command, ctx.model)
    return HarnessInvocation(command, ARGV)


def _build_pi_family(ctx: InvocationContext, *, prime: bool) -> HarnessInvocation:
    command = [str(ctx.executable), "--print", "--mode", "json", "--no-session"]
    if prime:
        command.extend(("--cwd", str(ctx.workspace)))
    # `--append-system-prompt` reads the file when the value is an existing path, and print mode
    # uses piped stdin as the initial message.
    system_file = ctx.private_file("system-prompt.md", ctx.system_prompt)
    command.extend(("--append-system-prompt", str(system_file)))
    if prime and (ctx.edit_denied or ctx.shell_denied):
        command.append("--no-tools")
    else:
        excluded = [
            *(("edit", "write") if ctx.edit_denied else ()),
            *(("bash",) if ctx.shell_denied else ()),
        ]
        if excluded:
            command.extend(("--exclude-tools", ",".join(excluded)))
    if ctx.model:
        command.extend(("--model", qualified_model(ctx.profile, ctx.model)))
    return HarnessInvocation(command, STDIN, stdin=ctx.prompt)


def build_pi(ctx: InvocationContext) -> HarnessInvocation:
    return _build_pi_family(ctx, prime=False)


def build_prime_agent(ctx: InvocationContext) -> HarnessInvocation:
    return _build_pi_family(ctx, prime=True)


def build_openclaw(ctx: InvocationContext) -> HarnessInvocation:
    message_file = ctx.private_file("message.md", ctx.prefixed_prompt)
    command = [str(ctx.executable), "agent", "--local", "--agent", "main", "--json"]
    command += ["--message-file", str(message_file)]
    if ctx.model:
        command.extend(("--model", qualified_model(ctx.profile, ctx.model)))
    return HarnessInvocation(command, FILE)


def build_kimi(ctx: InvocationContext) -> HarnessInvocation:
    command = [str(ctx.executable), "--quiet", "--plan", "--work-dir", str(ctx.workspace)]
    if config_file := os.environ.get("MERCED_AI_KIMI_CONFIG_FILE"):
        command.extend(("--config-file", config_file))
    _with_model(command, ctx.model)
    # Print mode reads the command from stdin when `--prompt` is absent.
    return HarnessInvocation(command, STDIN, stdin=ctx.prefixed_prompt)


def anton_turn(ctx: InvocationContext) -> str:
    """Anton's REPL treats each line as a turn, so the whole request is sent as one line."""
    atomic = " ".join(line.strip() for line in ctx.prefixed_prompt.splitlines() if line.strip())
    return atomic + "\nexit\n"


def build_anton(ctx: InvocationContext) -> HarnessInvocation:
    command = [str(ctx.executable), "--folder", str(ctx.workspace), "--no-update"]
    return HarnessInvocation(command, STDIN, stdin=anton_turn(ctx))


GOOGLE = frozenset({"google", "gemini"})

BUILTIN_SPECS: tuple[HarnessSpec, ...] = (
    HarnessSpec(
        descriptor("codex", "Codex", "codex"),
        build_codex,
        compatible_providers=frozenset({"openai"}),
    ),
    HarnessSpec(
        descriptor("claude", "Claude Code", "claude"),
        build_claude,
        projection="system_prompt",
        compatible_providers=frozenset({"anthropic"}),
        output="json",
    ),
    HarnessSpec(
        descriptor("gemini", "Gemini CLI", "gemini"),
        build_gemini,
        compatible_providers=GOOGLE,
        output="json",
    ),
    HarnessSpec(descriptor("opencode", "OpenCode", "opencode"), build_opencode, output="json"),
    HarnessSpec(
        descriptor("goose", "Goose", "goose"),
        build_goose,
        projection="system_prompt",
        output="json",
    ),
    HarnessSpec(
        descriptor(
            "loro",
            "Loro",
            "loro",
            delivery=ARGV,
            harness_supports=NATIVE_OAP,
            broker_implements=BROKER_AAIS_NATIVE,
        ),
        build_loro,
        projection="native",
        output=normalize_loro,
        aais_control=True,
    ),
    HarnessSpec(
        descriptor(
            "magagent",
            "MagAgent",
            "magent",
            delivery=ARGV,
            harness_supports=NATIVE_OAP,
            broker_implements=BROKER_AAIS_NATIVE,
        ),
        build_magagent,
        projection="native",
        output="json",
        aais_control=True,
    ),
    HarnessSpec(
        descriptor(
            "anton",
            "Anton",
            "anton",
            transports=(TransportKind.TEXT_SUBPROCESS,),
            version_args=("version",),
            harness_supports=NATIVE_SESSION.model_copy(update={"model_listing": False}),
        ),
        build_anton,
        output=lambda stdout: normalize_repl(stdout, "anton"),
    ),
    HarnessSpec(
        descriptor(
            "dsh",
            "DeepSeek Harness",
            "dsh",
            delivery=ARGV,
            harness_supports=NATIVE_SESSION.model_copy(update={"attachments": False}),
        ),
        build_dsh,
        compatible_providers=frozenset({"deepseek"}),
    ),
    HarnessSpec(
        descriptor("agy", "Antigravity CLI", "agy", delivery=ARGV),
        build_agy,
        compatible_providers=GOOGLE,
        output="json",
    ),
    HarnessSpec(
        descriptor("pi", "Pi Coding Agent", "pi"),
        build_pi,
        projection="system_prompt",
        output="json",
    ),
    HarnessSpec(
        descriptor("prime-agent", "Prime Agent", "prime-agent"),
        build_prime_agent,
        projection="system_prompt",
        output="json",
    ),
    HarnessSpec(
        descriptor("openclaw", "OpenClaw", "openclaw", delivery=FILE),
        build_openclaw,
        output="json",
        env=lambda ctx: {"OPENCLAW_WORKSPACE_DIR": str(ctx.workspace)},
    ),
    HarnessSpec(
        descriptor("kimi", "Kimi Code CLI", "kimi"),
        build_kimi,
        compatible_providers=frozenset(
            {"moonshot", "kimi", "openai", "anthropic", "google", "gemini"}
        ),
    ),
)

BUILTIN_BY_ID = {spec.descriptor.id: spec for spec in BUILTIN_SPECS}
