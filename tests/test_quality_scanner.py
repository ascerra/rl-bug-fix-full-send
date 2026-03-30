"""Tests for Background Quality Scanner (Phase 6.3).

Validates the scanner combines golden principles, extraction proposals,
and code metrics into unified scan reports with PR body generation.
Tests cover:
- ScanFinding, ScanReport, CodeMetrics dataclasses
- BackgroundQualityScanner: principles check, extraction scan, code metrics
- build_refactoring_pr_body and build_scan_summary formatting
- CLI entry point
- Integration with real engine codebase
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from engine.quality_scanner import (
    CRITICAL_PRINCIPLES,
    BackgroundQualityScanner,
    CodeMetrics,
    ScanFinding,
    ScanReport,
    _suggest_fix,
    build_refactoring_pr_body,
    build_scan_summary,
    main,
    parse_args,
)

# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestCodeMetrics:
    def test_defaults(self):
        m = CodeMetrics()
        assert m.total_files == 0
        assert m.total_lines == 0
        assert m.avg_file_lines == 0.0

    def test_to_dict(self):
        m = CodeMetrics(total_files=10, total_lines=500, test_files=3, avg_file_lines=50.0)
        d = m.to_dict()
        assert d["total_files"] == 10
        assert d["total_lines"] == 500
        assert d["test_files"] == 3
        assert d["avg_file_lines"] == 50.0

    def test_to_dict_rounds_avg(self):
        m = CodeMetrics(avg_file_lines=33.33333)
        assert m.to_dict()["avg_file_lines"] == 33.3


class TestScanFinding:
    def test_required_fields(self):
        f = ScanFinding(
            category="principles",
            severity="critical",
            file="engine/phases/foo.py",
            line=42,
            message="Missing logger call",
        )
        assert f.category == "principles"
        assert f.severity == "critical"
        assert f.file == "engine/phases/foo.py"
        assert f.line == 42

    def test_to_dict_minimal(self):
        f = ScanFinding(category="test", severity="info", file="a.py", line=1, message="msg")
        d = f.to_dict()
        assert d["category"] == "test"
        assert d["severity"] == "info"
        assert "code" not in d
        assert "suggestion" not in d

    def test_to_dict_with_code_and_suggestion(self):
        f = ScanFinding(
            category="principles",
            severity="critical",
            file="b.py",
            line=5,
            message="bad",
            code="GP001",
            suggestion="fix it",
        )
        d = f.to_dict()
        assert d["code"] == "GP001"
        assert d["suggestion"] == "fix it"


class TestScanReport:
    def test_empty_report(self):
        r = ScanReport()
        assert r.critical_count == 0
        assert r.warning_count == 0
        assert r.info_count == 0
        assert r.has_critical is False
        assert "PASS" in r.summary()

    def test_critical_finding_makes_has_critical_true(self):
        r = ScanReport(
            findings=[
                ScanFinding(
                    category="principles",
                    severity="critical",
                    file="a.py",
                    line=1,
                    message="bad",
                )
            ]
        )
        assert r.has_critical is True
        assert r.critical_count == 1
        assert "FAIL" in r.summary()

    def test_mixed_findings_counted(self):
        r = ScanReport(
            findings=[
                ScanFinding(category="p", severity="critical", file="a.py", line=1, message="c"),
                ScanFinding(category="p", severity="warning", file="b.py", line=2, message="w"),
                ScanFinding(category="p", severity="info", file="c.py", line=3, message="i"),
                ScanFinding(category="p", severity="info", file="d.py", line=4, message="i2"),
            ]
        )
        assert r.critical_count == 1
        assert r.warning_count == 1
        assert r.info_count == 2

    def test_summary_includes_extraction_proposals(self):
        r = ScanReport(extraction_proposals=[{"tool_name": "test"}])
        assert "1 extraction proposal" in r.summary()

    def test_to_dict(self):
        r = ScanReport(
            timestamp="2026-03-25T10:00:00Z",
            engine_path="engine",
        )
        d = r.to_dict()
        assert d["timestamp"] == "2026-03-25T10:00:00Z"
        assert d["engine_path"] == "engine"
        assert d["has_critical"] is False
        assert "summary" in d
        assert isinstance(d["findings"], list)
        assert isinstance(d["code_metrics"], dict)

    def test_to_dict_serializes_findings(self):
        r = ScanReport(
            findings=[
                ScanFinding(category="test", severity="info", file="x.py", line=1, message="ok")
            ]
        )
        d = r.to_dict()
        assert len(d["findings"]) == 1
        assert d["findings"][0]["file"] == "x.py"


# ---------------------------------------------------------------------------
# _suggest_fix helper
# ---------------------------------------------------------------------------


class TestSuggestFix:
    def test_known_code(self):
        assert "logger" in _suggest_fix("GP001").lower() or "log" in _suggest_fix("GP001").lower()
        assert "untrusted" in _suggest_fix("GP003").lower()
        sug5 = _suggest_fix("GP005")
        assert "PipelineEngine" in sug5
        assert "max_iterations" in sug5 or "time_budget" in sug5
        assert "provenance" in _suggest_fix("GP008").lower()
        assert "PipelineEngine" in _suggest_fix("GP009")

    def test_unknown_code_returns_generic(self):
        assert "SPEC.md" in _suggest_fix("GP999")


# ---------------------------------------------------------------------------
# BackgroundQualityScanner
# ---------------------------------------------------------------------------


def _make_engine_dir(tmp_path: Path) -> Path:
    """Create a minimal valid engine directory for scanning."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "__init__.py").write_text("")

    phases = engine / "phases"
    phases.mkdir()
    (phases / "__init__.py").write_text("")
    (phases / "base.py").write_text(
        textwrap.dedent("""\
        class Phase:
            pass
        """)
    )

    tools = engine / "tools"
    tools.mkdir()
    (tools / "__init__.py").write_text("")
    (tools / "executor.py").write_text(
        textwrap.dedent("""\
        class ToolExecutor:
            def execute(self, tool_name, params):
                self.tracer.record_action("tool", tool_name)
                return {}
        """)
    )

    (engine / "loop.py").write_text(
        textwrap.dedent("""\
        class PipelineEngine:
            async def run(self):
                if self.iteration >= self.config.loop.max_iterations:
                    return
                if self._elapsed() >= self.config.loop.time_budget:
                    return

            def _write_outputs(self):
                self._publish_reports()
        """)
    )

    return engine


