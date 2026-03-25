"""Tests for engine.visualization.publisher."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from engine.config import EngineConfig, ReportingConfig
from engine.integrations.llm import MockProvider
from engine.loop import RalphLoop
from engine.visualization.publisher import (
    PublishResult,
    ReportPublisher,
    build_artifact_manifest,
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
