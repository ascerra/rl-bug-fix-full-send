"""Action map builder — transforms execution records into layered action map data.

Produces a layered data structure suitable for D3.js visualization, where:
- Each layer represents a phase (triage, implement, review, validate, report)
- Objects on each layer represent actions taken (file reads, edits, test runs, API calls)
- Connections between objects show data flow (sequential within phase, cross-phase dependencies)
- Node size encodes token usage; color encodes action type and status

The output is JSON-serializable for embedding in HTML reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ActionMapNode:
    """A single action in the action map."""

    id: str = ""
    action_type: str = "unknown"
    description: str = ""
    phase: str = ""
    iteration: int = 0
    status: str = "unknown"
    duration_ms: float = 0.0
    tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON / D3.js consumption."""
        return {
            "id": self.id,
            "action_type": self.action_type,
            "description": self.description,
            "phase": self.phase,
            "iteration": self.iteration,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "meta": self.metadata,
        }


@dataclass
class ActionMapEdge:
    """A connection between two actions showing data flow."""

    source: str = ""
    target: str = ""
    edge_type: str = "sequential"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON / D3.js consumption."""
        return {
            "source": self.source,
            "target": self.target,
            "type": self.edge_type,
        }


@dataclass
class ActionMapLayer:
    """A phase layer containing action nodes."""

    phase: str = ""
    iteration: int = 0
    successful: bool = False
    nodes: list[ActionMapNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON / D3.js consumption."""
        return {
            "phase": self.phase,
            "iteration": self.iteration,
            "successful": self.successful,
            "nodes": [n.to_dict() for n in self.nodes],
        }


