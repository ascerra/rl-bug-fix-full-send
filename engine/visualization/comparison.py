"""Comparison report builder — generates side-by-side diff view of agent vs human fix.

Transforms execution records containing comparison data into a structured format
suitable for rendering in the HTML report. When running against a known-solved bug,
the execution record includes both the agent's diff and the human's diff. This module
parses those diffs, computes similarity metrics, and structures everything for the
Jinja2 template.

Per SPEC §6.3, the comparison report includes:
- Side-by-side diff: agent fix vs human fix
- Structural comparison: same files changed? same approach?
- Test comparison: did both fixes make the same tests pass?
- Annotation: AI-generated analysis of similarities and differences
- Metrics: lines changed, files touched, complexity delta
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FileDiff:
    """Parsed diff for a single file."""

    path: str = ""
    lines_added: int = 0
    lines_removed: int = 0
    hunks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "lines_added": self.lines_added,
            "lines_removed": self.lines_removed,
            "hunks": self.hunks,
        }


@dataclass
class DiffSummary:
    """Aggregate summary of a complete diff (all files)."""

    files: list[FileDiff] = field(default_factory=list)
    total_files: int = 0
    total_lines_added: int = 0
    total_lines_removed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [f.to_dict() for f in self.files],
            "total_files": self.total_files,
            "total_lines_added": self.total_lines_added,
            "total_lines_removed": self.total_lines_removed,
        }

    @property
    def file_paths(self) -> list[str]:
        return [f.path for f in self.files]


@dataclass
class ComparisonMetrics:
    """Quantitative comparison between agent and human diffs."""

    file_overlap: float = 0.0
    files_only_agent: list[str] = field(default_factory=list)
    files_only_human: list[str] = field(default_factory=list)
    files_both: list[str] = field(default_factory=list)
    agent_lines_added: int = 0
    agent_lines_removed: int = 0
    human_lines_added: int = 0
    human_lines_removed: int = 0
    complexity_delta: int = 0
    similarity_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_overlap": round(self.file_overlap, 3),
            "files_only_agent": self.files_only_agent,
            "files_only_human": self.files_only_human,
            "files_both": self.files_both,
            "agent_lines_added": self.agent_lines_added,
            "agent_lines_removed": self.agent_lines_removed,
            "human_lines_added": self.human_lines_added,
            "human_lines_removed": self.human_lines_removed,
            "complexity_delta": self.complexity_delta,
            "similarity_score": round(self.similarity_score, 3),
        }


@dataclass
class ComparisonData:
    """Complete comparison report data for template rendering."""

    enabled: bool = False
    comparison_ref: str = ""
    agent_diff: str = ""
    human_diff: str = ""
    agent_summary: DiffSummary = field(default_factory=DiffSummary)
    human_summary: DiffSummary = field(default_factory=DiffSummary)
    metrics: ComparisonMetrics = field(default_factory=ComparisonMetrics)
    analysis: str = ""
    test_comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "comparison_ref": self.comparison_ref,
            "agent_diff": self.agent_diff,
            "human_diff": self.human_diff,
            "agent_summary": self.agent_summary.to_dict(),
            "human_summary": self.human_summary.to_dict(),
            "metrics": self.metrics.to_dict(),
            "analysis": self.analysis,
            "test_comparison": self.test_comparison,
        }


def build_comparison(execution: dict[str, Any]) -> ComparisonData:
    """Build comparison data from a raw execution record.

    Accepts the full execution.json structure (with top-level ``"execution"`` key)
    or a flat execution dict. Returns a ``ComparisonData`` with ``enabled=False``
    if no comparison data is present.
    """
    exec_data = execution.get("execution", execution)

    target = exec_data.get("target", {})
    comparison_ref = target.get("comparison_ref", "")
    result = exec_data.get("result", {})
    comparison_raw = result.get("comparison", {})

    if not comparison_ref and not comparison_raw:
        return ComparisonData(enabled=False)

    agent_diff = comparison_raw.get("agent_diff", "")
    human_diff = comparison_raw.get("human_diff", "")

    agent_summary = parse_unified_diff(agent_diff)
    human_summary = parse_unified_diff(human_diff)

    metrics = compute_metrics(agent_summary, human_summary)
    metrics.similarity_score = comparison_raw.get("similarity_score", metrics.similarity_score)

    analysis = comparison_raw.get("analysis", "")

    test_comparison = comparison_raw.get("test_comparison", {})

    return ComparisonData(
        enabled=True,
        comparison_ref=comparison_ref,
        agent_diff=agent_diff,
        human_diff=human_diff,
        agent_summary=agent_summary,
        human_summary=human_summary,
        metrics=metrics,
        analysis=analysis,
        test_comparison=test_comparison,
    )


_DIFF_FILE_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_DIFF_HUNK_HEADER = re.compile(r"^@@\s")


def parse_unified_diff(diff_text: str) -> DiffSummary:
    """Parse a unified diff string into a structured ``DiffSummary``.

    Handles standard ``git diff`` output. Each file section starts with
    ``diff --git a/path b/path`` and contains one or more hunks starting with ``@@``.
    """
    if not diff_text or not diff_text.strip():
        return DiffSummary()

    files: list[FileDiff] = []
    current_file: FileDiff | None = None
    current_hunk_lines: list[str] = []

    for line in diff_text.splitlines():
        header_match = _DIFF_FILE_HEADER.match(line)
        if header_match:
            if current_file is not None:
                if current_hunk_lines:
                    current_file.hunks.append("\n".join(current_hunk_lines))
                files.append(current_file)
            current_file = FileDiff(path=header_match.group(2))
            current_hunk_lines = []
            continue

        if _DIFF_HUNK_HEADER.match(line):
            if current_file is not None and current_hunk_lines:
                current_file.hunks.append("\n".join(current_hunk_lines))
            current_hunk_lines = [line]
            continue

        if current_file is not None:
            if line.startswith("+") and not line.startswith("+++"):
                current_file.lines_added += 1
            elif line.startswith("-") and not line.startswith("---"):
                current_file.lines_removed += 1
            if current_hunk_lines:
                current_hunk_lines.append(line)

    if current_file is not None:
        if current_hunk_lines:
            current_file.hunks.append("\n".join(current_hunk_lines))
        files.append(current_file)

    total_added = sum(f.lines_added for f in files)
    total_removed = sum(f.lines_removed for f in files)

    return DiffSummary(
        files=files,
        total_files=len(files),
        total_lines_added=total_added,
        total_lines_removed=total_removed,
    )


def compute_file_overlap(agent_files: list[str], human_files: list[str]) -> float:
    """Compute Jaccard similarity coefficient between two file path sets.

    Returns 0.0 if both sets are empty, 1.0 if identical, and a value in [0, 1]
    representing the proportion of files in common.
    """
    if not agent_files and not human_files:
        return 0.0
    agent_set = set(agent_files)
    human_set = set(human_files)
    intersection = agent_set & human_set
    union = agent_set | human_set
    return len(intersection) / len(union) if union else 0.0


def compute_metrics(agent: DiffSummary, human: DiffSummary) -> ComparisonMetrics:
    """Compute comparison metrics between agent and human diff summaries."""
    agent_set = set(agent.file_paths)
    human_set = set(human.file_paths)

    files_both = sorted(agent_set & human_set)
    files_only_agent = sorted(agent_set - human_set)
    files_only_human = sorted(human_set - agent_set)

    file_overlap = compute_file_overlap(agent.file_paths, human.file_paths)

    agent_total = agent.total_lines_added + agent.total_lines_removed
    human_total = human.total_lines_added + human.total_lines_removed
    complexity_delta = agent_total - human_total

    similarity = _compute_similarity(agent, human, file_overlap)

    return ComparisonMetrics(
        file_overlap=file_overlap,
        files_only_agent=files_only_agent,
        files_only_human=files_only_human,
        files_both=files_both,
        agent_lines_added=agent.total_lines_added,
        agent_lines_removed=agent.total_lines_removed,
        human_lines_added=human.total_lines_added,
        human_lines_removed=human.total_lines_removed,
        complexity_delta=complexity_delta,
        similarity_score=similarity,
    )


def _compute_similarity(
    agent: DiffSummary,
    human: DiffSummary,
    file_overlap: float,
) -> float:
    """Compute a heuristic similarity score in [0, 1].

    Weighs file overlap (40%), size similarity (30%), and per-file line similarity (30%).
    """
    if not agent.files and not human.files:
        return 0.0

    size_sim = _size_similarity(
        agent.total_lines_added + agent.total_lines_removed,
        human.total_lines_added + human.total_lines_removed,
    )

    line_sim = _per_file_line_similarity(agent, human)

    return round(0.4 * file_overlap + 0.3 * size_sim + 0.3 * line_sim, 3)


def _size_similarity(a: int, b: int) -> float:
    """Similarity based on total change size.

    Returns 1.0 for identical sizes, 0.0 for extreme divergence.
    """
    if a == 0 and b == 0:
        return 1.0
    max_val = max(a, b)
    if max_val == 0:
        return 1.0
    return 1.0 - abs(a - b) / max_val


def _per_file_line_similarity(agent: DiffSummary, human: DiffSummary) -> float:
    """Average per-file line similarity for files touched by both diffs."""
    agent_by_path = {f.path: f for f in agent.files}
    human_by_path = {f.path: f for f in human.files}

    common_paths = set(agent_by_path.keys()) & set(human_by_path.keys())
    if not common_paths:
        return 0.0

    total = 0.0
    for path in common_paths:
        af = agent_by_path[path]
        hf = human_by_path[path]
        added_sim = _size_similarity(af.lines_added, hf.lines_added)
        removed_sim = _size_similarity(af.lines_removed, hf.lines_removed)
        total += (added_sim + removed_sim) / 2

    return total / len(common_paths)
