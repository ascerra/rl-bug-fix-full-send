"""Background quality scanner — periodic scans for principle violations and quality issues.

Combines golden principles enforcement (Phase 6.1) and deterministic tool extraction
(Phase 6.2) into a unified scanner that produces structured scan reports and can
generate refactoring PR descriptions.

Run via: python -m engine.quality_scanner [engine_path] [--execution-dir DIR] [--output FILE]
Exit code 0 = no critical violations; exit code 1 = critical violations found.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.golden_principles import GoldenPrinciplesChecker
from engine.tools.extraction import ExtractionProposal, detect_and_propose

CRITICAL_PRINCIPLES = {"GP001", "GP003", "GP005", "GP008"}


@dataclass
class CodeMetrics:
    """Quantitative metrics about the engine codebase."""

    total_files: int = 0
    total_lines: int = 0
    test_files: int = 0
    test_lines: int = 0
    phase_files: int = 0
    integration_files: int = 0
    avg_file_lines: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_files": self.total_files,
            "total_lines": self.total_lines,
            "test_files": self.test_files,
            "test_lines": self.test_lines,
            "phase_files": self.phase_files,
            "integration_files": self.integration_files,
            "avg_file_lines": round(self.avg_file_lines, 1),
        }


@dataclass
class ScanFinding:
    """A single quality finding from the scanner."""

    category: str
    severity: str
    file: str
    line: int
    message: str
    code: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "category": self.category,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }
        if self.code:
            result["code"] = self.code
        if self.suggestion:
            result["suggestion"] = self.suggestion
        return result


@dataclass
class ScanReport:
    """Aggregated result of a background quality scan."""

    timestamp: str = ""
    engine_path: str = ""
    findings: list[ScanFinding] = field(default_factory=list)
    principles_result: dict[str, Any] = field(default_factory=dict)
    extraction_proposals: list[dict[str, Any]] = field(default_factory=list)
    code_metrics: CodeMetrics = field(default_factory=CodeMetrics)
    execution_records_scanned: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "warning")

    @property
    def info_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "info")

    @property
    def has_critical(self) -> bool:
        return self.critical_count > 0

    def summary(self) -> str:
        status = "FAIL" if self.has_critical else "PASS"
        return (
            f"Quality Scan: {status} — "
            f"{len(self.findings)} finding(s) "
            f"({self.critical_count} critical, "
            f"{self.warning_count} warning, "
            f"{self.info_count} info), "
            f"{len(self.extraction_proposals)} extraction proposal(s)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "engine_path": self.engine_path,
            "summary": self.summary(),
            "has_critical": self.has_critical,
            "findings": [f.to_dict() for f in self.findings],
            "principles_result": self.principles_result,
            "extraction_proposals": self.extraction_proposals,
            "code_metrics": self.code_metrics.to_dict(),
            "execution_records_scanned": self.execution_records_scanned,
        }


class BackgroundQualityScanner:
    """Scans the engine codebase for quality issues and proposes improvements.

    Combines:
    - Golden principles enforcement (AST-based static analysis)
    - Deterministic tool extraction proposals (from execution records)
    - Code metrics collection (file counts, line counts, structure checks)
    """

    def __init__(
        self,
        engine_path: str | Path,
        execution_dir: str | Path | None = None,
    ):
        self.engine_path = Path(engine_path)
        self.execution_dir = Path(execution_dir) if execution_dir else None

    def scan(self) -> ScanReport:
        """Run a full background quality scan and return a structured report."""
        report = ScanReport(
            timestamp=datetime.now(UTC).isoformat(),
            engine_path=str(self.engine_path),
        )

        report = self._run_principles_check(report)
        report = self._run_extraction_scan(report)
        report = self._collect_code_metrics(report)

        return report

    def _run_principles_check(self, report: ScanReport) -> ScanReport:
        """Run golden principles checker and convert violations to findings."""
        if not self.engine_path.is_dir():
            report.findings.append(
                ScanFinding(
                    category="principles",
                    severity="critical",
                    file=str(self.engine_path),
                    line=0,
                    message=f"Engine path not found: {self.engine_path}",
                )
            )
            return report

        checker = GoldenPrinciplesChecker(self.engine_path)
        result = checker.check_all()

        report.principles_result = {
            "passed": result.passed,
            "checks_run": result.checks_run,
            "files_scanned": result.files_scanned,
            "violations": len(result.violations),
        }

        for violation in result.violations:
            severity = "critical" if violation.code in CRITICAL_PRINCIPLES else "warning"
            report.findings.append(
                ScanFinding(
                    category="principles",
                    severity=severity,
                    file=violation.file,
                    line=violation.line,
                    message=violation.message,
                    code=violation.code,
                    suggestion=_suggest_fix(violation.code),
                )
            )

        return report

    def _run_extraction_scan(self, report: ScanReport) -> ScanReport:
        """Scan execution records for deterministic tool extraction opportunities."""
        if self.execution_dir is None or not self.execution_dir.is_dir():
            return report

        records = self._load_execution_records()
        report.execution_records_scanned = len(records)

        if not records:
            return report

        all_proposals: list[ExtractionProposal] = []
        for record in records:
            proposals = detect_and_propose(record)
            all_proposals.extend(proposals)

        seen_tools: set[str] = set()
        for proposal in all_proposals:
            if proposal.tool_name in seen_tools:
                continue
            seen_tools.add(proposal.tool_name)
            report.extraction_proposals.append(proposal.to_dict())
            report.findings.append(
                ScanFinding(
                    category="extraction",
                    severity="info",
                    file="execution records",
                    line=0,
                    message=(
                        f"LLM pattern '{proposal.tool_name}' repeated "
                        f"{proposal.pattern.occurrences} time(s) — "
                        f"could save ~{proposal.pattern.estimated_tokens_saved:,} tokens"
                    ),
                    suggestion=(
                        f"Extract into deterministic tool: {proposal.tool_name} "
                        f"(confidence: {proposal.confidence:.0%})"
                    ),
                )
            )

        return report

    def _collect_code_metrics(self, report: ScanReport) -> ScanReport:
        """Collect quantitative metrics about the engine codebase."""
        if not self.engine_path.is_dir():
            return report

        metrics = CodeMetrics()
        test_dir = self.engine_path.parent / "tests"

        for py_file in sorted(self.engine_path.rglob("*.py")):
            if py_file.name == "__pycache__":
                continue
            try:
                line_count = len(py_file.read_text().splitlines())
            except OSError:
                continue
            metrics.total_files += 1
            metrics.total_lines += line_count

            rel = py_file.relative_to(self.engine_path)
            parts = rel.parts
            if len(parts) > 0 and parts[0] == "phases":
                metrics.phase_files += 1
            if len(parts) > 0 and parts[0] == "integrations":
                metrics.integration_files += 1

        if test_dir.is_dir():
            for py_file in sorted(test_dir.rglob("*.py")):
                try:
                    line_count = len(py_file.read_text().splitlines())
                except OSError:
                    continue
                metrics.test_files += 1
                metrics.test_lines += line_count

        if metrics.total_files > 0:
            metrics.avg_file_lines = metrics.total_lines / metrics.total_files

        report.code_metrics = metrics
        return report

    def _load_execution_records(self) -> list[dict[str, Any]]:
        """Load execution.json files from the execution directory."""
        if self.execution_dir is None:
            return []

        records: list[dict[str, Any]] = []
        for json_file in sorted(self.execution_dir.glob("*.json")):
            try:
                data = json.loads(json_file.read_text())
                if isinstance(data, dict) and (
                    "execution" in data or "actions" in data or "iterations" in data
                ):
                    records.append(data)
            except (json.JSONDecodeError, OSError):
                continue
        return records


def _suggest_fix(code: str) -> str:
    """Return a human-readable fix suggestion for a given violation code."""
    suggestions = {
        "GP001": (
            "Add self.logger.info/debug/warning call to the phase method "
            "or ensure ToolExecutor traces actions."
        ),
        "GP003": "Wrap untrusted content with _wrap_untrusted_content() before passing to LLM.",
        "GP005": "Ensure RalphLoop.run() checks both max_iterations and time_budget.",
        "GP008": (
            "Pair every self.llm.complete() call with self.tracer.record_llm_call() for provenance."
        ),
        "GP009": "Wire report generation into RalphLoop._write_outputs().",
        "GP010": "Reference self.config in phase implementations instead of hardcoding values.",
    }
    return suggestions.get(code, "Review the golden principles in SPEC.md §7.")


def build_refactoring_pr_body(report: ScanReport) -> str:
    """Generate a PR description for a refactoring PR based on scan findings."""
    lines = [
        "## Background Quality Scan Results",
        "",
        f"**Scan timestamp**: {report.timestamp}",
        f"**Status**: {'FAIL — critical violations found' if report.has_critical else 'PASS'}",
        f"**Findings**: {len(report.findings)} "
        f"({report.critical_count} critical, {report.warning_count} warning, "
        f"{report.info_count} info)",
        "",
    ]

    critical_findings = [f for f in report.findings if f.severity == "critical"]
    if critical_findings:
        lines.extend(["### Critical Violations", ""])
        for f in critical_findings:
            lines.append(f"- **{f.file}:{f.line}** [{f.code}] {f.message}")
            if f.suggestion:
                lines.append(f"  - Suggestion: {f.suggestion}")
        lines.append("")

    warning_findings = [f for f in report.findings if f.severity == "warning"]
    if warning_findings:
        lines.extend(["### Warnings", ""])
        for f in warning_findings:
            lines.append(f"- **{f.file}:{f.line}** [{f.code}] {f.message}")
            if f.suggestion:
                lines.append(f"  - Suggestion: {f.suggestion}")
        lines.append("")

    if report.extraction_proposals:
        lines.extend(
            [
                "### Tool Extraction Opportunities",
                "",
                f"Found {len(report.extraction_proposals)} pattern(s) that could be replaced "
                "with deterministic tools:",
                "",
            ]
        )
        for p in report.extraction_proposals:
            pattern = p.get("pattern", {})
            lines.append(
                f"- **{p.get('tool_name', 'unknown')}** — "
                f"{pattern.get('occurrences', 0)} occurrences, "
                f"~{pattern.get('estimated_tokens_saved', 0):,} tokens saved, "
                f"confidence {p.get('confidence', 0):.0%}"
            )
        lines.append("")

    if report.principles_result:
        lines.extend(
            [
                "### Principles Check",
                "",
                f"- Checks run: {report.principles_result.get('checks_run', 0)}",
                f"- Files scanned: {report.principles_result.get('files_scanned', 0)}",
                f"- Violations: {report.principles_result.get('violations', 0)}",
                "",
            ]
        )

    metrics = report.code_metrics.to_dict()
    lines.extend(
        [
            "### Code Metrics",
            "",
            f"- Engine files: {metrics['total_files']} ({metrics['total_lines']:,} lines)",
            f"- Test files: {metrics['test_files']} ({metrics['test_lines']:,} lines)",
            f"- Phase files: {metrics['phase_files']}",
            f"- Integration files: {metrics['integration_files']}",
            f"- Avg lines/file: {metrics['avg_file_lines']}",
            "",
        ]
    )

    lines.extend(
        [
            "---",
            "",
            "*This PR was generated by `engine.quality_scanner`. "
            "Review findings and apply fixes as appropriate.*",
        ]
    )

    return "\n".join(lines)


def build_scan_summary(report: ScanReport) -> str:
    """Generate a concise text summary suitable for CI output or notifications."""
    lines = [
        report.summary(),
        "",
    ]

    if report.findings:
        for f in report.findings:
            prefix = {"critical": "CRIT", "warning": "WARN", "info": "INFO"}.get(f.severity, "????")
            code_str = f" [{f.code}]" if f.code else ""
            lines.append(f"  [{prefix}]{code_str} {f.file}:{f.line}: {f.message}")

    if report.extraction_proposals:
        lines.extend(
            [
                "",
                f"Tool extraction: {len(report.extraction_proposals)} proposal(s)",
            ]
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> tuple[Path, Path | None, Path | None]:
    """Parse CLI arguments. Returns (engine_path, execution_dir, output_path)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="quality-scanner",
        description="Background quality scanner for the Ralph Loop engine",
    )
    parser.add_argument(
        "engine_path",
        nargs="?",
        default="engine",
        help="Path to the engine package (default: engine)",
    )
    parser.add_argument(
        "--execution-dir",
        default=None,
        help="Directory containing execution.json files to scan for extraction patterns",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write the scan report JSON (default: stdout summary only)",
    )
    args = parser.parse_args(argv)
    return (
        Path(args.engine_path),
        Path(args.execution_dir) if args.execution_dir else None,
        Path(args.output) if args.output else None,
    )


def main(argv: list[str] | None = None) -> int:
    """Run background quality scan. Returns 0 if no critical violations, 1 otherwise."""
    engine_path, execution_dir, output_path = parse_args(argv)

    if not engine_path.is_dir():
        print(f"Error: engine path not found: {engine_path}", file=sys.stderr)
        return 1

    scanner = BackgroundQualityScanner(
        engine_path=engine_path,
        execution_dir=execution_dir,
    )
    report = scanner.scan()

    print(build_scan_summary(report))

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report.to_dict(), indent=2, default=str))
        print(f"\nFull report written to {output_path}")

    return 1 if report.has_critical else 0


if __name__ == "__main__":
    sys.exit(main())
