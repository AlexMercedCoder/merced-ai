# Executable detection

## Assessment

The original MVP logic was reliable for commands already on PATH and a narrow Linux rootless
layout, but it was not robust enough to claim cross-operating-system coverage. It assumed `HOME`,
manually checked Unix executable bits, did not apply Windows PATHEXT to fallback directories, and
had no supported escape hatch for GUI/service PATH differences.

The current implementation addresses those gaps without scanning the filesystem or executing a
shell.

## Discovery order

For each known harness, Merced AI tries:

1. `MERCED_AI_<HARNESS>_PATH`, with non-alphanumeric harness ID characters converted to
   underscores—for example `MERCED_AI_PRIME_AGENT_PATH`.
2. Every descriptor executable name through the process PATH using `shutil.which`.
3. Directories in `MERCED_AI_HARNESS_PATHS`, split with the operating system's path separator.
4. Tool-prefix environment variables: `UV_TOOL_BIN_DIR` and `NPM_CONFIG_PREFIX`.
5. Bounded user bins: `~/.local/bin`, `~/.cargo/bin`, `~/.npm-global/bin`, and
   `~/.<executable>/bin`.
6. Windows environment-backed locations: `%APPDATA%\npm`, `%SCOOP%\shims`, and
   `%ChocolateyInstall%\bin`.
7. On macOS, `/opt/homebrew/bin` and `/usr/local/bin`.

Every directory fallback is still resolved with `shutil.which`, preserving Unix executable checks
and Windows PATHEXT behavior. Returned paths are resolved before execution.

## Probe behavior

- Probes use a fixed descriptor command such as `--version` or Anton's `version`.
- `shell=False` is always requested.
- Each probe is bounded to three seconds.
- Output is decoded as UTF-8 with replacement for malformed native bytes and capped at 500
  characters.
- Probe failures are isolated per harness and reported as `probe_failed`; they do not abort the
  inventory.
- Discovery never reads credentials, searches an entire drive, or runs installation commands.

## Cross-OS coverage

| Area | Linux | macOS | Windows |
| --- | --- | --- | --- |
| PATH lookup | yes | yes | yes, PATHEXT-aware |
| User-local bins | yes | yes | yes where environment-backed |
| npm launchers | PATH/prefix | PATH/prefix | roaming npm bin |
| Rust/Cargo bins | yes | yes | usually PATH or explicit override |
| Rootless OpenClaw | private prefix | private prefix | explicit path if installer differs |
| Package-manager fallbacks | bounded user bins | Homebrew defaults | Scoop and Chocolatey |
| Explicit executable override | yes | yes | yes |

CI is configured to exercise the same unit suite on Ubuntu, macOS, and Windows. The tests simulate per-harness
overrides, OS-separated extra paths, Windows package-manager directories, private rootless prefixes,
PATHEXT-preserving lookup calls, malformed output handling, and shell-free probes.

## Remaining limits

- Merced AI deliberately does not recursively scan application directories or the full filesystem.
- Unknown custom installer layouts require an explicit path or search directory.
- A successful version probe does not establish authentication, network reachability, model
  availability, or capability correctness.
- Windows `.cmd`/`.bat` launchers are governed by Windows process semantics even with
  `shell=False`; use trusted harness installations and prefer native executables where available.
- Harness CLI flags can change between versions. Live qualification and adapter contract tests are
  both required before a release can claim compatibility.
