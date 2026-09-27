# Harness adapter plugins

Merced AI talks to each harness through an adapter. Since 0.8.0 every built-in adapter is written
against the same public API that third-party adapters use, so adding a harness no longer means
editing Merced AI itself.

## Design

- **A spec, not a subclass.** An adapter is a `HarnessSpec`: a descriptor (ID, name, executable,
  prompt delivery, and the two capability sets described in [Architecture](ARCHITECTURE.md)), a
  `build(ctx)` function that returns one invocation, a projection style, and an output format.
  The generic runner owns everything that must be the same for every harness: executable
  discovery, a private per-run temp directory, the command-line size guard, bounded capture,
  cancellation of the whole process tree, the AAIS approval channel, and error reporting. A
  plugin therefore cannot skip those protections by accident.
- **Entry points for discovery.** Installed packages register adapters in the
  `merced_ai.harnesses` entry-point group. Plugins cannot replace a built-in or an earlier plugin;
  a plugin that fails to import, has the wrong API version, or returns the wrong type is skipped
  and reported by `merced-ai harness list` and `merced-ai doctor` instead of breaking the CLI.
- **A contract kit instead of trust.** `merced_ai.testing.contract.check_harness_spec` checks the
  rules the runner cannot enforce by itself. All fourteen built-in adapters pass it in CI.
- **Versioned.** The API is version 1 (`merced_ai.harnesses.api.ADAPTER_API_VERSION`). A spec
  declares the version it was written for; a mismatch is refused with a clear message.

## Writing an adapter

```python
# my_package/merced.py
from merced_ai.harnesses.api import HarnessInvocation, HarnessSpec, InvocationContext
from merced_ai.harnesses.builtin import descriptor
from merced_ai.models import PromptDelivery


def build(ctx: InvocationContext) -> HarnessInvocation:
    command = [str(ctx.executable), "run", "--json"]
    if ctx.edit_denied:
        command.append("--read-only")
    if ctx.model:
        command += ["--model", ctx.model]
    # Prefer stdin or a private file; use PromptDelivery.ARGV only if the CLI has no other input.
    return HarnessInvocation(command, PromptDelivery.STDIN, stdin=ctx.prefixed_prompt)


SPEC = HarnessSpec(
    descriptor("my-harness", "My Harness", "my-harness"),
    build,
    projection="prefixed",  # or "system_prompt", or "native"
    output="json",  # or "text", or a function stdout -> (reply, payload, session_id)
)
```

```toml
# pyproject.toml of the plugin package
[project.entry-points."merced_ai.harnesses"]
my-harness = "my_package.merced:SPEC"
```

The entry point may also point at a zero-argument function that returns a spec, or at a complete
adapter object implementing `merced_ai.harnesses.base.HarnessAdapter` (for transports other than
one subprocess per turn).

`InvocationContext` gives the build function the request, the resolved executable, the profile's
system prompt and the prefixed prompt, the projected model, permission helpers (`edit_denied`,
`shell_denied`), `native_profile`, and `private_file(name, content)` for files the harness should
read (mode `0600`, removed after the run). `env=` adds environment variables for the child, and
`aais_control=True` declares that the harness speaks AAIS 1.0 on stdout/stdin, in which case
stdin is not available as a prompt channel.

## Testing an adapter

```python
from merced_ai.testing.contract import check_harness_spec
from my_package.merced import SPEC


def test_merced_contract(tmp_path):
    check_harness_spec(SPEC, tmp_path)
```

The kit never starts the harness. It checks that the argv is a list of strings starting with the
executable and not a shell, that the declared prompt channel really carries the prompt (and the
others do not), that a 1 MB prompt keeps the command line under the size limit unless the
channel is `argv`, that a profile with edit and shell denied still builds, that normalization
never raises on empty or broken output, and that non-native projections list their adjustments.

For a live check against the installed harness, add it to your own smoke test the way
`tests/test_live_smoke.py` does for the built-ins.

## Operating plugins

- `merced-ai harness show HARNESS` prints whether the adapter is built in or which package
  provided it.
- `MERCED_AI_DISABLE_PLUGINS=1` loads only the built-in adapters.
- Plugins run in the Merced AI process with your permissions. Install them only from sources you
  trust, as you would the harnesses themselves.
- A distribution that sits in the current working directory (for example a `*.dist-info` folder
  checked into a project) is ignored and reported, so opening a project cannot register a plugin.
  `python -m merced_ai` also leaves the working directory off `sys.path`.