def _make_violation_engine(tmp_path: Path) -> Path:
    """Create an engine dir with known golden principle violations."""
    engine = tmp_path / "engine"
    engine.mkdir()
    (engine / "__init__.py").write_text("")

    phases = engine / "phases"
    phases.mkdir()
    (phases / "__init__.py").write_text("")
    (phases / "base.py").write_text("class Phase:\n    pass\n")
    (phases / "bad_phase.py").write_text(
        textwrap.dedent("""\
        from engine.phases.base import Phase

        class BadPhase(Phase):
            def observe(self):
                pass

            def plan(self):
                self.llm.complete("prompt")

            def act(self):
                pass

            def validate(self):
                pass

            def reflect(self):
                pass
        """)
    )

    tools = engine / "tools"
    tools.mkdir()
    (tools / "__init__.py").write_text("")
    (tools / "executor.py").write_text("class ToolExecutor:\n    pass\n")

    (engine / "loop.py").write_text("class PipelineEngine:\n    pass\n")

    return engine


class TestScannerPrinciples:
    def test_clean_engine_no_violations(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        assert report.principles_result["passed"] is True
        principle_findings = [f for f in report.findings if f.category == "principles"]
        assert len(principle_findings) == 0

    def test_violation_engine_produces_findings(self, tmp_path):
        engine = _make_violation_engine(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        assert report.principles_result["passed"] is False
        assert report.principles_result["violations"] > 0
        principle_findings = [f for f in report.findings if f.category == "principles"]
        assert len(principle_findings) > 0

    def test_critical_violation_codes(self, tmp_path):
        engine = _make_violation_engine(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        critical = [f for f in report.findings if f.severity == "critical"]
        for f in critical:
            assert f.code in CRITICAL_PRINCIPLES

    def test_findings_have_suggestions(self, tmp_path):
        engine = _make_violation_engine(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        principle_findings = [f for f in report.findings if f.category == "principles"]
        for f in principle_findings:
            assert f.suggestion, f"Finding {f.code} missing suggestion"

    def test_nonexistent_engine_path(self, tmp_path):
        scanner = BackgroundQualityScanner(engine_path=tmp_path / "nope")
        report = scanner.scan()
        assert len(report.findings) == 1
        assert report.findings[0].severity == "critical"
        assert "not found" in report.findings[0].message

    def test_principles_result_structure(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        pr = report.principles_result
        assert "passed" in pr
        assert "checks_run" in pr
        assert "files_scanned" in pr
        assert "violations" in pr


class TestScannerExtraction:
    def _make_execution_dir(self, tmp_path: Path) -> Path:
        """Create a directory with execution records containing repeated LLM patterns."""
        exec_dir = tmp_path / "executions"
        exec_dir.mkdir()
        record = {
            "execution": {
                "actions": [
                    {
                        "action_type": "llm_query",
                        "phase": "triage",
                        "llm_context": {
                            "prompt_summary": "Check if file exists in repo",
                            "tokens_in": 500,
                            "tokens_out": 100,
                        },
                        "input": {"description": "Check if file exists in repo"},
                    },
                    {
                        "action_type": "llm_query",
                        "phase": "implement",
                        "llm_context": {
                            "prompt_summary": "Check if file exists in repo path",
                            "tokens_in": 500,
                            "tokens_out": 100,
                        },
                        "input": {"description": "Check if file exists in repo path"},
                    },
                    {
                        "action_type": "llm_query",
                        "phase": "validate",
                        "llm_context": {
                            "prompt_summary": "Check if file found in repo",
                            "tokens_in": 500,
                            "tokens_out": 100,
                        },
                        "input": {"description": "Check if file found in repo"},
                    },
                ]
            }
        }
        (exec_dir / "execution.json").write_text(json.dumps(record))
        return exec_dir

    def test_no_execution_dir_skips_extraction(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        assert len(report.extraction_proposals) == 0
        assert report.execution_records_scanned == 0

    def test_empty_execution_dir(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = tmp_path / "exec"
        exec_dir.mkdir()
        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=exec_dir)
        report = scanner.scan()
        assert report.execution_records_scanned == 0

    def test_extraction_proposals_found(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = self._make_execution_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=exec_dir)
        report = scanner.scan()
        assert report.execution_records_scanned == 1
        assert len(report.extraction_proposals) > 0

    def test_extraction_findings_are_info_severity(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = self._make_execution_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=exec_dir)
        report = scanner.scan()
        extraction_findings = [f for f in report.findings if f.category == "extraction"]
        for f in extraction_findings:
            assert f.severity == "info"

    def test_extraction_findings_have_suggestions(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = self._make_execution_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=exec_dir)
        report = scanner.scan()
        extraction_findings = [f for f in report.findings if f.category == "extraction"]
        for f in extraction_findings:
            assert f.suggestion

    def test_deduplicates_proposals_by_tool_name(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = tmp_path / "exec2"
        exec_dir.mkdir()
        record = {
            "execution": {
                "actions": [
                    {
                        "action_type": "llm_query",
                        "phase": "triage",
                        "llm_context": {
                            "prompt_summary": "Check if file exists in repo",
                            "tokens_in": 100,
                            "tokens_out": 50,
                        },
                        "input": {"description": "Check if file exists in repo"},
                    },
                    {
                        "action_type": "llm_query",
                        "phase": "triage",
                        "llm_context": {
                            "prompt_summary": "Check if file found in repo",
                            "tokens_in": 100,
                            "tokens_out": 50,
                        },
                        "input": {"description": "Check if file found in repo"},
                    },
                    {
                        "action_type": "llm_query",
                        "phase": "implement",
                        "llm_context": {
                            "prompt_summary": "Check if file present in path",
                            "tokens_in": 100,
                            "tokens_out": 50,
                        },
                        "input": {"description": "Check if file present in path"},
                    },
                ]
            }
        }
        (exec_dir / "exec1.json").write_text(json.dumps(record))
        (exec_dir / "exec2.json").write_text(json.dumps(record))

        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=exec_dir)
        report = scanner.scan()
        tool_names = [p["tool_name"] for p in report.extraction_proposals]
        assert len(tool_names) == len(set(tool_names)), "Proposals should be deduplicated"

    def test_invalid_json_files_skipped(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = tmp_path / "exec_bad"
        exec_dir.mkdir()
        (exec_dir / "bad.json").write_text("not json")
        (exec_dir / "also_bad.json").write_text('{"unrelated": true}')
        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=exec_dir)
        report = scanner.scan()
        assert report.execution_records_scanned == 0

    def test_nonexistent_execution_dir_skips(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=tmp_path / "nope")
        report = scanner.scan()
        assert report.execution_records_scanned == 0


class TestScannerCodeMetrics:
    def test_metrics_collected(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        m = report.code_metrics
        assert m.total_files > 0
        assert m.total_lines > 0

    def test_phase_files_counted(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        assert report.code_metrics.phase_files >= 2  # __init__.py + base.py

    def test_test_files_counted(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        tests = tmp_path / "tests"
        tests.mkdir()
        (tests / "test_something.py").write_text("def test_foo():\n    assert True\n")
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        assert report.code_metrics.test_files == 1
        assert report.code_metrics.test_lines == 2

    def test_avg_file_lines_computed(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        m = report.code_metrics
        assert m.avg_file_lines > 0
        assert m.avg_file_lines == pytest.approx(m.total_lines / m.total_files, abs=0.1)

    def test_nonexistent_engine_skips_metrics(self, tmp_path):
        scanner = BackgroundQualityScanner(engine_path=tmp_path / "nope")
        report = scanner.scan()
        assert report.code_metrics.total_files == 0

    def test_integration_files_counted(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        integrations = engine / "integrations"
        integrations.mkdir()
        (integrations / "__init__.py").write_text("")
        (integrations / "github.py").write_text("# github adapter\n")
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        assert report.code_metrics.integration_files == 2


# ---------------------------------------------------------------------------
# build_refactoring_pr_body
# ---------------------------------------------------------------------------


class TestBuildRefactoringPrBody:
    def test_empty_report(self):
        report = ScanReport(timestamp="2026-03-25T10:00:00Z")
        body = build_refactoring_pr_body(report)
        assert "Background Quality Scan Results" in body
        assert "PASS" in body
        assert "2026-03-25T10:00:00Z" in body

    def test_critical_violations_section(self):
        report = ScanReport(
            timestamp="2026-03-25T10:00:00Z",
            findings=[
                ScanFinding(
                    category="principles",
                    severity="critical",
                    file="engine/phases/bad.py",
                    line=10,
                    message="Missing logger",
                    code="GP001",
                    suggestion="Add logger call",
                )
            ],
        )
        body = build_refactoring_pr_body(report)
        assert "Critical Violations" in body
        assert "FAIL" in body
        assert "engine/phases/bad.py:10" in body
        assert "GP001" in body
        assert "Add logger call" in body

    def test_warning_section(self):
        report = ScanReport(
            findings=[
                ScanFinding(
                    category="principles",
                    severity="warning",
                    file="engine/loop.py",
                    line=5,
                    message="Minor issue",
                    code="GP010",
                )
            ],
        )
        body = build_refactoring_pr_body(report)
        assert "Warnings" in body
        assert "engine/loop.py:5" in body

    def test_extraction_proposals_section(self):
        report = ScanReport(
            extraction_proposals=[
                {
                    "tool_name": "check_file_exists",
                    "pattern": {
                        "occurrences": 5,
                        "estimated_tokens_saved": 3000,
                    },
                    "confidence": 0.95,
                }
            ],
        )
        body = build_refactoring_pr_body(report)
        assert "Tool Extraction Opportunities" in body
        assert "check_file_exists" in body
        assert "5 occurrences" in body
        assert "3,000 tokens" in body

    def test_principles_check_section(self):
        report = ScanReport(
            principles_result={
                "passed": True,
                "checks_run": 20,
                "files_scanned": 15,
                "violations": 0,
            }
        )
        body = build_refactoring_pr_body(report)
        assert "Principles Check" in body
        assert "Checks run: 20" in body
        assert "Files scanned: 15" in body

    def test_code_metrics_section(self):
        report = ScanReport(
            code_metrics=CodeMetrics(
                total_files=10,
                total_lines=500,
                test_files=5,
                test_lines=300,
                phase_files=3,
                integration_files=2,
                avg_file_lines=50.0,
            )
        )
        body = build_refactoring_pr_body(report)
        assert "Code Metrics" in body
        assert "Engine files: 10" in body
        assert "Test files: 5" in body

    def test_footer_present(self):
        report = ScanReport()
        body = build_refactoring_pr_body(report)
        assert "engine.quality_scanner" in body


# ---------------------------------------------------------------------------
# build_scan_summary
# ---------------------------------------------------------------------------


class TestBuildScanSummary:
    def test_empty_report(self):
        report = ScanReport()
        text = build_scan_summary(report)
        assert "PASS" in text
        assert "0 finding" in text

    def test_critical_findings_labeled(self):
        report = ScanReport(
            findings=[
                ScanFinding(
                    category="p",
                    severity="critical",
                    file="a.py",
                    line=1,
                    message="bad thing",
                    code="GP001",
                )
            ]
        )
        text = build_scan_summary(report)
        assert "[CRIT]" in text
        assert "[GP001]" in text
        assert "bad thing" in text

    def test_warning_findings_labeled(self):
        report = ScanReport(
            findings=[
                ScanFinding(category="p", severity="warning", file="b.py", line=2, message="warn")
            ]
        )
        text = build_scan_summary(report)
        assert "[WARN]" in text

    def test_info_findings_labeled(self):
        report = ScanReport(
            findings=[
                ScanFinding(category="e", severity="info", file="c.py", line=3, message="info msg")
            ]
        )
        text = build_scan_summary(report)
        assert "[INFO]" in text

    def test_extraction_proposals_mentioned(self):
        report = ScanReport(extraction_proposals=[{"tool_name": "test"}])
        text = build_scan_summary(report)
        assert "1 proposal" in text

    def test_no_code_finding_omits_bracket(self):
        report = ScanReport(
            findings=[
                ScanFinding(category="x", severity="info", file="d.py", line=1, message="no code")
            ]
        )
        text = build_scan_summary(report)
        lines_with_info = [line for line in text.splitlines() if "[INFO]" in line]
        assert len(lines_with_info) == 1
        assert "[]" not in lines_with_info[0]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_defaults(self):
        engine_path, exec_dir, output_path = parse_args([])
        assert engine_path == Path("engine")
        assert exec_dir is None
        assert output_path is None

    def test_custom_engine_path(self):
        engine_path, _, _ = parse_args(["my_engine"])
        assert engine_path == Path("my_engine")

    def test_execution_dir(self):
        _, exec_dir, _ = parse_args(["engine", "--execution-dir", "/tmp/exec"])
        assert exec_dir == Path("/tmp/exec")

    def test_output_path(self):
        _, _, output_path = parse_args(["engine", "--output", "report.json"])
        assert output_path == Path("report.json")

    def test_all_args(self):
        engine_path, exec_dir, output_path = parse_args(
            ["my_engine", "--execution-dir", "/d", "--output", "/o.json"]
        )
        assert engine_path == Path("my_engine")
        assert exec_dir == Path("/d")
        assert output_path == Path("/o.json")


class TestMainCLI:
    def test_nonexistent_engine_returns_1(self, tmp_path):
        result = main([str(tmp_path / "nope")])
        assert result == 1

    def test_clean_engine_returns_0(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        result = main([str(engine)])
        assert result == 0

    def test_violation_engine_returns_1(self, tmp_path):
        engine = _make_violation_engine(tmp_path)
        result = main([str(engine)])
        assert result == 1

    def test_output_file_written(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        output = tmp_path / "out" / "report.json"
        result = main([str(engine), "--output", str(output)])
        assert result == 0
        assert output.exists()
        data = json.loads(output.read_text())
        assert "timestamp" in data
        assert "summary" in data
        assert "code_metrics" in data

    def test_output_file_parent_created(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        output = tmp_path / "deep" / "nested" / "report.json"
        result = main([str(engine), "--output", str(output)])
        assert result == 0
        assert output.exists()

    def test_with_execution_dir(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = tmp_path / "exec"
        exec_dir.mkdir()
        record = {
            "execution": {
                "actions": [
                    {
                        "action_type": "llm_query",
                        "phase": "triage",
                        "llm_context": {
                            "prompt_summary": "Check file exists in repo",
                            "tokens_in": 500,
                            "tokens_out": 100,
                        },
                        "input": {"description": "Check file exists in repo"},
                    },
                    {
                        "action_type": "llm_query",
                        "phase": "implement",
                        "llm_context": {
                            "prompt_summary": "Check file found in repo",
                            "tokens_in": 500,
                            "tokens_out": 100,
                        },
                        "input": {"description": "Check file found in repo"},
                    },
                ]
            }
        }
        (exec_dir / "execution.json").write_text(json.dumps(record))
        output = tmp_path / "report.json"
        result = main([str(engine), "--execution-dir", str(exec_dir), "--output", str(output)])
        assert result == 0
        data = json.loads(output.read_text())
        assert data["execution_records_scanned"] == 1


# ---------------------------------------------------------------------------
# Integration with real engine
# ---------------------------------------------------------------------------


class TestRealEngineIntegration:
    """Run the scanner against the actual engine/ directory to verify it works
    on the real codebase and produces no critical violations."""

    def test_real_engine_no_critical_violations(self):
        engine_path = Path("engine")
        if not engine_path.is_dir():
            pytest.skip("engine/ directory not found (not running from project root)")
        scanner = BackgroundQualityScanner(engine_path=engine_path)
        report = scanner.scan()
        assert not report.has_critical, (
            f"Real engine has critical violations: "
            f"{[str(f.to_dict()) for f in report.findings if f.severity == 'critical']}"
        )

    def test_real_engine_principles_pass(self):
        engine_path = Path("engine")
        if not engine_path.is_dir():
            pytest.skip("engine/ directory not found")
        scanner = BackgroundQualityScanner(engine_path=engine_path)
        report = scanner.scan()
        assert report.principles_result.get("passed") is True

    def test_real_engine_metrics_nonzero(self):
        engine_path = Path("engine")
        if not engine_path.is_dir():
            pytest.skip("engine/ directory not found")
        scanner = BackgroundQualityScanner(engine_path=engine_path)
        report = scanner.scan()
        m = report.code_metrics
        assert m.total_files > 10
        assert m.total_lines > 500
        assert m.phase_files > 0
        assert m.integration_files > 0
        assert m.test_files > 0

    def test_real_engine_scan_report_serializable(self):
        engine_path = Path("engine")
        if not engine_path.is_dir():
            pytest.skip("engine/ directory not found")
        scanner = BackgroundQualityScanner(engine_path=engine_path)
        report = scanner.scan()
        serialized = json.dumps(report.to_dict(), default=str)
        assert isinstance(json.loads(serialized), dict)

    def test_real_engine_pr_body_generates(self):
        engine_path = Path("engine")
        if not engine_path.is_dir():
            pytest.skip("engine/ directory not found")
        scanner = BackgroundQualityScanner(engine_path=engine_path)
        report = scanner.scan()
        body = build_refactoring_pr_body(report)
        assert "Background Quality Scan Results" in body
        assert "Code Metrics" in body

    def test_make_quality_scan_target_exists(self):
        makefile = Path("Makefile")
        if not makefile.is_file():
            pytest.skip("Makefile not found")
        content = makefile.read_text()
        assert "quality-scan" in content


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_scan_report_with_no_timestamp(self):
        r = ScanReport()
        d = r.to_dict()
        assert d["timestamp"] == ""

    def test_scan_finding_line_zero(self):
        f = ScanFinding(category="x", severity="info", file="y.py", line=0, message="m")
        assert f.line == 0
        d = f.to_dict()
        assert d["line"] == 0

    def test_code_metrics_all_zeros(self):
        m = CodeMetrics()
        d = m.to_dict()
        assert all(v == 0 or v == 0.0 for v in d.values())

    def test_pr_body_with_zero_confidence_proposal(self):
        report = ScanReport(
            extraction_proposals=[
                {
                    "tool_name": "unknown_tool",
                    "pattern": {"occurrences": 1, "estimated_tokens_saved": 0},
                    "confidence": 0.0,
                }
            ]
        )
        body = build_refactoring_pr_body(report)
        assert "unknown_tool" in body
        assert "0%" in body

    def test_summary_no_extraction_details(self):
        report = ScanReport()
        text = build_scan_summary(report)
        assert "Tool extraction:" not in text

    def test_empty_engine_dir(self, tmp_path):
        engine = tmp_path / "engine"
        engine.mkdir()
        (engine / "__init__.py").write_text("")
        scanner = BackgroundQualityScanner(engine_path=engine)
        report = scanner.scan()
        assert report.code_metrics.total_files == 1

    def test_execution_record_with_flat_format(self, tmp_path):
        engine = _make_engine_dir(tmp_path)
        exec_dir = tmp_path / "exec"
        exec_dir.mkdir()
        record = {
            "actions": [
                {
                    "action_type": "llm_query",
                    "phase": "triage",
                    "llm_context": {
                        "prompt_summary": "run test suite on repo",
                        "tokens_in": 200,
                        "tokens_out": 50,
                    },
                    "input": {"description": "run test suite on repo"},
                },
                {
                    "action_type": "llm_query",
                    "phase": "validate",
                    "llm_context": {
                        "prompt_summary": "run test suite and check pass",
                        "tokens_in": 200,
                        "tokens_out": 50,
                    },
                    "input": {"description": "run test suite and check pass"},
                },
            ]
        }
        (exec_dir / "flat.json").write_text(json.dumps(record))
        scanner = BackgroundQualityScanner(engine_path=engine, execution_dir=exec_dir)
        report = scanner.scan()
        assert report.execution_records_scanned == 1

    def test_critical_principles_set_is_nonempty(self):
        assert len(CRITICAL_PRINCIPLES) > 0
        for code in CRITICAL_PRINCIPLES:
            assert code.startswith("GP")
