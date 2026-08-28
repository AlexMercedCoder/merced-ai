"""Read-only Agentic Graph Specification (AGS) validation and planning.

Merced AI deliberately stops at conformance level 0: it can ingest, validate, identify,
and deterministically plan a graph, but it does not claim execution semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ags import graph_digest, reference_validator, validate_path


class GraphError(ValueError):
    """Raised when an AGS document cannot be safely consumed."""


@dataclass(frozen=True)
class GraphPlan:
    path: Path
    graph_id: str
    digest: str
    entrypoints: tuple[str, ...]
    node_order: tuple[str, ...]
    reachable_nodes: tuple[str, ...]
    worst_case_executions: int
    estimated_cost_usd: float | None
    tier_histogram: dict[str, int]
    unsupported_features: tuple[str, ...]
    warnings: tuple[str, ...]
    document: dict[str, Any]


def plan_graph(path: Path) -> GraphPlan:
    """Validate an AGS graph and return a stable, non-executing dependency plan."""
    target = path.expanduser().resolve()
    report = validate_path(target)
    if report.errors:
        raise GraphError("; ".join(str(finding) for finding in report.errors))

    validator = reference_validator()
    load_report = validator.Report(target)
    document = validator.load_document(target, load_report)
    if document is None or load_report.errors:
        findings = load_report.errors or load_report.findings
        raise GraphError("; ".join(str(finding) for finding in findings))

    nodes = document["nodes"]
    declared = tuple(nodes)
    dependencies: dict[str, set[str]] = {node_id: set() for node_id in declared}
    for node_id, node in nodes.items():
        dependencies[node_id].update(node.get("depends_on", []))
    for edge in document.get("edges", []):
        dependencies[edge["to"]].add(edge["from"])

    order: list[str] = []
    remaining = {node_id: set(values) for node_id, values in dependencies.items()}
    while remaining:
        ready = [node_id for node_id in declared if node_id in remaining and not remaining[node_id]]
        if not ready:  # Defensive: the reference validator should already report AG111.
            raise GraphError("graph dependency cycle prevents deterministic planning")
        order.extend(ready)
        for node_id in ready:
            remaining.pop(node_id)
        for values in remaining.values():
            values.difference_update(ready)

    forward: dict[str, set[str]] = {node_id: set() for node_id in declared}
    for node_id, values in dependencies.items():
        for dependency in values:
            forward[dependency].add(node_id)
    reachable = set(document["entrypoints"])
    frontier = list(document["entrypoints"])
    while frontier:
        for child in forward[frontier.pop(0)]:
            if child not in reachable:
                reachable.add(child)
                frontier.append(child)

    tier_histogram = {tier: 0 for tier in ("minimal", "standard", "advanced", "frontier")}
    estimated_cost = 0.0
    has_cost_estimate = False
    worst_case = 0
    unsupported = {"execution"}
    for node in nodes.values():
        tier = (node.get("intelligence") or {}).get("tier")
        if tier in tier_histogram:
            tier_histogram[tier] += 1
        cost = (node.get("estimate") or {}).get("cost_usd")
        if isinstance(cost, (int, float)):
            estimated_cost += float(cost)
            has_cost_estimate = True
        attempts = ((node.get("failure") or {}).get("retry") or {}).get("max_attempts", 1)
        multiplier = 1
        node_type = node.get("type")
        if node_type == "loop":
            multiplier = (node.get("loop") or {}).get("max_iterations", 1)
            unsupported.add("loop")
        elif node_type == "map":
            multiplier = (node.get("map") or {}).get("max_items", 1)
            unsupported.add("map")
        elif node_type == "subgraph":
            unsupported.add("subgraph")
        elif node_type in {"decision", "gate"}:
            unsupported.add(str(node_type))
        worst_case += int(attempts) * int(multiplier)

    return GraphPlan(
        path=target,
        graph_id=document["id"],
        digest=graph_digest(document),
        entrypoints=tuple(document["entrypoints"]),
        node_order=tuple(order),
        reachable_nodes=tuple(node_id for node_id in order if node_id in reachable),
        worst_case_executions=worst_case,
        estimated_cost_usd=estimated_cost if has_cost_estimate else None,
        tier_histogram=tier_histogram,
        unsupported_features=tuple(sorted(unsupported)),
        warnings=tuple(str(finding) for finding in report.warnings),
        document=document,
    )
