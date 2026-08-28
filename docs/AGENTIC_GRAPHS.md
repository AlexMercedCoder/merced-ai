# Agentic Graphs

Merced AI 0.3.0 provides read-only Agentic Graph Specification (AGS) 1.0 validation and planning at
conformance Level 0. It uses `agentic-graph-spec>=1.0.1,<2` and tests against upstream commit
`f180a4dbd07911f90dd0821f531d7ccd51bb0764`.

## Scope

`merced_ai.graphs.plan_graph()` validates a YAML or JSON graph and returns:

- the canonical RFC 8785 graph digest;
- entrypoints and deterministic dependency order;
- nodes reachable from the declared entrypoints;
- a conservative worst-case execution count from retries, loops, and maps;
- declared cost totals and an intelligence-tier histogram; and
- an explicit list of features that require an executing harness.

```python
from pathlib import Path

from merced_ai.graphs import plan_graph

plan = plan_graph(Path("release.agraph.yaml"))
print(plan.digest)
print(plan.node_order)
print(plan.unsupported_features)
```

Invalid graphs raise `GraphError` with the reference validator's diagnostic code. CI exercises all
valid upstream examples and all upstream invalid fixtures, including recursive-subgraph diagnostic
`AG131`.

## Deliberate boundary

Merced AI does not execute nodes, evaluate criteria, activate conditional edges, request human
decisions, write portable run records, or resume a graph. Features such as gates, decisions,
loops, maps, subgraphs, and execution itself are reported as unsupported rather than silently
downgraded. Use a Level 3 implementation such as Loro 0.17.0 or MagAgent 0.99.0 when execution is
required.

The exact Level 0 result and pinned revision are published in
[ags-conformance.json](ags-conformance.json). This is implementation evidence, not third-party
certification.
