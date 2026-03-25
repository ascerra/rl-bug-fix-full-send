"""Tests for engine.visualization.comparison."""

from __future__ import annotations

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
from engine.visualization.report_generator import ReportGenerator, extract_report_data

# ---------------------------------------------------------------------------
# Sample diff data
# ---------------------------------------------------------------------------

SINGLE_FILE_DIFF = """\
diff --git a/pkg/controller/reconciler.go b/pkg/controller/reconciler.go
index abc1234..def5678 100644
--- a/pkg/controller/reconciler.go
+++ b/pkg/controller/reconciler.go
@@ -42,6 +42,9 @@ func (r *Reconciler) Reconcile(ctx context.Context) error {
     obj := r.getObject()
-    result := obj.Process()
+    if obj == nil {
+        return fmt.Errorf("object is nil")
+    }
+    result := obj.Process()
     return result
"""

TWO_FILE_DIFF = """\
diff --git a/pkg/controller/reconciler.go b/pkg/controller/reconciler.go
index abc1234..def5678 100644
--- a/pkg/controller/reconciler.go
+++ b/pkg/controller/reconciler.go
@@ -42,6 +42,9 @@ func (r *Reconciler) Reconcile(ctx context.Context) error {
     obj := r.getObject()
-    result := obj.Process()
+    if obj == nil {
+        return fmt.Errorf("object is nil")
+    }
+    result := obj.Process()
     return result
diff --git a/pkg/controller/reconciler_test.go b/pkg/controller/reconciler_test.go
index 111aaaa..222bbbb 100644
--- a/pkg/controller/reconciler_test.go
+++ b/pkg/controller/reconciler_test.go
@@ -10,0 +10,8 @@ func TestReconcile(t *testing.T) {
+func TestReconcileNilObject(t *testing.T) {
+    r := &Reconciler{}
+    err := r.Reconcile(context.Background())
+    if err == nil {
+        t.Fatal("expected error for nil object")
+    }
+}
"""

HUMAN_DIFF = """\
diff --git a/pkg/controller/reconciler.go b/pkg/controller/reconciler.go
index abc1234..def5678 100644
--- a/pkg/controller/reconciler.go
+++ b/pkg/controller/reconciler.go
@@ -42,6 +42,8 @@ func (r *Reconciler) Reconcile(ctx context.Context) error {
     obj := r.getObject()
+    if obj == nil {
+        return nil
+    }
     result := obj.Process()
     return result
"""


def _make_execution(
    *,
    comparison_ref: str = "",
    comparison: dict | None = None,
    status: str = "success",
) -> dict:
    """Build a minimal execution record for comparison testing."""
    result: dict = {"status": status, "total_iterations": 2, "phase_results": []}
    if comparison is not None:
        result["comparison"] = comparison
    return {
        "execution": {
            "id": "cmp-test-id",
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:05:00+00:00",
            "trigger": {
                "type": "github_issue",
                "source_url": "https://github.com/o/r/issues/1",
            },
            "target": {"repo_path": "/tmp/repo", "comparison_ref": comparison_ref},
            "config": {"llm": {"provider": "mock"}},
            "iterations": [
                {
                    "number": 1,
                    "phase": "triage",
                    "started_at": "2026-03-25T10:00:00+00:00",
                    "completed_at": "2026-03-25T10:01:00+00:00",
                    "duration_ms": 1500.0,
                    "result": {
                        "success": True,
                        "should_continue": True,
                        "next_phase": "implement",
                        "escalate": False,
                    },
                }
            ],
            "result": result,
            "metrics": {
                "total_iterations": 2,
                "total_llm_calls": 1,
                "total_tokens_in": 500,
                "total_tokens_out": 100,
                "total_tool_executions": 2,
                "time_per_phase_ms": {},
                "phase_iteration_counts": {},
                "errors": [],
            },
            "actions": [],
        }
    }


# ---------------------------------------------------------------------------
# FileDiff tests
# ---------------------------------------------------------------------------


class TestFileDiff:
    def test_to_dict(self):
        fd = FileDiff(path="a.go", lines_added=3, lines_removed=1, hunks=["@@ ..."])
        d = fd.to_dict()
        assert d["path"] == "a.go"
        assert d["lines_added"] == 3
        assert d["lines_removed"] == 1
        assert d["hunks"] == ["@@ ..."]

    def test_defaults(self):
        fd = FileDiff()
        assert fd.path == ""
        assert fd.lines_added == 0
        assert fd.hunks == []


# ---------------------------------------------------------------------------
# DiffSummary tests
# ---------------------------------------------------------------------------


