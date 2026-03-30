"""Decision tree builder — transforms execution records into hierarchical tree structures.

Produces a tree data structure suitable for D3.js visualization, where:
- Root node represents the loop execution
- Phase execution nodes represent each iteration
- Action nodes are children of their phase (collapsed by default)
- An outcome node shows the final result

Every node carries metadata (actions, LLM transcripts, timings) accessible
via click-to-expand in the rendered visualization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TreeNode:
    """A node in the decision tree."""

    id: str = ""
    label: str = ""
    node_type: str = "default"
    status: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list[TreeNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON / D3.js consumption."""
        return {
            "id": self.id,
            "label": self.label,
            "type": self.node_type,
            "status": self.status,
            "meta": self.metadata,
            "children": [c.to_dict() for c in self.children],
        }


def build_decision_tree(execution: dict[str, Any]) -> TreeNode:
    """Build a decision tree from a raw execution record.

    Accepts the full execution.json structure (with top-level ``"execution"`` key)
    or a flat execution dict.
    """
    exec_data = execution.get("execution", execution)

    exec_id = exec_data.get("id", "unknown")
    result = exec_data.get("result", {})
    overall_status = result.get("status", "unknown")
    iterations = exec_data.get("iterations", [])
    actions = exec_data.get("actions", [])
    metrics = exec_data.get("metrics", {})

    actions_by_iter = _group_actions_by_iteration(actions)

    root = TreeNode(
        id="root",
        label="RL Engine",
        node_type="root",
        status=overall_status,
        metadata={
            "execution_id": exec_id,
            "started_at": exec_data.get("started_at", ""),
            "trigger": exec_data.get("trigger", {}),
            "target": _safe_target(exec_data.get("target", {})),
            "total_iterations": result.get("total_iterations", len(iterations)),
            "total_tokens": metrics.get("total_tokens_in", 0) + metrics.get("total_tokens_out", 0),
        },
    )

    if not iterations:
        root.children.append(
            TreeNode(
                id="empty",
                label="No iterations executed",
                node_type="outcome",
                status="unknown",
            )
        )
        return root

    for iteration in iterations:
        phase_node = _build_phase_node(iteration, actions_by_iter)
        root.children.append(phase_node)

    outcome_node = _build_outcome_node(overall_status, exec_data, result, iterations)
    root.children.append(outcome_node)

    return root


def node_count(node: TreeNode) -> int:
    """Count total nodes in a tree (inclusive of root)."""
    return 1 + sum(node_count(c) for c in node.children)


def _group_actions_by_iteration(
    actions: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for action in actions:
        iter_num = action.get("iteration", 0)
        grouped.setdefault(iter_num, []).append(action)
    return grouped


def _build_phase_node(
    iteration: dict[str, Any],
    actions_by_iter: dict[int, list[dict[str, Any]]],
) -> TreeNode:
    iter_num = iteration.get("number", 0)
    phase = iteration.get("phase", "unknown")
    iter_result = iteration.get("result", {})
    success = iter_result.get("success", False)
    escalate = iter_result.get("escalate", False)
    should_continue = iter_result.get("should_continue", False)
    next_phase = iter_result.get("next_phase", "")

    node_status = _resolve_status(success, escalate, should_continue)

    iter_actions = actions_by_iter.get(iter_num, [])
    llm_calls = [a for a in iter_actions if a.get("action_type") == "llm_query"]
    tool_calls = [
        a for a in iter_actions if a.get("action_type") not in ("llm_query", "escalation")
    ]

    phase_node = TreeNode(
        id=f"iter-{iter_num}",
        label=_phase_label(phase, iter_num, node_status, next_phase),
        node_type="phase",
        status=node_status,
        metadata={
            "phase": phase,
            "iteration": iter_num,
            "started_at": iteration.get("started_at", ""),
            "completed_at": iteration.get("completed_at", ""),
            "duration_ms": iteration.get("duration_ms", 0),
            "success": success,
            "escalate": escalate,
            "next_phase": next_phase,
            "action_count": len(iter_actions),
            "llm_call_count": len(llm_calls),
            "tool_call_count": len(tool_calls),
        },
    )

    for action in iter_actions:
        phase_node.children.append(_build_action_node(action))

    return phase_node


def _build_action_node(action: dict[str, Any]) -> TreeNode:
    action_type = action.get("action_type", "unknown")
    description = action.get("input", {}).get("description", "No description")
    output_success = action.get("output", {}).get("success", False)

    return TreeNode(
        id=f"action-{action.get('id', 'unknown')}",
        label=_action_label(action_type, description),
        node_type="action",
        status="success" if output_success else "failure",
        metadata={
            "action_type": action_type,
            "description": description,
            "duration_ms": action.get("duration_ms", 0),
            "input": action.get("input", {}),
            "output": action.get("output", {}),
            "llm_context": action.get("llm_context", {}),
            "provenance": action.get("provenance", {}),
        },
    )


def _build_outcome_node(
    overall_status: str,
    exec_data: dict[str, Any],
    result: dict[str, Any],
    iterations: list[dict[str, Any]],
) -> TreeNode:
    label_map = {
        "success": "COMPLETE",
        "failure": "FAILED",
        "escalated": "ESCALATED",
        "timeout": "TIMEOUT",
    }
    return TreeNode(
        id="outcome",
        label=label_map.get(overall_status, overall_status.upper() or "UNKNOWN"),
        node_type="outcome",
        status=overall_status,
        metadata={
            "status": overall_status,
            "total_iterations": result.get("total_iterations", len(iterations)),
            "completed_at": exec_data.get("completed_at", ""),
        },
    )


def _resolve_status(success: bool, escalate: bool, should_continue: bool) -> str:
    if escalate:
        return "escalated"
    if success:
        return "success"
    if should_continue:
        return "retry"
    return "failure"


def _phase_label(phase: str, iter_num: int, status: str, next_phase: str) -> str:
    suffix = ""
    if status == "escalated":
        suffix = " [ESCALATED]"
    elif status == "retry":
        suffix = " [retry]"
    elif status == "failure":
        suffix = " [FAILED]"
    elif next_phase and next_phase != _default_next(phase):
        suffix = f" -> {next_phase}"
    return f"{phase} #{iter_num}{suffix}"


def _default_next(phase: str) -> str:
    order = ["triage", "implement", "review", "validate", "report"]
    try:
        idx = order.index(phase)
        return order[idx + 1] if idx + 1 < len(order) else ""
    except ValueError:
        return ""


def _action_label(action_type: str, description: str) -> str:
    prefix = {"llm_query": "LLM", "tool_execution": "Tool", "escalation": "ESC"}.get(
        action_type, action_type
    )
    truncated = description[:80] + ("..." if len(description) > 80 else "")
    return f"[{prefix}] {truncated}"


def _safe_target(target: dict[str, Any]) -> dict[str, Any]:
    """Return target metadata without overly large fields."""
    safe = dict(target)
    safe.pop("workflow", None)
    return safe
