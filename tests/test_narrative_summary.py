"""Tests for the narrative summary landing page (Phase 9.5).

Covers:
  - NarrativeSummaryBuilder: story generation, metrics cards, phase timeline
  - LandingData, MetricCard, PhaseBar: dataclasses and serialisation
  - build_landing: module-level convenience
  - Helpers: issue desc, triage/impl/review info extraction, formatting
  - Report template: landing page section, enter-3D button, phase bars
  - ReportGenerator: landing_data in ReportData
  - Publisher: build_narrative delegates to NarrativeSummaryBuilder
"""

from __future__ import annotations

from pathlib import Path

from engine.visualization.narrative.summary import (
    PHASE_COLORS,
    LandingData,
    MetricCard,
    NarrativeSummaryBuilder,
    PhaseBar,
    _count_files_modified,
    _count_tests_run,
    _extract_impl_info,
    _extract_issue_desc,
    _extract_review_info,
    _extract_triage_info,
    _format_duration_display,
    _format_ms,
    build_landing,
)
from engine.visualization.report_generator import ReportGenerator, extract_report_data

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "visual-report"
REPORT_HTML = TEMPLATES_DIR / "report.html"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _exec(
    *,
    status: str = "success",
    source_url: str = "https://github.com/org/repo/issues/42",
    repo: str = "org/repo",
    triage_cls: str = "bug",
    triage_confidence: float | None = 0.90,
    triage_escalate: bool = False,
    triage_escalation_reason: str = "",
    triage_components: list[str] | None = None,
    impl_count: int = 1,
    impl_success: bool = True,
    impl_files: list[str] | None = None,
    review_verdict: str = "approve",
    review_success: bool = True,
    pr_url: str = "",
    total_ms: float = 120_000,
    time_per_phase: dict[str, float] | None = None,
    actions: list[dict] | None = None,
    include_triage: bool = True,
    include_impl: bool = True,
    include_review: bool = True,
) -> dict:
    """Build an execution record with sensible defaults for landing page tests."""
    iterations: list[dict] = []

    if include_triage:
        findings: dict = {}
        result: dict = {"success": not triage_escalate, "escalate": triage_escalate}
        if triage_cls:
            findings["classification"] = triage_cls
        if triage_confidence is not None:
            findings["confidence"] = triage_confidence
        if triage_components:
            findings["affected_components"] = triage_components
        if triage_escalation_reason:
            result["escalation_reason"] = triage_escalation_reason
        iterations.append(
            {
                "number": len(iterations) + 1,
                "phase": "triage",
                "started_at": "2026-03-29T10:00:00Z",
                "duration_ms": 5000,
                "result": result,
                "findings": findings,
            }
        )

    if include_impl:
        for i in range(impl_count):
            is_last = i == impl_count - 1
            arts = {}
            if impl_files and is_last:
                arts["files_changed"] = impl_files
            iterations.append(
                {
                    "number": len(iterations) + 1,
                    "phase": "implement",
                    "started_at": f"2026-03-29T10:01:{i:02d}Z",
                    "duration_ms": 30000,
                    "result": {"success": impl_success if is_last else False},
                    "artifacts": arts,
                }
            )

    if include_review:
        iterations.append(
            {
                "number": len(iterations) + 1,
                "phase": "review",
                "started_at": "2026-03-29T10:02:00Z",
                "duration_ms": 10000,
                "result": {"success": review_success},
                "findings": {"verdict": review_verdict},
            }
        )

    return {
        "execution": {
            "id": "test-landing-id",
            "trigger": {"type": "github_issue", "source_url": source_url},
            "target": {"repo": repo},
            "iterations": iterations,
            "result": {"status": status, "pr_url": pr_url, "total_iterations": len(iterations)},
            "metrics": {
                "total_duration_ms": total_ms,
                "total_llm_calls": 3,
                "total_tokens_in": 5000,
                "total_tokens_out": 1500,
                "total_tool_executions": 7,
                "time_per_phase_ms": time_per_phase
                or {"triage": 5000, "implement": 30000, "review": 10000},
                "phase_iteration_counts": {
                    "triage": 1,
                    "implement": impl_count,
                    "review": 1,
                },
            },
            "actions": actions or [],
            "config": {},
        }
    }


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestMetricCard:
    def test_to_dict(self):
        card = MetricCard(label="Time", value="2.5m", unit="", status="success")
        d = card.to_dict()
        assert d["label"] == "Time"
        assert d["value"] == "2.5m"
        assert d["status"] == "success"

    def test_defaults(self):
        card = MetricCard()
        assert card.label == ""
        assert card.value == ""