class TestDiffSummary:
    def test_to_dict(self):
        ds = DiffSummary(
            files=[FileDiff(path="a.go", lines_added=2)],
            total_files=1,
            total_lines_added=2,
        )
        d = ds.to_dict()
        assert d["total_files"] == 1
        assert len(d["files"]) == 1
        assert d["files"][0]["path"] == "a.go"

    def test_file_paths_property(self):
        ds = DiffSummary(
            files=[FileDiff(path="a.go"), FileDiff(path="b.go")],
            total_files=2,
        )
        assert ds.file_paths == ["a.go", "b.go"]

    def test_empty_defaults(self):
        ds = DiffSummary()
        assert ds.total_files == 0
        assert ds.file_paths == []


# ---------------------------------------------------------------------------
# ComparisonMetrics tests
# ---------------------------------------------------------------------------


class TestComparisonMetrics:
    def test_to_dict_rounds_floats(self):
        cm = ComparisonMetrics(file_overlap=0.66666, similarity_score=0.12345)
        d = cm.to_dict()
        assert d["file_overlap"] == 0.667
        assert d["similarity_score"] == 0.123

    def test_defaults(self):
        cm = ComparisonMetrics()
        assert cm.file_overlap == 0.0
        assert cm.files_both == []
        assert cm.complexity_delta == 0


# ---------------------------------------------------------------------------
# ComparisonData tests
# ---------------------------------------------------------------------------


class TestComparisonData:
    def test_to_dict(self):
        cd = ComparisonData(enabled=True, comparison_ref="abc123", analysis="Good fix")
        d = cd.to_dict()
        assert d["enabled"] is True
        assert d["comparison_ref"] == "abc123"
        assert d["analysis"] == "Good fix"

    def test_disabled_by_default(self):
        cd = ComparisonData()
        assert cd.enabled is False

    def test_to_dict_nested(self):
        cd = ComparisonData(
            enabled=True,
            agent_summary=DiffSummary(total_files=1),
            metrics=ComparisonMetrics(similarity_score=0.5),
        )
        d = cd.to_dict()
        assert d["agent_summary"]["total_files"] == 1
        assert d["metrics"]["similarity_score"] == 0.5


# ---------------------------------------------------------------------------
# parse_unified_diff tests
# ---------------------------------------------------------------------------


class TestParseUnifiedDiff:
    def test_single_file(self):
        result = parse_unified_diff(SINGLE_FILE_DIFF)
        assert result.total_files == 1
        assert result.files[0].path == "pkg/controller/reconciler.go"
        assert result.files[0].lines_added == 4
        assert result.files[0].lines_removed == 1
        assert result.total_lines_added == 4
        assert result.total_lines_removed == 1

    def test_two_files(self):
        result = parse_unified_diff(TWO_FILE_DIFF)
        assert result.total_files == 2
        paths = [f.path for f in result.files]
        assert "pkg/controller/reconciler.go" in paths
        assert "pkg/controller/reconciler_test.go" in paths

    def test_two_files_line_counts(self):
        result = parse_unified_diff(TWO_FILE_DIFF)
        reconciler = next(f for f in result.files if f.path.split("/")[-1] == "reconciler.go")
        test_file = next(f for f in result.files if "test" in f.path)
        assert reconciler.lines_added == 4
        assert reconciler.lines_removed == 1
        assert test_file.lines_added == 7
        assert test_file.lines_removed == 0

    def test_empty_string(self):
        result = parse_unified_diff("")
        assert result.total_files == 0
        assert result.files == []

    def test_none_like_empty(self):
        result = parse_unified_diff("   \n  \n  ")
        assert result.total_files == 0

    def test_hunks_captured(self):
        result = parse_unified_diff(SINGLE_FILE_DIFF)
        assert len(result.files[0].hunks) == 1
        assert result.files[0].hunks[0].startswith("@@")

    def test_multiple_hunks_in_one_file(self):
        diff = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 line1
+added1
 line2
@@ -10,3 +11,4 @@
 line10
+added2
 line11
