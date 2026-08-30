"""File-backed bot bindings that keep local harness choice outside portable OAP profiles."""

from __future__ import annotations

from pathlib import Path

import yaml

from merced_ai.models import BotBinding, BotHarnessPreference
from merced_ai.paths import ensure_project_layout, ensure_user_layout
from merced_ai.profiles import NAME_RE, resolve_profile


class BotError(ValueError):
    pass


def create_bot(
    name: str,
    profile: str,
    harness: str,
    fallbacks: tuple[str, ...],
    workspace: Path,
    *,
    user: bool = False,
) -> BotBinding:
    if not NAME_RE.fullmatch(name):
        raise BotError("bot name must match ^[a-z][a-z0-9-]{0,62}$")
    resolved_profile = resolve_profile(profile, workspace)
    stored_profile = str(resolved_profile.path) if Path(profile).expanduser().is_file() else profile
    source = "user" if user else "project"
    root = (ensure_user_layout() if user else ensure_project_layout(workspace)) / "bots"
    path = root / f"{name}.bot.yaml"
    if path.exists():
        raise BotError(f"bot file already exists: {path}")
    binding = BotBinding(
        name=name,
        profile=stored_profile,
        harness=BotHarnessPreference(preferred=harness, fallbacks=fallbacks),
        workspace=str(workspace.resolve()),
        source=source,
        path=path,
    )
    payload = {
        "apiVersion": binding.api_version,
        "kind": binding.kind,
        "metadata": {"name": binding.name},
        "spec": {
            "profile": binding.profile,
            "harness": {
                "preferred": binding.harness.preferred,
                "fallbacks": list(binding.harness.fallbacks),
            },
            "workspace": binding.workspace,
            "session": {"resume": binding.session.resume},
        },
    }
    _atomic_write(path, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    return binding


def update_bot(
    name: str,
    profile: str,
    harness: str,
    fallbacks: tuple[str, ...],
    workspace: Path,
) -> BotBinding:
    current = resolve_bot(name, workspace)
    project_root = (ensure_project_layout(workspace) / "bots").resolve()
    if current.source != "project" or current.path.parent.resolve() != project_root:
        raise BotError("only project-local bot bindings can be edited")
    previous = current.path.read_text(encoding="utf-8")
    current.path.unlink()
    try:
        return create_bot(name, profile, harness, fallbacks, workspace)
    except Exception:
        _atomic_write(current.path, previous)
        raise


def delete_bot(name: str, workspace: Path) -> None:
    current = resolve_bot(name, workspace)
    project_root = (ensure_project_layout(workspace) / "bots").resolve()
    if current.source != "project" or current.path.parent.resolve() != project_root:
        raise BotError("only project-local bot bindings can be deleted")
    current.path.unlink()


def discover_bots(workspace: Path) -> tuple[BotBinding, ...]:
    roots = (
        (ensure_user_layout() / "bots", "user"),
        (ensure_project_layout(workspace) / "bots", "project"),
    )
    selected: dict[str, BotBinding] = {}
    for root, source in roots:
        by_name: dict[str, BotBinding] = {}
        for path in sorted(root.glob("*.bot.yaml")):
            binding = _load_bot(path, source)
            if binding.name in by_name:
                raise BotError(f"duplicate bot {binding.name!r} in {root}")
            by_name[binding.name] = binding
        selected.update(by_name)
    return tuple(sorted(selected.values(), key=lambda item: item.name))


def resolve_bot(name: str, workspace: Path) -> BotBinding:
    for bot in discover_bots(workspace):
        if bot.name == name:
            return bot
    raise BotError(f"bot {name!r} was not found")


def _load_bot(path: Path, source: str) -> BotBinding:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (
            document.get("apiVersion") != "merced.ai/v1alpha1"
            or document.get("kind") != "BotBinding"
        ):
            raise BotError("unsupported bot document")
        metadata = document["metadata"]
        spec = document["spec"]
        harness = spec["harness"]
        return BotBinding(
            name=metadata["name"],
            profile=spec["profile"],
            harness=BotHarnessPreference(
                preferred=harness["preferred"], fallbacks=tuple(harness.get("fallbacks", ()))
            ),
            workspace=spec.get("workspace", "."),
            session=spec.get("session", {}),
            source=source,  # type: ignore[arg-type]
            path=path.resolve(),
        )
    except (KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        raise BotError(f"invalid bot file {path}: {exc}") from exc


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
