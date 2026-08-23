# Release process

## 1. Establish scope

- Confirm the target version and changelog section.
- Review compatibility claims against the exact harness versions tested.
- Ensure breaking profile, bot, session, CLI, or adapter changes are called out.
- Confirm no credentials, qualification state, logs, or temporary configs are tracked.

## 2. Automated validation

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest -q
python -m build
python -m twine check dist/*
```

CI must pass on Ubuntu, macOS, and Windows. Coverage must remain at or above 70%.

## 3. Package smoke test

Install the wheel into a clean environment and avoid importing from the checkout:

```bash
python -m venv /tmp/merced-ai-release-smoke
/tmp/merced-ai-release-smoke/bin/python -m pip install dist/merced_ai-*.whl
/tmp/merced-ai-release-smoke/bin/merced-ai --version
/tmp/merced-ai-release-smoke/bin/merced-ai harness list --json
```

Use the platform-specific virtual-environment script path on Windows.

## 4. Live qualification

- Use an empty disposable workspace.
- Use no-tool exact-token prompts.
- Bound every invocation and preserve per-harness evidence.
- Record harness version, provider/model route, result, duration, and any required configuration.
- Treat a token preceded by unintended tool activity as a failure.
- Never print or persist API key values.

Update [compatibility](COMPATIBILITY.md) and [validation](MVP_VALIDATION.md) with observed—not
inferred—results.

## 5. Release metadata

- Update the version in `pyproject.toml`.
- Finalize `CHANGELOG.md` with the release date.
- Confirm README links and screenshots render.
- Verify repository URLs, license metadata, classifiers, and Python support.
- Commit the release preparation and create an annotated version tag.

## 6. Publish

- Push the commit and tag.
- Create the public GitHub release from the changelog.
- Publish with a scoped PyPI token or trusted publishing.
- Do not upload from a dirty worktree or reuse artifacts built before the final commit.

## 7. Post-release

- Install from PyPI in a clean environment on at least one supported platform.
- Verify `merced-ai --version`, inventory JSON, profile creation, dry-run projection, and UI startup.
- Check GitHub release assets and PyPI metadata.
- Open issues for deferred compatibility gaps rather than weakening the published matrix.