"""
        result = parse_unified_diff(diff)
        assert result.total_files == 1
        assert len(result.files[0].hunks) == 2
        assert result.files[0].lines_added == 2

    def test_no_diff_header(self):
        result = parse_unified_diff("just some random text\nno diff here")
        assert result.total_files == 0

    def test_human_diff(self):
        result = parse_unified_diff(HUMAN_DIFF)
        assert result.total_files == 1
        assert result.files[0].lines_added == 3
        assert result.files[0].lines_removed == 0


# ---------------------------------------------------------------------------
# compute_file_overlap tests
# ---------------------------------------------------------------------------


class TestComputeFileOverlap:
    def test_identical_sets(self):
        assert compute_file_overlap(["a.go", "b.go"], ["a.go", "b.go"]) == 1.0

    def test_disjoint_sets(self):
        assert compute_file_overlap(["a.go"], ["b.go"]) == 0.0

    def test_partial_overlap(self):
        result = compute_file_overlap(["a.go", "b.go"], ["b.go", "c.go"])
        assert abs(result - 1 / 3) < 0.01

    def test_both_empty(self):
        assert compute_file_overlap([], []) == 0.0

    def test_one_empty(self):
        assert compute_file_overlap(["a.go"], []) == 0.0

    def test_single_common(self):
        assert compute_file_overlap(["a.go"], ["a.go"]) == 1.0

    def test_subset(self):
        result = compute_file_overlap(["a.go"], ["a.go", "b.go"])
        assert abs(result - 0.5) < 0.01


# ---------------------------------------------------------------------------
# compute_metrics tests
# ---------------------------------------------------------------------------


class TestComputeMetrics:
    def test_identical_diffs(self):
        agent = parse_unified_diff(SINGLE_FILE_DIFF)
        human = parse_unified_diff(SINGLE_FILE_DIFF)
        m = compute_metrics(agent, human)
        assert m.file_overlap == 1.0
        assert m.files_both == ["pkg/controller/reconciler.go"]
        assert m.files_only_agent == []
        assert m.files_only_human == []
        assert m.similarity_score > 0.9

    def test_disjoint_files(self):
        agent = parse_unified_diff(SINGLE_FILE_DIFF)
        human_diff = """\
diff --git a/other/file.go b/other/file.go
--- a/other/file.go
+++ b/other/file.go
@@ -1,3 +1,4 @@
 line1
+added
 line2