class TestPhaseBar:
    def test_to_dict(self):
        bar = PhaseBar(phase="triage", duration_ms=5000, percent=25.0, color="#58a6ff")
        d = bar.to_dict()
        assert d["phase"] == "triage"
        assert d["percent"] == 25.0
        assert d["color"] == "#58a6ff"

    def test_rounding(self):
        bar = PhaseBar(duration_ms=1234.567, percent=33.333)
        d = bar.to_dict()
        assert d["duration_ms"] == 1234.57
        assert d["percent"] == 33.3


class TestLandingData:
    def test_to_dict(self):
        ld = LandingData(story="Test story", total_duration_display="2.0m")
        d = ld.to_dict()
        assert d["story"] == "Test story"
        assert d["total_duration_display"] == "2.0m"
        assert isinstance(d["metrics_cards"], list)
        assert isinstance(d["phase_bars"], list)

    def test_defaults(self):
        ld = LandingData()
        assert ld.story == ""
        assert ld.metrics_cards == []
        assert ld.comparison_summary == ""


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_issue_desc_with_url_and_repo(self):
        desc = _extract_issue_desc("https://github.com/org/repo/issues/42", "repo")
        assert "#42" in desc
        assert "repo" in desc

    def test_extract_issue_desc_no_repo(self):
        desc = _extract_issue_desc("https://github.com/org/repo/issues/42", "")
        assert "42" in desc

    def test_extract_issue_desc_no_url(self):
        desc = _extract_issue_desc("", "my-repo")
        assert "my-repo" in desc

    def test_extract_issue_desc_neither(self):
        desc = _extract_issue_desc("", "")
        assert "bug-fix loop" in desc

    def test_extract_issue_desc_no_issue_number(self):
        desc = _extract_issue_desc("https://github.com/org/repo", "repo")
        assert "repo" in desc

    def test_extract_triage_info_bug(self):
        iterations = [{"phase": "triage", "findings": {"classification": "bug"}, "result": {}}]
        info = _extract_triage_info(iterations)
        assert "bug" in info

    def test_extract_triage_info_with_confidence(self):
        iterations = [
            {
                "phase": "triage",
                "findings": {"classification": "bug", "confidence": 0.92},
                "result": {},
            }
        ]
        info = _extract_triage_info(iterations)
        assert "0.92" in info

    def test_extract_triage_info_with_components(self):
        iterations = [
            {
                "phase": "triage",
                "findings": {
                    "classification": "bug",
                    "affected_components": ["pkg/foo.go", "pkg/bar.go"],
                },
                "result": {},
            }
        ]
        info = _extract_triage_info(iterations)
        assert "pkg/foo.go" in info

    def test_extract_triage_info_escalation(self):
        iterations = [
            {
                "phase": "triage",
                "findings": {},
                "result": {"escalate": True, "escalation_reason": "ambiguous"},
            }
        ]
        info = _extract_triage_info(iterations)
        assert "escalated" in info
        assert "ambiguous" in info

    def test_extract_triage_info_empty(self):
        assert _extract_triage_info([]) == ""

    def test_extract_impl_info_success(self):
        iterations = [{"phase": "implement", "result": {"success": True}, "artifacts": {}}]
        info = _extract_impl_info(iterations)
        assert "1 attempt" in info
        assert "failed" not in info

    def test_extract_impl_info_multiple_failure(self):
        iterations = [
            {"phase": "implement", "result": {"success": False}, "artifacts": {}},
            {"phase": "implement", "result": {"success": False}, "artifacts": {}},
        ]
        info = _extract_impl_info(iterations)
        assert "2 attempts" in info
        assert "failed" in info

    def test_extract_impl_info_with_files(self):
        iterations = [
            {
                "phase": "implement",
                "result": {"success": True},
                "artifacts": {"files_changed": ["a.go", "b.go"]},
            }
        ]
        info = _extract_impl_info(iterations)
        assert "a.go" in info

    def test_extract_impl_info_empty(self):
        assert _extract_impl_info([]) == ""

    def test_extract_review_info_approve(self):
        iterations = [
            {"phase": "review", "findings": {"verdict": "approve"}, "result": {"success": True}}
        ]
        info = _extract_review_info(iterations)
        assert "approved" in info

    def test_extract_review_info_block(self):
        iterations = [
            {
                "phase": "review",
                "findings": {"verdict": "block", "summary": "injection"},
                "result": {"success": False},
            }
        ]
        info = _extract_review_info(iterations)
        assert "blocked" in info
        assert "injection" in info

    def test_extract_review_info_request_changes(self):
        iterations = [
            {
                "phase": "review",
                "findings": {"verdict": "request_changes"},
                "result": {"success": False},
            }
        ]
        info = _extract_review_info(iterations)
        assert "requested changes" in info

    def test_extract_review_info_empty(self):
        assert _extract_review_info([]) == ""

    def test_extract_review_info_multi_reviews(self):
        iterations = [
            {
                "phase": "review",
                "findings": {"verdict": "request_changes"},
                "result": {"success": False},
            },
            {
                "phase": "review",
                "findings": {"verdict": "approve"},
                "result": {"success": True},
            },
        ]
        info = _extract_review_info(iterations)
        assert "attempt 2" in info

    def test_count_files_modified(self):
        actions = [
            {"action_type": "file_write", "input": {"path": "a.go"}},
            {"action_type": "file_write", "input": {"path": "b.go"}},
            {"action_type": "file_write", "input": {"path": "a.go"}},
            {"action_type": "file_read", "input": {"path": "c.go"}},
        ]
        assert _count_files_modified(actions) == 2

    def test_count_tests_run(self):
        actions = [
            {"action_type": "shell_run", "input": {"description": "Run pytest"}},
            {"action_type": "command_run", "input": {"command": "go test ./..."}},
            {"action_type": "shell_run", "input": {"description": "Run linter"}},
        ]
        assert _count_tests_run(actions) == 2

    def test_format_ms(self):
        assert _format_ms(500) == "500ms"
        assert _format_ms(1500) == "1.5s"
        assert _format_ms(90_000) == "1.5m"
        assert _format_ms(5_400_000) == "1.5h"

    def test_format_duration_display(self):
        assert _format_duration_display({"metrics": {"total_duration_ms": 120_000}}) == "2.0m"
        assert _format_duration_display({"metrics": {}}) == "—"
        assert _format_duration_display({}) == "—"


