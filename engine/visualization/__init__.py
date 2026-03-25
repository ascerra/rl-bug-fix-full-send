"""Visualization and report generation."""

from engine.visualization.action_map import (
    ActionMapData,
    ActionMapEdge,
    ActionMapLayer,
    ActionMapNode,
    build_action_map,
    total_nodes,
)
from engine.visualization.comparison import (
    ComparisonData,
    ComparisonMetrics,
    DiffSummary,
    FileDiff,
    build_comparison,
    compute_file_overlap,
    compute_metrics,
    parse_unified_diff,
)
from engine.visualization.decision_tree import TreeNode, build_decision_tree, node_count
from engine.visualization.publisher import (
    PublishResult,
    ReportPublisher,
    build_artifact_manifest,
    build_summary_markdown,
)
from engine.visualization.report_generator import ReportData, ReportGenerator, extract_report_data

__all__ = [
    "ActionMapData",
    "ActionMapEdge",
    "ActionMapLayer",
    "ActionMapNode",
    "ComparisonData",
    "ComparisonMetrics",
    "DiffSummary",
    "FileDiff",
    "PublishResult",
    "ReportData",
    "ReportGenerator",
    "ReportPublisher",
    "TreeNode",
    "build_action_map",
    "build_artifact_manifest",
    "build_comparison",
    "build_decision_tree",
    "build_summary_markdown",
    "compute_file_overlap",
    "compute_metrics",
    "extract_report_data",
    "node_count",
    "parse_unified_diff",
    "total_nodes",
]
