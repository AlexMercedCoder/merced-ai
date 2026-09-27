"""Contract-test kit for harness adapter plugins.

Run it from your plugin's own test suite::

    from merced_ai.testing.contract import check_harness_spec
    from my_package.merced import SPEC

    def test_merced_contract(tmp_path):
        check_harness_spec(SPEC, tmp_path)

It never runs the harness. It builds invocations against a placeholder executable and checks the
rules every adapter must follow: no shell, the declared prompt channel really carries the
prompt, long prompts stay off the command line unless the channel is ``argv``, denied
permissions still build, normalization never raises, and the projection is labeled honestly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from merced_ai.harnesses.adapters.command import ARGV_LIMIT_POSIX, CommandHarnessAdapter, argv_size
from merced_ai.harnesses.api import ADAPTER_API_VERSION, HarnessSpec, InvocationContext
from merced_ai.models import PromptDelivery, RunRequest
from merced_ai.profiles import create_profile

ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SHELLS = {"sh", "bash", "zsh", "cmd", "cmd.exe", "powershell", "pwsh"}
MARKER = "MERCED-CONTRACT-PROMPT"


@dataclass
class ContractReport:
    harness_id: str
    checked: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def expect(self, name: str, condition: bool, detail: str) -> None:
        self.checked.append(name)
        if not condition:
            self.failures.append(f"{name}: {detail}")


def _request(
    adapter: CommandHarnessAdapter, workspace: Path, prompt: str, *, deny: bool
) -> RunRequest:
    name = "contract-deny" if deny else "contract"
    path = workspace / ".agents" / f"{name}.agent.yaml"
    if path.exists():
        from merced_ai.profiles import validate_profile

        profile = validate_profile(path, "project")
    else:
        profile = create_profile(
            name,
            "Contract-test profile for adapter authors.",
            "Contract instructions: answer briefly.",
            workspace,
            edit_permission="deny" if deny else None,
            shell_permission="deny" if deny else None,
        )
    return RunRequest(
        harness_id=adapter.descriptor.id,
        prompt=prompt,
        workspace=workspace,
        profile=profile,
        projection=adapter.project_profile(profile),
    )


def run_harness_contract(spec: HarnessSpec, workspace: Path) -> ContractReport:
    """Check a spec and return the report (use ``check_harness_spec`` to assert on it)."""
    workspace.mkdir(parents=True, exist_ok=True)
    adapter = CommandHarnessAdapter(spec)
    descriptor = spec.descriptor
    report = ContractReport(descriptor.id)
    report.expect("api-version", spec.api_version == ADAPTER_API_VERSION, str(spec.api_version))
    report.expect("id", bool(ID_RE.fullmatch(descriptor.id)), f"{descriptor.id!r} is not a slug")
    report.expect("executables", bool(descriptor.executable_names), "no executable names")
    report.expect(
        "subprocess-transport",
        any(item.value.endswith("subprocess") for item in descriptor.transports),
        "a spec is run as a subprocess and must declare a subprocess transport",
    )

    executable = workspace / "bin" / (descriptor.executable_names[0] or "harness")
    # Builds run against a placeholder executable; nothing is spawned.
    if descriptor.executable_names:
        for deny in (False, True):
            label = "denied" if deny else "default"
            scratch = workspace / f".contract-{label}"
            scratch.mkdir(exist_ok=True)
            invocation = spec.build(
                InvocationContext(
                    _request(adapter, workspace, f"{MARKER} short", deny=deny), executable, scratch
                )
            )
            argv = invocation.argv
            report.expect(f"{label}:argv-strings", all(isinstance(a, str) for a in argv), "")
            report.expect(f"{label}:executable-first", argv[:1] == [str(executable)], str(argv[:1]))
            report.expect(
                f"{label}:no-shell",
                Path(argv[0]).name not in SHELLS and "-c" not in argv[1:2],
                "adapters must not go through a shell",
            )
            report.expect(
                f"{label}:declared-delivery",
                invocation.prompt_delivery == descriptor.prompt_delivery,
                f"built {invocation.prompt_delivery}, descriptor says {descriptor.prompt_delivery}",
            )
            files = "".join(
                path.read_text(encoding="utf-8") for path in scratch.iterdir() if path.is_file()
            )
            joined = "\n".join(argv)
            channel = {
                PromptDelivery.ARGV: joined,
                PromptDelivery.STDIN: invocation.stdin or "",
                PromptDelivery.FILE: files,
            }[invocation.prompt_delivery]
            report.expect(f"{label}:prompt-arrives", MARKER in channel, "prompt missing")
            if invocation.prompt_delivery is not PromptDelivery.ARGV:
                report.expect(f"{label}:prompt-off-argv", MARKER not in joined, "prompt on argv")
            if invocation.prompt_delivery is not PromptDelivery.STDIN:
                report.expect(f"{label}:stdin-unused", invocation.stdin is None, "stray stdin")

        if descriptor.prompt_delivery is not PromptDelivery.ARGV:
            scratch = workspace / ".contract-large"
            scratch.mkdir(exist_ok=True)
            large = spec.build(
                InvocationContext(
                    _request(adapter, workspace, MARKER + "x" * 1_000_000, deny=False),
                    executable,
                    scratch,
                )
            )
            report.expect(
                "large-prompt-off-argv",
                argv_size(large.argv) < ARGV_LIMIT_POSIX,
                f"argv is {argv_size(large.argv)} bytes for a 1 MB prompt",
            )

    for sample in ("", "not json", '{"result": "ok"}', "{broken"):
        try:
            output, raw, session = adapter.normalize(sample)
            report.expect("normalize-types", isinstance(output, str), repr(output))
        except Exception as error:
            report.expect("normalize-no-raise", False, f"{sample!r}: {error}")
    projection = adapter.project_profile(_request(adapter, workspace, "x", deny=False).profile)
    report.expect(
        "projection-label",
        projection.support_level in {"native", "projected", "degraded"},
        projection.support_level,
    )
    report.expect(
        "projection-adjustments",
        projection.support_level == "native" or bool(projection.adjustments),
        "non-native projections must say what was mapped, narrowed, or dropped",
    )
    return report


def check_harness_spec(spec: HarnessSpec, workspace: Path) -> ContractReport:
    """Assert that a spec satisfies the adapter contract; returns the report on success."""
    report = run_harness_contract(spec, workspace)
    if report.failures:
        raise AssertionError(
            f"{report.harness_id} fails the Merced AI adapter contract:\n  "
            + "\n  ".join(report.failures)
        )
    return report