# ---------------------------------------------------------------------------
# NarrativeSummaryBuilder.build_story tests
# ---------------------------------------------------------------------------


class TestBuildStory:
    def test_success_with_all_phases(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec()["execution"])
        assert "#42" in story
        assert "repo" in story
        assert "bug" in story
        assert "approved" in story
        assert "successfully" in story

    def test_failure_status(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(status="failure", impl_success=False)["execution"])
        assert "failure" in story.lower()
        assert "failed" in story.lower()

    def test_escalated_status(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(status="escalated")["execution"])
        assert "escalated" in story.lower()

    def test_timeout_status(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(status="timeout")["execution"])
        assert "timed out" in story.lower()

    def test_pr_url_in_story(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(
            _exec(pr_url="https://github.com/org/repo/pull/99")["execution"]
        )
        assert "pull/99" in story

    def test_timing_in_story(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(total_ms=120_000)["execution"])
        assert "2.0m" in story
        assert "phase" in story

    def test_no_triage(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(include_triage=False)["execution"])
        assert "bug" not in story
        assert "repo" in story

    def test_no_implementation(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(include_impl=False)["execution"])
        assert "attempt" not in story

    def test_no_review(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(include_review=False)["execution"])
        assert "approved" not in story
        assert "blocked" not in story

    def test_empty_execution(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story({})
        assert "bug-fix loop" in story

    def test_multiple_attempts(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(impl_count=3)["execution"])
        assert "3 attempts" in story

    def test_triage_escalation(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(
            _exec(
                triage_cls="",
                triage_escalate=True,
                triage_escalation_reason="feature request",
            )["execution"]
        )
        assert "escalated" in story.lower()
        assert "feature request" in story

    def test_confidence_omitted_when_none(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(triage_confidence=None)["execution"])
        assert "confidence" not in story.lower()

    def test_with_components(self):
        builder = NarrativeSummaryBuilder()
        story = builder.build_story(_exec(triage_components=["pkg/reconciler.go"])["execution"])
        assert "reconciler.go" in story


# ---------------------------------------------------------------------------
# NarrativeSummaryBuilder.build_metrics_cards tests
# ---------------------------------------------------------------------------


class TestBuildMetricsCards:
    def test_basic_cards(self):
        builder = NarrativeSummaryBuilder()
        cards = builder.build_metrics_cards(_exec()["execution"])
        labels = [c.label for c in cards]
        assert "Total Time" in labels
        assert "Iterations" in labels
        assert "LLM Calls" in labels
        assert "Status" in labels
        assert "Files Modified" in labels
        assert "Tests Run" in labels

    def test_token_card_present_when_nonzero(self):
        builder = NarrativeSummaryBuilder()
        cards = builder.build_metrics_cards(_exec()["execution"])
        labels = [c.label for c in cards]
        assert "Total Tokens" in labels

    def test_token_card_absent_when_zero(self):
        builder = NarrativeSummaryBuilder()
        exec_data = _exec()["execution"]
        exec_data["metrics"]["total_tokens_in"] = 0
        exec_data["metrics"]["total_tokens_out"] = 0
        cards = builder.build_metrics_cards(exec_data)
        labels = [c.label for c in cards]
        assert "Total Tokens" not in labels

    def test_status_on_cards(self):
        builder = NarrativeSummaryBuilder()
        cards = builder.build_metrics_cards(_exec(status="failure")["execution"])
        assert all(c.status == "failure" for c in cards)

    def test_time_display(self):
        builder = NarrativeSummaryBuilder()
        cards = builder.build_metrics_cards(_exec(total_ms=120_000)["execution"])
        time_card = next(c for c in cards if c.label == "Total Time")
        assert time_card.value == "2.0m"

    def test_no_time(self):
        builder = NarrativeSummaryBuilder()
        cards = builder.build_metrics_cards(_exec(total_ms=0)["execution"])
        time_card = next(c for c in cards if c.label == "Total Time")
        assert time_card.value == "—"

    def test_files_modified_from_actions(self):
        actions = [
            {"action_type": "file_write", "input": {"path": "a.go"}},
            {"action_type": "file_write", "input": {"path": "b.go"}},
        ]
        builder = NarrativeSummaryBuilder()
        cards = builder.build_metrics_cards(_exec(actions=actions)["execution"])
        files_card = next(c for c in cards if c.label == "Files Modified")
        assert files_card.value == "2"

    def test_to_dict_serialisation(self):
        builder = NarrativeSummaryBuilder()
        cards = builder.build_metrics_cards(_exec()["execution"])
        for card in cards:
            d = card.to_dict()
            assert "label" in d
            assert "value" in d


# ---------------------------------------------------------------------------
# NarrativeSummaryBuilder.build_phase_timeline tests
# ---------------------------------------------------------------------------


class TestBuildPhaseTimeline:
    def test_basic_bars(self):
        builder = NarrativeSummaryBuilder()
        bars = builder.build_phase_timeline(_exec()["execution"])
        phases = [b.phase for b in bars]
        assert "triage" in phases
        assert "implement" in phases
        assert "review" in phases

    def test_bar_percentages_sum_to_100(self):
        builder = NarrativeSummaryBuilder()
        bars = builder.build_phase_timeline(_exec()["execution"])
        total = sum(b.percent for b in bars)
        assert abs(total - 100.0) < 1.0

    def test_bar_colors(self):
        builder = NarrativeSummaryBuilder()
        bars = builder.build_phase_timeline(_exec()["execution"])
        for bar in bars:
            assert bar.color == PHASE_COLORS.get(bar.phase, "#6b7280")

    def test_bar_status(self):
        builder = NarrativeSummaryBuilder()
        bars = builder.build_phase_timeline(_exec()["execution"])
        triage_bar = next(b for b in bars if b.phase == "triage")
        assert triage_bar.status == "success"

    def test_bar_iterations(self):
        builder = NarrativeSummaryBuilder()
        bars = builder.build_phase_timeline(_exec(impl_count=3)["execution"])
        impl_bar = next(b for b in bars if b.phase == "implement")
        assert impl_bar.iterations == 3

    def test_empty_execution(self):
        builder = NarrativeSummaryBuilder()
        bars = builder.build_phase_timeline({})
        assert bars == []

    def test_fallback_to_iteration_durations(self):
        builder = NarrativeSummaryBuilder()
        exec_data = _exec()["execution"]
        exec_data["metrics"]["time_per_phase_ms"] = {}
        bars = builder.build_phase_timeline(exec_data)
        assert len(bars) > 0
        assert any(b.duration_ms > 0 for b in bars)

    def test_to_dict_serialisation(self):
        builder = NarrativeSummaryBuilder()
        bars = builder.build_phase_timeline(_exec()["execution"])
        for bar in bars:
            d = bar.to_dict()
            assert "phase" in d
            assert "percent" in d
            assert "duration_ms" in d


# ---------------------------------------------------------------------------
# NarrativeSummaryBuilder.build_landing (full pipeline) tests
# ---------------------------------------------------------------------------


class TestBuildLanding:
    def test_full_pipeline(self):
        builder = NarrativeSummaryBuilder()
        landing = builder.build_landing(_exec())
        assert landing.story != ""
        assert len(landing.metrics_cards) > 0
        assert len(landing.phase_bars) > 0
        assert landing.total_duration_display != "—"

    def test_to_dict(self):
        builder = NarrativeSummaryBuilder()
        landing = builder.build_landing(_exec())
        d = landing.to_dict()
        assert "story" in d
        assert "metrics_cards" in d
        assert "phase_bars" in d
        assert "total_duration_display" in d

    def test_comparison_summary(self):
        exec_data = _exec()
        exec_data["execution"]["result"]["comparison"] = {
            "similarity_score": 0.85,
            "analysis": "Both fixes add a nil check. The agent's approach is equivalent.",
        }
        builder = NarrativeSummaryBuilder()
        landing = builder.build_landing(exec_data)
        assert "85%" in landing.comparison_summary
        assert "nil check" in landing.comparison_summary

    def test_no_comparison(self):
        builder = NarrativeSummaryBuilder()
        landing = builder.build_landing(_exec())
        assert landing.comparison_summary == ""

    def test_empty_execution(self):
        builder = NarrativeSummaryBuilder()
        landing = builder.build_landing({})
        assert "bug-fix loop" in landing.story
        assert len(landing.metrics_cards) > 0


# ---------------------------------------------------------------------------
# Module-level build_landing convenience
# ---------------------------------------------------------------------------


class TestBuildLandingConvenience:
    def test_matches_class(self):
        exec_data = _exec()
        expected = NarrativeSummaryBuilder().build_landing(exec_data)
        actual = build_landing(exec_data)
        assert actual.story == expected.story
        assert actual.total_duration_display == expected.total_duration_display


# ---------------------------------------------------------------------------
# ReportData integration
# ---------------------------------------------------------------------------


class TestReportDataIntegration:
    def test_landing_data_in_report_data(self):
        data = extract_report_data(_exec())
        assert data.landing_data != {}
        assert "story" in data.landing_data
        assert data.landing_data["story"] != ""

    def test_landing_data_in_to_dict(self):
        data = extract_report_data(_exec())
        d = data.to_dict()
        assert "landing_data" in d
        assert "story" in d["landing_data"]

    def test_landing_data_has_metrics_and_bars(self):
        data = extract_report_data(_exec())
        assert len(data.landing_data.get("metrics_cards", [])) > 0
        assert len(data.landing_data.get("phase_bars", [])) > 0


# ---------------------------------------------------------------------------
# Report template integration
# ---------------------------------------------------------------------------


class TestReportTemplateIntegration:
    def test_landing_page_in_html(self, tmp_path):
        gen = ReportGenerator()
        html = gen.generate(_exec(), output_path=tmp_path / "report.html")
        assert "landing-page" in html
        assert "metric-card" in html

    def test_story_in_html(self, tmp_path):
        gen = ReportGenerator()
        html = gen.generate(_exec(), output_path=tmp_path / "report.html")
        assert "#42" in html
        assert "repo" in html

    def test_phase_bars_in_html(self, tmp_path):
        gen = ReportGenerator()
        html = gen.generate(_exec(), output_path=tmp_path / "report.html")
        assert "Time per Phase" in html
        assert "triage" in html

    def test_enter_3d_button_in_html(self, tmp_path):
        gen = ReportGenerator()
        html = gen.generate(_exec(), output_path=tmp_path / "report.html")
        assert "enter-3d-btn" in html
        assert "Enter 3D View" in html

    def test_comparison_summary_in_html(self, tmp_path):
        exec_data = _exec()
        exec_data["execution"]["result"]["comparison"] = {
            "similarity_score": 0.75,
            "analysis": "Similar approach.",
        }
        gen = ReportGenerator()
        html = gen.generate(exec_data, output_path=tmp_path / "report.html")
        assert "75%" in html

    def test_fallback_when_no_landing(self, tmp_path):
        gen = ReportGenerator()
        html = gen.generate({}, output_path=tmp_path / "report.html")
        assert "bug-fix loop" in html

    def test_phase_bar_colors_in_html(self, tmp_path):
        gen = ReportGenerator()
        html = gen.generate(_exec(), output_path=tmp_path / "report.html")
        assert "#58a6ff" in html
        assert "#3fb950" in html

    def test_metrics_cards_values(self, tmp_path):
        gen = ReportGenerator()
        html = gen.generate(_exec(), output_path=tmp_path / "report.html")
        assert "2.0m" in html
        assert "LLM Calls" in html


# ---------------------------------------------------------------------------
# Publisher build_narrative delegates correctly
# ---------------------------------------------------------------------------


class TestPublisherDelegation:
    def test_build_narrative_uses_summary_builder(self):
        from engine.visualization.publisher import build_narrative

        text = build_narrative(_exec())
        assert "#42" in text
        assert "repo" in text
        assert "bug" in text

    def test_build_narrative_empty(self):
        from engine.visualization.publisher import build_narrative

        text = build_narrative({})
        assert "bug-fix loop" in text


# ---------------------------------------------------------------------------
# Package exports
# ---------------------------------------------------------------------------


class TestPackageExports:
    def test_narrative_package_exports(self):
        from engine.visualization.narrative import (  # noqa: F401
            LandingData,
            MetricCard,
            NarrativeSummaryBuilder,
            PhaseBar,
            build_landing,
        )

    def test_visualization_package_exports(self):
        from engine.visualization import (  # noqa: F401
            LandingData,
            MetricCard,
            NarrativeSummaryBuilder,
            PhaseBar,
            build_landing,
        )
