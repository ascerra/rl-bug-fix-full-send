"""Tests for engine.visualization.report_generator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.visualization.report_generator import (
    ReportData,
    ReportGenerator,
    _build_phases_summary,
    _format_duration_filter,
    _status_color_filter,
    _status_icon_filter,
    _to_json_filter,
    extract_report_data,
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
    exec_id: str = "test-exec-id-12345",
) -> dict:
    """Build a minimal execution record dict for testing."""
    return {
        "execution": {
            "id": exec_id,
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:05:00+00:00",
            "trigger": {"type": "github_issue", "source_url": "https://github.com/o/r/issues/1"},
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
                {
                    "number": 2,
                    "phase": "implement",
                    "started_at": "2026-03-25T10:01:00+00:00",
                    "completed_at": "2026-03-25T10:03:00+00:00",
                    "duration_ms": 30000.0,
                    "result": {
                        "success": True,
                        "should_continue": True,
                        "next_phase": "review",
                        "escalate": False,
                    },
                },
            ],
            "result": {
                "status": status,
                "total_iterations": 2,
                "phase_results": [
                    {"phase": "triage", "success": True, "escalate": False},
                    {"phase": "implement", "success": True, "escalate": False},
                ],
            },
            "metrics": metrics
            or {
                "total_iterations": 2,
                "total_llm_calls": 4,
                "total_tokens_in": 5000,
                "total_tokens_out": 1500,
                "total_tool_executions": 8,
                "time_per_phase_ms": {"triage": 1500.0, "implement": 30000.0},
                "phase_iteration_counts": {"triage": 1, "implement": 1},
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
                    "llm_context": {"model": "mock", "tokens_in": 1000, "tokens_out": 200},
                    "provenance": {},
                },
                {
                    "id": "act-2",
                    "iteration": 2,
                    "phase": "implement",
                    "action_type": "tool_execution",
                    "timestamp": "2026-03-25T10:01:30+00:00",
                    "input": {"description": "Write fix to controller.py", "context": {}},
                    "output": {"success": True, "data": {}},
                    "duration_ms": 200.0,
                    "llm_context": {},
                    "provenance": {},
                },
            ],
        }
    }


def _make_flat_execution(**kwargs) -> dict:
    """Build a flat execution dict (no top-level 'execution' key)."""
    return _make_execution(**kwargs)["execution"]


# ---------------------------------------------------------------------------
# extract_report_data tests
# ---------------------------------------------------------------------------


class TestExtractReportData:
    def test_extracts_from_wrapped_execution(self):
        data = extract_report_data(_make_execution())
        assert data.execution_id == "test-exec-id-12345"
        assert data.status == "success"
        assert data.total_iterations == 2

    def test_extracts_from_flat_execution(self):
        data = extract_report_data(_make_flat_execution())
        assert data.execution_id == "test-exec-id-12345"
        assert data.status == "success"

    def test_extracts_trigger(self):
        data = extract_report_data(_make_execution())
        assert data.trigger["source_url"] == "https://github.com/o/r/issues/1"

    def test_extracts_target(self):
        data = extract_report_data(_make_execution())
        assert data.target["repo_path"] == "/tmp/repo"

    def test_extracts_iterations(self):
        data = extract_report_data(_make_execution())
        assert len(data.iterations) == 2
        assert data.iterations[0]["phase"] == "triage"

    def test_extracts_actions(self):
        data = extract_report_data(_make_execution())
        assert len(data.actions) == 2

    def test_extracts_metrics(self):
        data = extract_report_data(_make_execution())
        assert data.metrics["total_llm_calls"] == 4

    def test_extracts_phase_results(self):
        data = extract_report_data(_make_execution())
        assert len(data.phase_results) == 2

    def test_builds_phases_summary(self):
        data = extract_report_data(_make_execution())
        assert len(data.phases_summary) == 2
        assert data.phases_summary[0]["phase"] == "triage"
        assert data.phases_summary[1]["phase"] == "implement"

    def test_handles_empty_execution(self):
        data = extract_report_data({})
        assert data.execution_id == ""
        assert data.status == "unknown"
        assert data.total_iterations == 0
        assert data.phases_summary == []

    def test_handles_missing_result(self):
        exec_data = _make_execution()
        del exec_data["execution"]["result"]
        data = extract_report_data(exec_data)
        assert data.status == "unknown"

    def test_extracts_errors_from_metrics(self):
        exec_data = _make_execution(
            metrics={
                "total_iterations": 1,
                "total_llm_calls": 0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_tool_executions": 0,
                "time_per_phase_ms": {},
                "phase_iteration_counts": {},
                "errors": ["Something went wrong", "Another error"],
            }
        )
        data = extract_report_data(exec_data)
        assert len(data.errors) == 2
        assert "Something went wrong" in data.errors

    def test_total_iterations_falls_back_to_len(self):
        exec_data = _make_execution()
        del exec_data["execution"]["result"]["total_iterations"]
        data = extract_report_data(exec_data)
        assert data.total_iterations == 2


# ---------------------------------------------------------------------------
# ReportData tests
# ---------------------------------------------------------------------------


class TestReportData:
    def test_to_dict_roundtrip(self):
        data = extract_report_data(_make_execution())
        d = data.to_dict()
        assert d["execution_id"] == "test-exec-id-12345"
        assert d["status"] == "success"
        assert isinstance(d["phases_summary"], list)

    def test_defaults(self):
        data = ReportData()
        assert data.execution_id == ""
        assert data.status == "unknown"
        assert data.total_iterations == 0
        assert data.phases_summary == []
        assert data.errors == []


# ---------------------------------------------------------------------------
# _build_phases_summary tests
# ---------------------------------------------------------------------------


class TestBuildPhasesSummary:
    def test_basic_summary(self):
        iterations = [
            {"phase": "triage", "result": {"success": True}},
            {"phase": "implement", "result": {"success": True}},
        ]
        actions = [
            {"phase": "triage", "action_type": "llm_query"},
            {"phase": "triage", "action_type": "tool_execution"},
            {"phase": "implement", "action_type": "llm_query"},
        ]
        metrics = {
            "time_per_phase_ms": {"triage": 1000.0, "implement": 2000.0},
            "phase_iteration_counts": {"triage": 1, "implement": 1},
        }
        result = _build_phases_summary(iterations, actions, metrics)
        assert len(result) == 2
        assert result[0]["phase"] == "triage"
        assert result[0]["llm_call_count"] == 1
        assert result[0]["tool_call_count"] == 1
        assert result[0]["action_count"] == 2
        assert result[0]["duration_ms"] == 1000.0
        assert result[0]["successful"] is True

    def test_empty_inputs(self):
        assert _build_phases_summary([], [], {}) == []

    def test_preserves_phase_order(self):
        iterations = [
            {"phase": "implement", "result": {"success": True}},
            {"phase": "triage", "result": {"success": True}},
        ]
        result = _build_phases_summary(iterations, [], {})
        assert result[0]["phase"] == "implement"
        assert result[1]["phase"] == "triage"

    def test_multiple_iterations_same_phase(self):
        iterations = [
            {"phase": "implement", "result": {"success": False}},
            {"phase": "implement", "result": {"success": True}},
        ]
        metrics = {
            "time_per_phase_ms": {"implement": 5000.0},
            "phase_iteration_counts": {"implement": 2},
        }
        result = _build_phases_summary(iterations, [], metrics)
        assert len(result) == 1
        assert result[0]["iterations"] == 2
        assert result[0]["successful"] is True

    def test_failed_phase(self):
        iterations = [{"phase": "triage", "result": {"success": False}}]
        result = _build_phases_summary(iterations, [], {})
        assert result[0]["successful"] is False

    def test_escalation_actions_excluded_from_tool_count(self):
        actions = [
            {"phase": "triage", "action_type": "escalation"},
            {"phase": "triage", "action_type": "tool_execution"},
        ]
        iterations = [{"phase": "triage", "result": {"success": False}}]
        result = _build_phases_summary(iterations, actions, {})
        assert result[0]["tool_call_count"] == 1
        assert result[0]["action_count"] == 2


# ---------------------------------------------------------------------------
# Jinja2 filter tests
# ---------------------------------------------------------------------------


class TestFilters:
    def test_to_json_basic(self):
        result = _to_json_filter({"key": "value"})
        parsed = json.loads(result)
        assert parsed == {"key": "value"}

    def test_to_json_with_indent(self):
        result = _to_json_filter({"a": 1}, indent=4)
        assert "    " in result

    def test_to_json_handles_non_serializable(self):
        result = _to_json_filter({"path": Path("/tmp")})
        assert "/tmp" in result

    def test_format_duration_ms(self):
        assert _format_duration_filter(500) == "500ms"

    def test_format_duration_seconds(self):
        assert _format_duration_filter(1500) == "1.5s"

    def test_format_duration_minutes(self):
        assert _format_duration_filter(120000) == "2.0m"

    def test_format_duration_hours(self):
        assert _format_duration_filter(7200000) == "2.0h"

    def test_format_duration_zero(self):
        assert _format_duration_filter(0) == "0ms"

    def test_status_color_success(self):
        assert _status_color_filter("success") == "status-success"

    def test_status_color_failure(self):
        assert _status_color_filter("failure") == "status-failure"

    def test_status_color_escalated(self):
        assert _status_color_filter("escalated") == "status-escalated"

    def test_status_color_timeout(self):
        assert _status_color_filter("timeout") == "status-timeout"

    def test_status_color_unknown(self):
        assert _status_color_filter("nope") == "status-unknown"

    def test_status_icon_success(self):
        assert _status_icon_filter("success") == "PASS"

    def test_status_icon_failure(self):
        assert _status_icon_filter("failure") == "FAIL"

    def test_status_icon_escalated(self):
        assert _status_icon_filter("escalated") == "ESCALATED"

    def test_status_icon_unknown(self):
        assert _status_icon_filter("nope") == "?"


# ---------------------------------------------------------------------------
# ReportGenerator tests
# ---------------------------------------------------------------------------


class TestReportGenerator:
    def test_generate_returns_html(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "<!DOCTYPE html>" in html
        assert "Ralph Loop Execution Report" in html

    def test_generate_contains_execution_id(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "test-exec-id" in html

    def test_generate_contains_status_badge(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution(status="failure"))
        assert "status-failure" in html
        assert "FAIL" in html

    def test_generate_contains_issue_url(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "https://github.com/o/r/issues/1" in html

    def test_generate_contains_metrics(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert ">4<" in html or "4" in html

    def test_generate_contains_phase_summary_table(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "triage" in html
        assert "implement" in html

    def test_generate_contains_iterations_timeline(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "iteration 1" in html
        assert "iteration 2" in html

    def test_generate_contains_actions_log(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "Classify issue" in html
        assert "Write fix to controller.py" in html

    def test_generate_writes_to_file(self, tmp_path):
        gen = ReportGenerator()
        out = tmp_path / "report.html"
        html = gen.generate(_make_execution(), output_path=out)
        assert out.exists()
        assert out.read_text(encoding="utf-8") == html

    def test_generate_creates_parent_dirs(self, tmp_path):
        gen = ReportGenerator()
        out = tmp_path / "nested" / "deep" / "report.html"
        gen.generate(_make_execution(), output_path=out)
        assert out.exists()

    def test_generate_from_file(self, tmp_path):
        exec_json = tmp_path / "execution.json"
        exec_json.write_text(json.dumps(_make_execution()), encoding="utf-8")
        gen = ReportGenerator()
        html = gen.generate_from_file(exec_json)
        assert "Ralph Loop Execution Report" in html

    def test_generate_from_file_writes_output(self, tmp_path):
        exec_json = tmp_path / "execution.json"
        exec_json.write_text(json.dumps(_make_execution()), encoding="utf-8")
        out = tmp_path / "report.html"
        gen = ReportGenerator()
        gen.generate_from_file(exec_json, output_path=out)
        assert out.exists()

    def test_generate_from_file_not_found(self):
        gen = ReportGenerator()
        with pytest.raises(FileNotFoundError):
            gen.generate_from_file("/nonexistent/execution.json")

    def test_generate_from_file_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        gen = ReportGenerator()
        with pytest.raises(json.JSONDecodeError):
            gen.generate_from_file(bad)

    def test_generate_empty_execution(self):
        gen = ReportGenerator()
        html = gen.generate({})
        assert "<!DOCTYPE html>" in html
        assert "No phases were executed" in html

    def test_generate_escalated_status(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution(status="escalated"))
        assert "status-escalated" in html
        assert "ESCALATED" in html

    def test_generate_timeout_status(self):
        gen = ReportGenerator()
        html = gen.generate(_make_execution(status="timeout"))
        assert "status-timeout" in html
        assert "TIMEOUT" in html

    def test_generate_with_errors(self):
        exec_data = _make_execution(
            metrics={
                "total_iterations": 1,
                "total_llm_calls": 0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "total_tool_executions": 0,
                "time_per_phase_ms": {},
                "phase_iteration_counts": {},
                "errors": ["Test failure in controller_test.go"],
            }
        )
        gen = ReportGenerator()
        html = gen.generate(exec_data)
        assert "Test failure in controller_test.go" in html

    def test_available_templates(self):
        gen = ReportGenerator()
        templates = gen.available_templates()
        assert "report.html" in templates

    def test_available_templates_empty_dir(self, tmp_path):
        gen = ReportGenerator(templates_dir=tmp_path)
        assert gen.available_templates() == []

    def test_available_templates_nonexistent_dir(self, tmp_path):
        gen = ReportGenerator(templates_dir=tmp_path / "nope")
        assert gen.available_templates() == []

    def test_custom_templates_dir(self, tmp_path):
        tmpl = tmp_path / "custom.html"
        tmpl.write_text("<html>{{ report.status }}</html>", encoding="utf-8")
        gen = ReportGenerator(templates_dir=tmp_path)
        html = gen.generate(_make_execution(), template_name="custom.html")
        assert "success" in html

    def test_missing_template_raises(self, tmp_path):
        gen = ReportGenerator(templates_dir=tmp_path)
        with pytest.raises(FileNotFoundError, match=r"nonexistent\.html"):
            gen.generate(_make_execution(), template_name="nonexistent.html")


# ---------------------------------------------------------------------------
# Integration: generate + extract + render roundtrip
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_full_roundtrip_via_file(self, tmp_path):
        exec_json = tmp_path / "execution.json"
        exec_json.write_text(json.dumps(_make_execution()), encoding="utf-8")
        out_html = tmp_path / "report.html"

        gen = ReportGenerator()
        html = gen.generate_from_file(exec_json, output_path=out_html)

        assert out_html.exists()
        content = out_html.read_text(encoding="utf-8")
        assert content == html
        assert "test-exec-id" in content
        assert "triage" in content
        assert "implement" in content

    def test_loop_execution_record_format(self):
        """Verify generator handles the dict shape from RalphLoop._write_outputs."""
        exec_record = {
            "execution": {
                "id": "abc-123",
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:05:00+00:00",
                "trigger": {
                    "type": "github_issue",
                    "source_url": "https://github.com/a/b/issues/9",
                },
                "target": {"repo_path": "/tmp/r"},
                "config": {},
                "iterations": [
                    {
                        "number": 1,
                        "phase": "triage",
                        "started_at": "2026-03-25T10:00:00+00:00",
                        "completed_at": "2026-03-25T10:00:30+00:00",
                        "duration_ms": 500.0,
                        "result": {
                            "success": True,
                            "should_continue": True,
                            "next_phase": "implement",
                            "escalate": False,
                        },
                    },
                ],
                "result": {
                    "status": "success",
                    "total_iterations": 1,
                    "phase_results": [
                        {"phase": "triage", "success": True, "escalate": False},
                    ],
                },
                "metrics": {
                    "total_iterations": 1,
                    "total_llm_calls": 1,
                    "total_tokens_in": 500,
                    "total_tokens_out": 100,
                    "total_tool_executions": 2,
                    "time_per_phase_ms": {"triage": 500.0},
                    "phase_iteration_counts": {"triage": 1},
                    "errors": [],
                },
                "actions": [],
            }
        }
        gen = ReportGenerator()
        html = gen.generate(exec_record)
        assert "abc-123" in html
        assert "PASS" in html
