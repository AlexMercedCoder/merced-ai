from __future__ import annotations

import os
from pathlib import Path

import pytest

from merced_ai.graphs import GraphError, plan_graph

AGS_REPO = Path(
    os.environ.get(
        "AGS_FIXTURE_REPO",
        str(Path(__file__).resolve().parents[2] / "agentic-graph-spec"),
    )
)


def test_plan_graph_validates_and_orders_current_ags_example() -> None:
    plan = plan_graph(AGS_REPO / "examples" / "minimal.agraph.yaml")

    assert plan.graph_id == "examples/minimal"
    assert plan.digest.startswith("sha256-")
    assert plan.node_order == ("draft_contributing", "maintainer_approval")
    assert plan.reachable_nodes == plan.node_order
    assert plan.worst_case_executions == 3
    assert plan.estimated_cost_usd == pytest.approx(0.3)
    assert plan.tier_histogram == {
        "minimal": 0,
        "standard": 1,
        "advanced": 0,
        "frontier": 0,
    }
    assert plan.unsupported_features == ("execution", "gate")


def test_plan_graph_rejects_recursive_subgraph_fixture() -> None:
    with pytest.raises(GraphError, match="AG131"):
        plan_graph(AGS_REPO / "conformance" / "invalid" / "ag131-recursive-subgraph.agraph.yaml")


@pytest.mark.parametrize(
    "path",
    sorted((AGS_REPO / "examples").glob("*.agraph.*")),
    ids=lambda path: path.name,
)
def test_all_immutable_upstream_examples_plan(path: Path) -> None:
    assert plan_graph(path).digest.startswith("sha256-")


@pytest.mark.parametrize(
    "path",
    sorted((AGS_REPO / "conformance" / "invalid").glob("*.agraph.*")),
    ids=lambda path: path.name,
)
def test_all_immutable_upstream_invalid_fixtures_are_rejected(path: Path) -> None:
    expected = path.name.split("-", 1)[0].upper()
    with pytest.raises(GraphError, match=expected):
        plan_graph(path)
