# Installation

## Requirements

- Python 3.11–3.14.
- At least one supported harness installed and authenticated for live runs.
- A terminal capable of launching subprocesses. The optional UI additionally needs a modern web
  browser.

Merced AI does not install or authenticate harnesses. It discovers their command-line entry points
and delegates execution to them.

## Install Merced AI

From PyPI after publication:

```bash
python -m pip install merced-ai
```

For an isolated CLI installation:

```bash
pipx install merced-ai
# or
uv tool install merced-ai
```

Install the optional local web UI:

```bash
python -m pip install 'merced-ai[webui]'
```

Development checkout:

```bash
git clone https://github.com/AlexMercedCoder/merced-ai.git
cd merced-ai
python -m pip install -e '.[dev]'
```

## Verify the installation

```bash
merced-ai --version
merced-ai harness list
merced-ai doctor
```

`installed` means an executable was found and its bounded version probe succeeded. It does not
prove authentication, provider quota, model availability, or tool permissions. Run a disposable
no-tool bot request to qualify those boundaries.

## Platform notes

### Linux

PATH discovery is tried first. Merced AI also checks common user bins such as `~/.local/bin`,
`~/.cargo/bin`, `~/.npm-global/bin`, and rootless private prefixes such as `~/.openclaw/bin`.

### macOS

PATH discovery is preferred. `/opt/homebrew/bin` and `/usr/local/bin` are checked as bounded
fallbacks for Apple Silicon/Homebrew and Intel/local installations.

### Windows

Python's PATHEXT-aware executable lookup supports `.exe`, `.cmd`, and `.bat` launchers. Additional
fallbacks cover npm's roaming bin, Scoop shims, and Chocolatey bin when their standard environment
variables are present. PowerShell users separate custom search directories with `;`.

### Containers and GUI-launched applications

A GUI or service may inherit a smaller PATH than an interactive shell. Prefer explicit overrides:

```bash
export MERCED_AI_CODEX_PATH=/absolute/path/to/codex
export MERCED_AI_HARNESS_PATHS=/extra/bin:/another/bin
```

On Windows PowerShell:

```powershell
$env:MERCED_AI_CODEX_PATH = 'C:\Tools\codex.cmd'
$env:MERCED_AI_HARNESS_PATHS = 'C:\Tools;D:\AgentBins'
```

See [executable detection](DETECTION.md) for precedence and every supported override.
