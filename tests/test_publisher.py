"""Tests for engine.visualization.publisher."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from engine.config import EngineConfig, ReportingConfig
from engine.integrations.llm import MockProvider
from engine.loop import RalphLoop
from engine.visualization.publisher import (
    PublishResult,
    ReportPublisher,
    _format_finding_value,
    _summarise_dict,
    build_artifact_manifest,
    build_narrative,
    build_summary_markdown,
    main,
    parse_args,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_execution(
    *,
    status: str = "success",
    iterations: list | None = None,
    actions: list | None = None,
    metrics: dict | None = None,
    exec_id: str = "pub-test-id-12345",
) -> dict:
    """Build a minimal execution record dict for testing."""
    return {
        "execution": {
            "id": exec_id,
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:05:00+00:00",
            "trigger": {
                "type": "github_issue",
                "source_url": "https://github.com/o/r/issues/1",
            },
            "target": {"repo_path": "/tmp/repo", "comparison_ref": ""},
            "config": {"llm": {"provider": "mock"}},
            "iterations": iterations
            or [
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
                },
            ],
            "result": {
                "status": status,
                "total_iterations": 1,
                "phase_results": [
                    {"phase": "triage", "success": True, "escalate": False},
                ],
            },
            "metrics": metrics
            or {
                "total_iterations": 1,
                "total_llm_calls": 2,
                "total_tokens_in": 3000,
                "total_tokens_out": 800,
                "total_tool_executions": 5,
                "time_per_phase_ms": {"triage": 1500.0},
                "phase_iteration_counts": {"triage": 1},
                "errors": [],
            },
            "actions": actions
            or [
                {
                    "id": "act-1",
                    "iteration": 1,
                    "phase": "triage",
                    "action_type": "llm_query",
                    "timestamp": "2026-03-25T10:00:05+00:00",
                    "input": {"description": "Classify issue", "context": {}},
                    "output": {"success": True, "data": {}},
                    "duration_ms": 500.0,
                    "llm_context": {
                        "model": "mock",
                        "tokens_in": 1000,
                        "tokens_out": 200,
                    },
                    "provenance": {},
                },
            ],
        }
    }


# ---------------------------------------------------------------------------
# PublishResult tests
# ---------------------------------------------------------------------------


class TestPublishResult:
    def test_defaults(self):
        r = PublishResult()
        assert r.success is True
        assert r.files_generated == []
        assert r.errors == []
        assert r.report_path == ""

    def test_to_dict(self):
        r = PublishResult(
            report_dir="/out",
            report_path="/out/report.html",
            success=True,
            files_generated=["/out/report.html"],
        )
        d = r.to_dict()
        assert d["report_dir"] == "/out"
        assert d["report_path"] == "/out/report.html"
        assert d["success"] is True
        assert len(d["files_generated"]) == 1

    def test_to_dict_with_errors(self):
        r = PublishResult(success=False, errors=["boom"])
        d = r.to_dict()
        assert d["success"] is False
        assert "boom" in d["errors"]


# ---------------------------------------------------------------------------
# build_summary_markdown tests
# ---------------------------------------------------------------------------


class TestBuildSummaryMarkdown:
    def test_basic_summary(self):
        md = build_summary_markdown(_make_execution())
        assert "pub-test-id-" in md
        assert "`success`" in md
        assert "**Iterations**: 1" in md

    def test_includes_issue_url(self):
        md = build_summary_markdown(_make_execution())
        assert "https://github.com/o/r/issues/1" in md

    def test_includes_metrics(self):
        md = build_summary_markdown(_make_execution())
        assert "LLM calls: 2" in md
        assert "Tokens in: 3,000" in md
        assert "Tokens out: 800" in md
        assert "Tool executions: 5" in md

    def test_includes_phases_table(self):
        md = build_summary_markdown(_make_execution())
        assert "| triage |" in md
        assert "PASS" in md

    def test_includes_errors(self):
        exec_data = _make_execution(
            metrics={
                "total_iterations": 1,
                "total_llm_calls": 0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_tool_executions": 0,
                "time_per_phase_ms": {},
                "phase_iteration_counts": {},
                "errors": ["Something broke"],
            }
        )
        md = build_summary_markdown(exec_data)
        assert "Something broke" in md

    def test_comparison_mode_note(self):
        config = ReportingConfig(comparison_mode=True)
        md = build_summary_markdown(_make_execution(), config=config)
        assert "Comparison report included" in md

    def test_no_comparison_mode_note_by_default(self):
        md = build_summary_markdown(_make_execution())
        assert "Comparison report included" not in md

    def test_failure_status(self):
        md = build_summary_markdown(_make_execution(status="failure"))
        assert "`failure`" in md

    def test_empty_execution(self):
        md = build_summary_markdown({})
        assert "unknown" in md

    def test_no_timestamps(self):
        exec_data = _make_execution()
        exec_data["execution"]["started_at"] = ""
        exec_data["execution"]["completed_at"] = ""
        md = build_summary_markdown(exec_data)
        assert "**Started**" not in md
        assert "**Completed**" not in md


# ---------------------------------------------------------------------------
# build_artifact_manifest tests
# ---------------------------------------------------------------------------


class TestBuildArtifactManifest:
    def test_basic_manifest(self):
        m = build_artifact_manifest(_make_execution(), ["/out/report.html"])
        assert m["execution_id"] == "pub-test-id-12345"
        assert m["status"] == "success"
        assert "/out/report.html" in m["files"]
        assert "generated_at" in m

    def test_config_flags(self):
        config = ReportingConfig(
            decision_tree=True,
            action_map=False,
            comparison_mode=True,
            publish_to_pages=True,
            artifact_retention_days=7,
        )
        m = build_artifact_manifest(_make_execution(), [], config)
        assert m["config"]["decision_tree"] is True
        assert m["config"]["action_map"] is False
        assert m["config"]["comparison_mode"] is True
        assert m["config"]["publish_to_pages"] is True
        assert m["config"]["artifact_retention_days"] == 7

    def test_default_config(self):
        m = build_artifact_manifest(_make_execution(), [])
        assert m["config"]["decision_tree"] is True
        assert m["config"]["comparison_mode"] is False

    def test_flat_execution(self):
        flat = _make_execution()["execution"]
        m = build_artifact_manifest(flat, [])
        assert m["execution_id"] == "pub-test-id-12345"

    def test_empty_execution(self):
        m = build_artifact_manifest({}, [])
        assert m["execution_id"] == ""
        assert m["status"] == "unknown"


# ---------------------------------------------------------------------------
# ReportPublisher tests
# ---------------------------------------------------------------------------


class TestReportPublisher:
    def test_publish_creates_output_dir(self, tmp_path):
        out = tmp_path / "nested" / "reports"
        pub = ReportPublisher(output_dir=out)
        result = pub.publish(_make_execution())
        assert out.exists()
        assert result.report_dir == str(out)

    def test_publish_generates_report_html(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        result = pub.publish(_make_execution())
        report = tmp_path / "report.html"
        assert report.exists()
        content = report.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "pub-test-id" in content
        assert result.report_path == str(report)

    def test_publish_generates_summary_md(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        result = pub.publish(_make_execution())
        summary = tmp_path / "summary.md"
        assert summary.exists()
        content = summary.read_text(encoding="utf-8")
        assert "pub-test-id" in content
        assert "`success`" in content
        assert result.summary_path == str(summary)

    def test_publish_generates_manifest(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        result = pub.publish(_make_execution())
        manifest = tmp_path / "artifact-manifest.json"
        assert manifest.exists()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["execution_id"] == "pub-test-id-12345"
        assert len(data["files"]) >= 2
        assert result.manifest_path == str(manifest)

    def test_publish_returns_success(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        result = pub.publish(_make_execution())
        assert result.success is True
        assert len(result.files_generated) == 3
        assert len(result.errors) == 0

    def test_publish_files_listed_in_manifest(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        result = pub.publish(_make_execution())
        manifest_data = json.loads(
            (tmp_path / "artifact-manifest.json").read_text(encoding="utf-8")
        )
        for f in result.files_generated:
            if "manifest" not in f:
                assert f in manifest_data["files"]

    def test_publish_with_comparison_mode(self, tmp_path):
        config = ReportingConfig(comparison_mode=True)
        pub = ReportPublisher(output_dir=tmp_path, config=config)
        result = pub.publish(_make_execution())
        assert result.success is True
        summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
        assert "Comparison report included" in summary

    def test_publish_from_file(self, tmp_path):
        exec_json = tmp_path / "execution.json"
        exec_json.write_text(json.dumps(_make_execution()), encoding="utf-8")
        out = tmp_path / "reports"
        pub = ReportPublisher(output_dir=out)
        result = pub.publish_from_file(exec_json)
        assert result.success is True
        assert (out / "report.html").exists()

    def test_publish_from_file_not_found(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        with pytest.raises(FileNotFoundError):
            pub.publish_from_file(tmp_path / "nope.json")

    def test_publish_from_file_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        pub = ReportPublisher(output_dir=tmp_path)
        with pytest.raises(json.JSONDecodeError):
            pub.publish_from_file(bad)

    def test_output_dir_property(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        assert pub.output_dir == tmp_path

    def test_config_property(self):
        config = ReportingConfig(comparison_mode=True)
        pub = ReportPublisher(output_dir="/tmp", config=config)
        assert pub.config.comparison_mode is True

    def test_default_config(self):
        pub = ReportPublisher(output_dir="/tmp")
        assert pub.config.decision_tree is True
        assert pub.config.comparison_mode is False

    def test_publish_empty_execution(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        result = pub.publish({})
        assert result.success is True
        assert (tmp_path / "report.html").exists()
        assert (tmp_path / "summary.md").exists()


# ---------------------------------------------------------------------------
# ReportPublisher error handling tests
# ---------------------------------------------------------------------------


class TestPublisherErrorHandling:
    def test_report_generation_failure_recorded(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        pub._generator = _BrokenGenerator()
        result = pub.publish(_make_execution())
        assert result.success is False
        assert any("Failed to generate report.html" in e for e in result.errors)
        assert (tmp_path / "summary.md").exists()

    def test_summary_failure_non_blocking(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        with patch(
            "engine.visualization.publisher.build_summary_markdown",
            side_effect=RuntimeError("summary broke"),
        ):
            result = pub.publish(_make_execution())
        assert (tmp_path / "report.html").exists()
        assert any("Failed to generate summary.md" in e for e in result.errors)


class _BrokenGenerator:
    """A ReportGenerator substitute that always raises."""

    def generate(self, *args, **kwargs):
        raise RuntimeError("template engine exploded")


# ---------------------------------------------------------------------------
# CLI parse_args tests
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_required_args(self):
        args = parse_args(["--execution-log", "ex.json", "--output-dir", "/out"])
        assert args.execution_log == "ex.json"
        assert args.output_dir == "/out"
        assert args.comparison_mode is False

    def test_comparison_mode_flag(self):
        args = parse_args(["--execution-log", "e.json", "--output-dir", "/o", "--comparison-mode"])
        assert args.comparison_mode is True

    def test_missing_required_exits(self):
        with pytest.raises(SystemExit):
            parse_args(["--output-dir", "/out"])

    def test_missing_output_dir_exits(self):
        with pytest.raises(SystemExit):
            parse_args(["--execution-log", "e.json"])


# ---------------------------------------------------------------------------
# CLI main() tests
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_success(self, tmp_path):
        exec_json = tmp_path / "execution.json"
        exec_json.write_text(json.dumps(_make_execution()), encoding="utf-8")
        out = tmp_path / "reports"
        rc = main(["--execution-log", str(exec_json), "--output-dir", str(out)])
        assert rc == 0
        assert (out / "report.html").exists()

    def test_main_file_not_found(self, tmp_path):
        rc = main(
            [
                "--execution-log",
                str(tmp_path / "nope.json"),
                "--output-dir",
                str(tmp_path),
            ]
        )
        assert rc == 1

    def test_main_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{broken", encoding="utf-8")
        rc = main(["--execution-log", str(bad), "--output-dir", str(tmp_path)])
        assert rc == 1

    def test_main_with_comparison_mode(self, tmp_path):
        exec_json = tmp_path / "execution.json"
        exec_json.write_text(json.dumps(_make_execution()), encoding="utf-8")
        out = tmp_path / "reports"
        rc = main(
            [
                "--execution-log",
                str(exec_json),
                "--output-dir",
                str(out),
                "--comparison-mode",
            ]
        )
        assert rc == 0
        summary = (out / "summary.md").read_text(encoding="utf-8")
        assert "Comparison report included" in summary


# ---------------------------------------------------------------------------
# build_narrative tests
# ---------------------------------------------------------------------------


def _make_full_execution(
    *,
    status: str = "success",
    source_url: str = "https://github.com/org/repo/issues/42",
    repo_path: str = "/tmp/repo",
    triage_findings: dict | None = None,
    impl_count: int = 1,
    impl_success: bool = True,
    review_findings: dict | None = None,
    review_success: bool = True,
) -> dict:
    """Build an execution record with controllable phase iterations for narrative tests."""
    iterations: list[dict] = []

    if triage_findings is not None:
        iterations.append(
            {
                "number": 1,
                "phase": "triage",
                "result": {"success": True, "escalate": False},
                "findings": triage_findings,
            }
        )

    for i in range(impl_count):
        is_last = i == impl_count - 1
        iterations.append(
            {
                "number": len(iterations) + 1,
                "phase": "implement",
                "result": {"success": impl_success if is_last else False},
            }
        )

    if review_findings is not None:
        iterations.append(
            {
                "number": len(iterations) + 1,
                "phase": "review",
                "result": {"success": review_success},
                "findings": review_findings,
            }
        )

    return {
        "execution": {
            "id": "narr-test-id",
            "trigger": {"type": "github_issue", "source_url": source_url},
            "target": {"repo_path": repo_path},
            "iterations": iterations,
            "result": {"status": status},
            "metrics": {},
            "actions": [],
            "config": {},
        }
    }


class TestBuildNarrative:
    def test_basic_success(self):
        text = build_narrative(
            _make_full_execution(
                triage_findings={"classification": "bug", "confidence": 0.85},
                review_findings={"verdict": "approve"},
            )
        )
        assert "repo" in text
        assert "issues/42" in text
        assert "bug" in text
        assert "0.85" in text
        assert "approved" in text
        assert "success" in text.lower()

    def test_failure_status(self):
        text = build_narrative(
            _make_full_execution(
                status="failure",
                impl_success=False,
                triage_findings={"classification": "bug"},
            )
        )
        assert "failure" in text.lower()
        assert "failed" in text.lower()

    def test_escalated_status(self):
        text = build_narrative(_make_full_execution(status="escalated"))
        assert "escalated" in text.lower()

    def test_timeout_status(self):
        text = build_narrative(_make_full_execution(status="timeout"))
        assert "timed out" in text.lower()

    def test_multiple_impl_attempts(self):
        text = build_narrative(
            _make_full_execution(impl_count=5, triage_findings={"classification": "bug"})
        )
        assert "5 attempts" in text

    def test_single_impl_attempt(self):
        text = build_narrative(
            _make_full_execution(impl_count=1, triage_findings={"classification": "bug"})
        )
        assert "1 attempt" in text
        assert "attempts" not in text

    def test_review_block(self):
        text = build_narrative(
            _make_full_execution(
                review_findings={"verdict": "block", "summary": "injection concern"},
                review_success=False,
            )
        )
        assert "blocked" in text.lower()
        assert "injection concern" in text

    def test_review_request_changes(self):
        text = build_narrative(
            _make_full_execution(
                review_findings={"verdict": "request_changes"},
                review_success=False,
            )
        )
        assert "requested changes" in text

    def test_triage_escalation(self):
        exec_data = {
            "execution": {
                "id": "t1",
                "trigger": {"source_url": "https://github.com/o/r/issues/1"},
                "target": {"repo_path": "/tmp/r"},
                "iterations": [
                    {
                        "number": 1,
                        "phase": "triage",
                        "result": {
                            "success": False,
                            "escalate": True,
                            "escalation_reason": "ambiguous issue",
                        },
                        "findings": {},
                    }
                ],
                "result": {"status": "escalated"},
                "metrics": {},
                "actions": [],
                "config": {},
            }
        }
        text = build_narrative(exec_data)
        assert "escalated" in text.lower()
        assert "ambiguous issue" in text

    def test_no_issue_url(self):
        text = build_narrative(
            _make_full_execution(source_url="", triage_findings={"classification": "bug"})
        )
        assert "repo" in text

    def test_no_repo(self):
        text = build_narrative(
            _make_full_execution(repo_path="", triage_findings={"classification": "bug"})
        )
        assert "issues/42" in text

    def test_empty_execution(self):
        text = build_narrative({})
        assert "bug-fix loop" in text
        assert "unknown" in text.lower()

    def test_no_triage_no_impl_no_review(self):
        exec_data = {
            "execution": {
                "id": "t2",
                "trigger": {},
                "target": {},
                "iterations": [],
                "result": {"status": "failure"},
                "metrics": {},
                "actions": [],
                "config": {},
            }
        }
        text = build_narrative(exec_data)
        assert "failure" in text.lower()

    def test_confidence_omitted_when_none(self):
        text = build_narrative(_make_full_execution(triage_findings={"classification": "bug"}))
        assert "bug" in text
        assert "confidence" not in text.lower()

    def test_repo_extracted_from_path(self):
        text = build_narrative(_make_full_execution(repo_path="/home/user/my-project"))
        assert "my-project" in text

    def test_repo_field_preferred_over_repo_path(self):
        exec_data = {
            "execution": {
                "id": "t3",
                "trigger": {"source_url": "https://github.com/o/r/issues/1"},
                "target": {"repo": "org/cool-repo", "repo_path": "/tmp/repo"},
                "iterations": [],
                "result": {"status": "success"},
                "metrics": {},
                "actions": [],
                "config": {},
            }
        }
        text = build_narrative(exec_data)
        assert "cool-repo" in text

    def test_flat_execution(self):
        flat = _make_full_execution(triage_findings={"classification": "bug"})["execution"]
        text = build_narrative(flat)
        assert "bug" in text


class TestNarrativeInSummaryMarkdown:
    def test_narrative_present_in_summary(self):
        md = build_summary_markdown(_make_execution())
        assert "processed" in md.lower() or "engine" in md.lower()
        assert "success" in md.lower()

    def test_narrative_before_status_line(self):
        md = build_summary_markdown(_make_execution())
        lines = md.split("\n")
        narrative_idx = None
        status_idx = None
        for i, line in enumerate(lines):
            if "engine" in line.lower() and "processed" in line.lower():
                narrative_idx = i
            if line.startswith("**Status**"):
                status_idx = i
        if narrative_idx is not None and status_idx is not None:
            assert narrative_idx < status_idx


class TestNarrativeInReportHtml:
    def test_narrative_appears_in_html(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        pub.publish(
            _make_full_execution(
                triage_findings={"classification": "bug", "confidence": 0.9},
                review_findings={"verdict": "approve"},
            )
        )
        html = (tmp_path / "report.html").read_text(encoding="utf-8")
        assert "bug" in html
        assert "approved" in html.lower()
        assert "success" in html.lower()

    def test_narrative_before_metrics_in_html(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        pub.publish(_make_full_execution(triage_findings={"classification": "bug"}))
        html = (tmp_path / "report.html").read_text(encoding="utf-8")
        narrative_pos = html.find("Narrative Summary")
        metrics_pos = html.find("Metrics Overview")
        assert narrative_pos != -1
        assert metrics_pos != -1
        assert narrative_pos < metrics_pos

    def test_empty_narrative_hidden(self, tmp_path):
        pub = ReportPublisher(output_dir=tmp_path)
        pub.publish({})
        html = (tmp_path / "report.html").read_text(encoding="utf-8")
        assert "bug-fix loop" in html


class TestNarrativeInReportData:
    def test_report_data_has_narrative(self):
        from engine.visualization.report_generator import extract_report_data

        data = extract_report_data(_make_full_execution(triage_findings={"classification": "bug"}))
        assert data.narrative != ""
        assert "bug" in data.narrative

    def test_report_data_to_dict_has_narrative(self):
        from engine.visualization.report_generator import extract_report_data

        data = extract_report_data(_make_execution())
        d = data.to_dict()
        assert "narrative" in d
        assert isinstance(d["narrative"], str)


# ---------------------------------------------------------------------------
# Loop integration tests
# ---------------------------------------------------------------------------


class TestLoopIntegration:
    """Verify that RalphLoop._publish_reports integrates with the publisher."""

    def test_loop_write_outputs_generates_reports(self, tmp_path):
        config = EngineConfig()
        config.loop.max_iterations = 1
        loop = RalphLoop(
            config=config,
            llm=MockProvider(),
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )
        loop._start_time = 0
        loop._write_outputs("success")

        reports_dir = tmp_path / "output" / "reports"
        assert reports_dir.exists()
        assert (reports_dir / "report.html").exists()
        assert (reports_dir / "summary.md").exists()
        assert (reports_dir / "artifact-manifest.json").exists()

    def test_loop_run_generates_reports(self, tmp_path):
        import asyncio

        config = EngineConfig()
        config.loop.max_iterations = 1
        loop = RalphLoop(
            config=config,
            llm=MockProvider(),
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )

        asyncio.run(loop.run())

        reports_dir = tmp_path / "output" / "reports"
        assert reports_dir.exists()
        assert (reports_dir / "report.html").exists()

    def test_publish_reports_failure_does_not_crash_loop(self, tmp_path):
        config = EngineConfig()
        loop = RalphLoop(
            config=config,
            llm=MockProvider(),
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )
        loop._start_time = 0

        with patch(
            "engine.visualization.publisher.ReportPublisher.publish",
            side_effect=RuntimeError("publish exploded"),
        ):
            loop._write_outputs("success")

        assert (tmp_path / "output" / "execution.json").exists()
        assert (tmp_path / "output" / "status.txt").exists()


# ---------------------------------------------------------------------------
# _format_finding_value / _summarise_dict tests (D11 — 7.11)
# ---------------------------------------------------------------------------


class TestFormatFindingValue:
    """Verify human-readable rendering of finding values in summary.md."""

    def test_none_renders_dash(self):
        assert _format_finding_value(None) == "—"

    def test_bool_true(self):
        assert _format_finding_value(True) == "yes"

    def test_bool_false(self):
        assert _format_finding_value(False) == "no"

    def test_int(self):
        assert _format_finding_value(42) == "42"

    def test_float(self):
        assert _format_finding_value(0.85) == "0.85"

    def test_simple_string(self):
        assert _format_finding_value("bug") == "bug"

    def test_empty_string(self):
        assert _format_finding_value("") == "—"

    def test_whitespace_only_string(self):
        assert _format_finding_value("   ") == "—"

    def test_long_string_truncated(self):
        long = "x" * 300
        result = _format_finding_value(long, max_len=100)
        assert len(result) == 101  # 100 chars + ellipsis
        assert result.endswith("…")

    def test_string_at_max_len(self):
        exact = "a" * 200
        assert _format_finding_value(exact) == exact

    def test_list_of_strings(self):
        result = _format_finding_value(["file1.py", "file2.go"])
        assert "file1.py" in result
        assert "file2.go" in result
        assert ", " in result

    def test_empty_list(self):
        assert _format_finding_value([]) == "none"

    def test_list_of_dicts(self):
        items = [
            {"file": "a.py", "severity": "high", "issue": "null deref"},
            {"file": "b.py", "severity": "low", "issue": "typo"},
        ]
        result = _format_finding_value(items)
        assert "a.py" in result
        assert "high" in result
        assert "b.py" in result

    def test_list_cap_at_ten(self):
        items = [{"id": i} for i in range(15)]
        result = _format_finding_value(items)
        assert "5 more" in result

    def test_list_of_strings_truncated(self):
        items = ["a" * 100 for _ in range(5)]
        result = _format_finding_value(items, max_len=50)
        assert result.endswith("…")

    def test_dict_renders_key_value(self):
        result = _format_finding_value({"classification": "bug", "confidence": 0.9})
        assert "classification: bug" in result
        assert "confidence: 0.9" in result

    def test_empty_dict(self):
        assert _format_finding_value({}) == "—"

    def test_dict_with_nested_dict(self):
        result = _format_finding_value({"root_cause": "unknown", "details": {"a": 1, "b": 2}})
        assert "root_cause: unknown" in result
        assert "(2 keys)" in result

    def test_dict_with_nested_list(self):
        result = _format_finding_value({"files": ["a.py", "b.py"], "count": 2})
        assert "(2 items)" in result
        assert "count: 2" in result

    def test_dict_with_none_value(self):
        result = _format_finding_value({"reason": None})
        assert "reason:" in result
        assert "—" in result

    def test_dict_with_bool_value(self):
        result = _format_finding_value({"injection_detected": False})
        assert "no" in result

    def test_long_dict_truncated(self):
        big = {f"key_{i}": f"value_{i}_" + "x" * 50 for i in range(20)}
        result = _format_finding_value(big, max_len=100)
        assert result.endswith("…")

    def test_custom_max_len(self):
        result = _format_finding_value("hello world", max_len=5)
        assert result == "hello…"


class TestSummariseDict:
    """Verify dict summarisation helper."""

    def test_simple_dict(self):
        result = _summarise_dict({"a": 1, "b": "two"})
        assert "a: 1" in result
        assert "b: two" in result

    def test_nested_list_count(self):
        result = _summarise_dict({"items": [1, 2, 3]})
        assert "(3 items)" in result

    def test_nested_dict_count(self):
        result = _summarise_dict({"sub": {"x": 1, "y": 2}})
        assert "(2 keys)" in result

    def test_truncation(self):
        d = {f"k{i}": "v" * 50 for i in range(10)}
        result = _summarise_dict(d, max_len=80)
        assert result.endswith("…")
        assert len(result) <= 81  # 80 + ellipsis char

    def test_bool_values(self):
        result = _summarise_dict({"flag": True, "off": False})
        assert "flag: yes" in result
        assert "off: no" in result

    def test_none_value(self):
        result = _summarise_dict({"x": None})
        assert "x: —" in result


class TestSummaryFindingsRendering:
    """Integration tests verifying that build_summary_markdown renders findings readably."""

    def _make_exec_with_findings(self, findings: dict) -> dict:
        return {
            "execution": {
                "id": "d11-test",
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:05:00+00:00",
                "trigger": {"source_url": "https://github.com/o/r/issues/1"},
                "target": {"repo_path": "/tmp/repo"},
                "config": {},
                "iterations": [
                    {
                        "number": 1,
                        "phase": "triage",
                        "duration_ms": 500,
                        "result": {"success": True},
                        "findings": findings,
                    }
                ],
                "result": {"status": "success"},
                "metrics": {},
                "actions": [],
            }
        }

    def test_string_finding_rendered_cleanly(self):
        md = build_summary_markdown(self._make_exec_with_findings({"classification": "bug"}))
        assert "**classification**: bug" in md
        assert "{'classification'" not in md

    def test_dict_finding_rendered_as_key_value(self):
        findings = {
            "impl_plan": {"root_cause": "null ptr", "approach": "add check"},
        }
        md = build_summary_markdown(self._make_exec_with_findings(findings))
        assert "root_cause: null ptr" in md
        assert "approach: add check" in md
        assert "{'root_cause'" not in md

    def test_list_finding_rendered_as_items(self):
        comps = ["pkg/api/handler.go", "pkg/util/helper.go"]
        md = build_summary_markdown(self._make_exec_with_findings({"affected_components": comps}))
        assert "pkg/api/handler.go" in md
        assert "pkg/util/helper.go" in md

    def test_bool_finding_readable(self):
        md = build_summary_markdown(self._make_exec_with_findings({"injection_detected": False}))
        assert "**injection_detected**: no" in md
        assert "False" not in md

    def test_none_finding_rendered_as_dash(self):
        md = build_summary_markdown(self._make_exec_with_findings({"reason": None}))
        assert "**reason**: —" in md

    def test_nested_dict_shows_key_count(self):
        md = build_summary_markdown(
            self._make_exec_with_findings({"details": {"a": {"x": 1}, "b": [1, 2]}})
        )
        assert "(2 keys)" in md or "(2 items)" in md
        assert "{'a'" not in md

    def test_no_raw_python_repr(self):
        """No findings should produce Python dict/list repr in the markdown."""
        findings = {
            "classification": "bug",
            "confidence": 0.85,
            "affected_components": ["a.py", "b.go"],
            "triage": {"reasoning": "stack trace present", "severity": "high"},
        }
        md = build_summary_markdown(self._make_exec_with_findings(findings))
        assert "{'classification'" not in md
        assert "{'reasoning'" not in md
        assert "['a.py'" not in md

    def test_empty_findings_no_crash(self):
        md = build_summary_markdown(self._make_exec_with_findings({}))
        assert "d11-test" in md


# ---------------------------------------------------------------------------
# Artifact completeness tests (D10 — 7.10)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ralph-loop.yml"

EXPECTED_ARTIFACT_PATHS = [
    "./output/execution.json",
    "./output/log.json",
    "./output/progress.md",
    "./output/reports/",
    "./output/transcripts/",
    "./output/status.txt",
]


class TestArtifactCompleteness:
    """Verify that the workflow uploads all expected output files (D10)."""

    def test_workflow_lists_all_artifact_paths(self):
        """The 'Upload execution artifacts' step must include every expected path."""
        raw = WORKFLOW_PATH.read_text(encoding="utf-8")
        wf = yaml.safe_load(raw)
        upload_steps = [
            step
            for step in wf["jobs"]["run-ralph-loop"]["steps"]
            if step.get("name", "").startswith("Upload execution artifacts")
        ]
        assert len(upload_steps) == 1, "Expected exactly one 'Upload execution artifacts' step"
        path_block = upload_steps[0]["with"]["path"]
        for expected in EXPECTED_ARTIFACT_PATHS:
            assert expected in path_block, f"Missing artifact path: {expected}"

    def test_loop_run_produces_log_json(self, tmp_path):
        """log.json must exist after a loop run (written by StructuredLogger.flush)."""
        import asyncio

        config = EngineConfig()
        config.loop.max_iterations = 1
        loop = RalphLoop(
            config=config,
            llm=MockProvider(),
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )
        asyncio.run(loop.run())
        assert (tmp_path / "output" / "log.json").exists()
        entries = json.loads((tmp_path / "output" / "log.json").read_text())
        assert isinstance(entries, list)
        assert len(entries) > 0

    def test_loop_run_produces_progress_md(self, tmp_path):
        """progress.md must exist after a loop run (written by logger.narrate)."""
        import asyncio

        config = EngineConfig()
        config.loop.max_iterations = 1
        loop = RalphLoop(
            config=config,
            llm=MockProvider(),
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )
        asyncio.run(loop.run())
        assert (tmp_path / "output" / "progress.md").exists()
        content = (tmp_path / "output" / "progress.md").read_text()
        assert "Ralph Loop" in content

    def test_loop_run_produces_all_core_outputs(self, tmp_path):
        """Every core output file must exist after a loop run."""
        import asyncio

        config = EngineConfig()
        config.loop.max_iterations = 1
        loop = RalphLoop(
            config=config,
            llm=MockProvider(),
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )
        asyncio.run(loop.run())

        output = tmp_path / "output"
        assert (output / "execution.json").exists()
        assert (output / "log.json").exists()
        assert (output / "progress.md").exists()
        assert (output / "status.txt").exists()
        assert (output / "reports").is_dir()
        assert (output / "reports" / "report.html").exists()
        assert (output / "reports" / "summary.md").exists()

    def test_workflow_artifact_retention(self):
        """Retention days in the workflow must match config default (30)."""
        raw = WORKFLOW_PATH.read_text(encoding="utf-8")
        wf = yaml.safe_load(raw)
        upload_steps = [
            step
            for step in wf["jobs"]["run-ralph-loop"]["steps"]
            if step.get("name", "").startswith("Upload execution artifacts")
        ]
        retention = upload_steps[0]["with"]["retention-days"]
        assert retention == 30