@dataclass
class ActionMapData:
    """Complete action map with layers and edges."""

    layers: list[ActionMapLayer] = field(default_factory=list)
    edges: list[ActionMapEdge] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON / D3.js consumption."""
        return {
            "layers": [layer.to_dict() for layer in self.layers],
            "edges": [edge.to_dict() for edge in self.edges],
            "summary": self.summary,
        }


def build_action_map(execution: dict[str, Any]) -> ActionMapData:
    """Build an action map from a raw execution record.

    Accepts the full execution.json structure (with top-level ``"execution"`` key)
    or a flat execution dict.
    """
    exec_data = execution.get("execution", execution)

    iterations = exec_data.get("iterations", [])
    actions = exec_data.get("actions", [])

    if not iterations and not actions:
        return ActionMapData(summary=_build_summary([], []))

    actions_by_iter = _group_actions_by_iteration(actions)
    layers = _build_layers(iterations, actions_by_iter)
    edges = _build_edges(layers)
    summary = _build_summary(layers, actions)

    return ActionMapData(layers=layers, edges=edges, summary=summary)


def total_nodes(data: ActionMapData) -> int:
    """Count total action nodes across all layers."""
    return sum(len(layer.nodes) for layer in data.layers)


def _group_actions_by_iteration(
    actions: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for action in actions:
        iter_num = action.get("iteration", 0)
        grouped.setdefault(iter_num, []).append(action)
    return grouped


def _build_layers(
    iterations: list[dict[str, Any]],
    actions_by_iter: dict[int, list[dict[str, Any]]],
) -> list[ActionMapLayer]:
    layers: list[ActionMapLayer] = []
    for iteration in iterations:
        iter_num = iteration.get("number", 0)
        phase = iteration.get("phase", "unknown")
        result = iteration.get("result", {})
        successful = result.get("success", False)

        iter_actions = actions_by_iter.get(iter_num, [])
        nodes = [_build_node(action) for action in iter_actions]

        layers.append(
            ActionMapLayer(
                phase=phase,
                iteration=iter_num,
                successful=successful,
                nodes=nodes,
            )
        )

    return layers


def _build_node(action: dict[str, Any]) -> ActionMapNode:
    action_type = action.get("action_type", "unknown")
    description = action.get("input", {}).get("description", "No description")
    output = action.get("output", {})
    llm_ctx = action.get("llm_context", {})

    tokens_in = llm_ctx.get("tokens_in", 0) or 0
    tokens_out = llm_ctx.get("tokens_out", 0) or 0

    return ActionMapNode(
        id=action.get("id", "unknown"),
        action_type=action_type,
        description=_truncate(description, 120),
        phase=action.get("phase", "unknown"),
        iteration=action.get("iteration", 0),
        status="success" if output.get("success") else "failure",
        duration_ms=action.get("duration_ms", 0.0),
        tokens=tokens_in + tokens_out,
        metadata={
            "full_description": description,
            "input": action.get("input", {}),
            "output": output,
            "llm_context": llm_ctx,
            "provenance": action.get("provenance", {}),
            "timestamp": action.get("timestamp", ""),
        },
    )


def _build_edges(layers: list[ActionMapLayer]) -> list[ActionMapEdge]:
    """Build edges representing data flow between actions.

    Within a layer: sequential edges connect consecutive actions.
    Across layers: the last action of one layer connects to the first of the next,
    representing phase-to-phase data flow.
    """
    edges: list[ActionMapEdge] = []

    for layer in layers:
        nodes = layer.nodes
        for i in range(len(nodes) - 1):
            edges.append(
                ActionMapEdge(
                    source=nodes[i].id,
                    target=nodes[i + 1].id,
                    edge_type="sequential",
                )
            )

    for i in range(len(layers) - 1):
        prev_nodes = layers[i].nodes
        next_nodes = layers[i + 1].nodes
        if prev_nodes and next_nodes:
            edges.append(
                ActionMapEdge(
                    source=prev_nodes[-1].id,
                    target=next_nodes[0].id,
                    edge_type="phase_transition",
                )
            )

    file_edges = _infer_file_flow_edges(layers)
    edges.extend(file_edges)

    return edges


def _infer_file_flow_edges(layers: list[ActionMapLayer]) -> list[ActionMapEdge]:
    """Infer data flow edges based on shared file paths between actions.

    When a file_read action in one phase touches the same path as a file_write
    action in a later phase, a data_flow edge connects them.
    """
    read_nodes: list[tuple[str, str]] = []
    write_nodes: list[tuple[str, str]] = []

    for layer in layers:
        for node in layer.nodes:
            path = node.metadata.get("input", {}).get("path", "")
            if not path:
                continue
            if node.action_type == "tool_execution" and "read" in node.description.lower():
                read_nodes.append((node.id, path))
            elif node.action_type == "tool_execution" and "write" in node.description.lower():
                write_nodes.append((node.id, path))

    edges: list[ActionMapEdge] = []
    seen: set[tuple[str, str]] = set()
    for read_id, read_path in read_nodes:
        for write_id, write_path in write_nodes:
            if read_id == write_id:
                continue
            if read_path == write_path and (read_id, write_id) not in seen:
                seen.add((read_id, write_id))
                edges.append(
                    ActionMapEdge(
                        source=read_id,
                        target=write_id,
                        edge_type="data_flow",
                    )
                )

    return edges


def _build_summary(
    layers: list[ActionMapLayer],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build summary statistics for the action map."""
    total_actions = sum(len(layer.nodes) for layer in layers)
    phases = list(dict.fromkeys(layer.phase for layer in layers))
    total_tokens = 0
    total_duration = 0.0
    type_counts: dict[str, int] = {}

    for action in actions:
        llm_ctx = action.get("llm_context", {})
        tokens_in = llm_ctx.get("tokens_in", 0) or 0
        tokens_out = llm_ctx.get("tokens_out", 0) or 0
        total_tokens += tokens_in + tokens_out
        total_duration += action.get("duration_ms", 0.0)
        a_type = action.get("action_type", "unknown")
        type_counts[a_type] = type_counts.get(a_type, 0) + 1

    return {
        "total_actions": total_actions,
        "total_layers": len(layers),
        "phases": phases,
        "total_tokens": total_tokens,
        "total_duration_ms": round(total_duration, 2),
        "action_type_counts": type_counts,
    }


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