"""
        human = parse_unified_diff(human_diff)
        m = compute_metrics(agent, human)
        assert m.file_overlap == 0.0
        assert m.files_both == []
        assert len(m.files_only_agent) == 1
        assert len(m.files_only_human) == 1

    def test_complexity_delta(self):
        agent = parse_unified_diff(TWO_FILE_DIFF)
        human = parse_unified_diff(HUMAN_DIFF)
        m = compute_metrics(agent, human)
        agent_total = agent.total_lines_added + agent.total_lines_removed
        human_total = human.total_lines_added + human.total_lines_removed
        assert m.complexity_delta == agent_total - human_total

    def test_empty_diffs(self):
        agent = parse_unified_diff("")
        human = parse_unified_diff("")
        m = compute_metrics(agent, human)
        assert m.file_overlap == 0.0
        assert m.similarity_score == 0.0

    def test_agent_has_more_files(self):
        agent = parse_unified_diff(TWO_FILE_DIFF)
        human = parse_unified_diff(SINGLE_FILE_DIFF)
        m = compute_metrics(agent, human)
        assert "pkg/controller/reconciler.go" in m.files_both
        assert "pkg/controller/reconciler_test.go" in m.files_only_agent
        assert m.files_only_human == []

    def test_line_counts_populated(self):
        agent = parse_unified_diff(SINGLE_FILE_DIFF)
        human = parse_unified_diff(HUMAN_DIFF)
        m = compute_metrics(agent, human)
        assert m.agent_lines_added == 4
        assert m.agent_lines_removed == 1
        assert m.human_lines_added == 3
        assert m.human_lines_removed == 0


# ---------------------------------------------------------------------------
# build_comparison tests
# ---------------------------------------------------------------------------


class TestBuildComparison:
    def test_disabled_when_no_comparison_data(self):
        result = build_comparison(_make_execution())
        assert result.enabled is False

    def test_enabled_with_comparison_ref(self):
        result = build_comparison(
            _make_execution(
                comparison_ref="abc123",
                comparison={"agent_diff": "", "human_diff": ""},
            )
        )
        assert result.enabled is True
        assert result.comparison_ref == "abc123"

    def test_enabled_with_comparison_data_no_ref(self):
        result = build_comparison(
            _make_execution(comparison={"agent_diff": SINGLE_FILE_DIFF, "human_diff": HUMAN_DIFF})
        )
        assert result.enabled is True

    def test_parses_diffs(self):
        result = build_comparison(
            _make_execution(
                comparison_ref="human-fix",
                comparison={
                    "agent_diff": SINGLE_FILE_DIFF,
                    "human_diff": HUMAN_DIFF,
                },
            )
        )
        assert result.agent_summary.total_files == 1
        assert result.human_summary.total_files == 1
        assert result.metrics.file_overlap == 1.0

    def test_preserves_analysis(self):
        result = build_comparison(
            _make_execution(
                comparison_ref="x",
                comparison={
                    "agent_diff": "",
                    "human_diff": "",
                    "analysis": "Both fixes add a nil check.",
                },
            )
        )
        assert result.analysis == "Both fixes add a nil check."

    def test_preserves_similarity_score_from_record(self):
        result = build_comparison(
            _make_execution(
                comparison_ref="x",
                comparison={
                    "agent_diff": SINGLE_FILE_DIFF,
                    "human_diff": HUMAN_DIFF,
                    "similarity_score": 0.95,
                },
            )
        )
        assert result.metrics.similarity_score == 0.95

    def test_preserves_test_comparison(self):
        result = build_comparison(
            _make_execution(
                comparison_ref="x",
                comparison={
                    "agent_diff": "",
                    "human_diff": "",
                    "test_comparison": {
                        "agent_tests_pass": True,
                        "human_tests_pass": True,
                        "same_tests_affected": True,
                    },
                },
            )
        )
        assert result.test_comparison["agent_tests_pass"] is True

    def test_flat_execution_dict(self):
        wrapped = _make_execution(
            comparison_ref="abc",
            comparison={"agent_diff": SINGLE_FILE_DIFF, "human_diff": ""},
        )
        flat = wrapped["execution"]
        result = build_comparison(flat)
        assert result.enabled is True
        assert result.agent_summary.total_files == 1

    def test_empty_execution(self):
        result = build_comparison({})
        assert result.enabled is False

    def test_to_dict_roundtrip(self):
        result = build_comparison(
            _make_execution(
                comparison_ref="ref",
                comparison={
                    "agent_diff": SINGLE_FILE_DIFF,
                    "human_diff": HUMAN_DIFF,
                    "analysis": "Similar approach",
                    "similarity_score": 0.8,
                },
            )
        )
        d = result.to_dict()
        assert d["enabled"] is True
        assert d["comparison_ref"] == "ref"
        assert d["agent_summary"]["total_files"] == 1
        assert d["human_summary"]["total_files"] == 1
        assert d["metrics"]["similarity_score"] == 0.8
        assert d["analysis"] == "Similar approach"


# ---------------------------------------------------------------------------
# ReportGenerator integration tests
# ---------------------------------------------------------------------------


class TestReportGeneratorIntegration:
    def test_extract_report_data_includes_comparison(self):
        data = extract_report_data(
            _make_execution(
                comparison_ref="human-ref",
                comparison={
                    "agent_diff": SINGLE_FILE_DIFF,
                    "human_diff": HUMAN_DIFF,
                },
            )
        )
        assert data.comparison["enabled"] is True
        assert data.comparison["metrics"]["file_overlap"] == 1.0

    def test_extract_report_data_disabled_comparison(self):
        data = extract_report_data(_make_execution())
        assert data.comparison["enabled"] is False

    def test_report_data_to_dict_includes_comparison(self):
        data = extract_report_data(
            _make_execution(
                comparison_ref="ref",
                comparison={"agent_diff": "", "human_diff": ""},
            )
        )
        d = data.to_dict()
        assert "comparison" in d
        assert d["comparison"]["enabled"] is True

    def test_generate_html_with_comparison(self):
        gen = ReportGenerator()
        html = gen.generate(
            _make_execution(
                comparison_ref="human-fix-ref",
                comparison={
                    "agent_diff": SINGLE_FILE_DIFF,
                    "human_diff": HUMAN_DIFF,
                    "analysis": "Both fixes add a nil check before processing.",
                    "similarity_score": 0.85,
                },
            )
        )
        assert "Comparison: Agent vs Human Fix" in html
        assert "COMPARISON MODE" in html
        assert "human-fix-r" in html
        assert "Similarity Score" in html
        assert "File Overlap" in html
        assert "Both fixes add a nil check" in html
        assert "pkg/controller/reconciler.go" in html

    def test_generate_html_without_comparison(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "Comparison: Agent vs Human Fix" not in html

    def test_generate_html_comparison_with_test_data(self):
        gen = ReportGenerator()
        html = gen.generate(
            _make_execution(
                comparison_ref="ref",
                comparison={
                    "agent_diff": SINGLE_FILE_DIFF,
                    "human_diff": HUMAN_DIFF,
                    "test_comparison": {
                        "agent_tests_pass": True,
                        "human_tests_pass": True,
                    },
                },
            )
        )
        assert "Test Results" in html
        assert "agent_tests_pass" in html

    def test_generate_html_comparison_raw_diffs(self):
        gen = ReportGenerator()
        html = gen.generate(
            _make_execution(
                comparison_ref="ref",
                comparison={
                    "agent_diff": SINGLE_FILE_DIFF,
                    "human_diff": HUMAN_DIFF,
                },
            )
        )
        assert "Agent Diff" in html
        assert "Human Diff" in html
