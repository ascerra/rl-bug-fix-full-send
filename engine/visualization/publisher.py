"""Report publisher — generates all reports and manages artifact output.

Coordinates report generation, summary creation, and artifact packaging
for GitHub Actions artifact upload and optional GitHub Pages deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.config import ReportingConfig
from engine.visualization.report_generator import ReportGenerator, extract_report_data


@dataclass
class PublishResult:
    """Result of report publishing."""

    report_dir: str = ""
    report_path: str = ""
    summary_path: str = ""
    manifest_path: str = ""
    files_generated: list[str] = field(default_factory=list)
    success: bool = True
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_dir": self.report_dir,
            "report_path": self.report_path,
            "summary_path": self.summary_path,
            "manifest_path": self.manifest_path,
            "files_generated": self.files_generated,
            "success": self.success,
            "errors": self.errors,
        }


class ReportPublisher:
    """Publishes execution reports to an output directory.

    Generates:
    - Main HTML report (``report.html``)
    - Summary markdown (``summary.md``) for GitHub Actions step summary
    - Artifact manifest (``artifact-manifest.json``) listing all generated files

    Respects reporting config flags (``decision_tree``, ``action_map``,
    ``comparison_mode``).
    """

    def __init__(
        self,
        output_dir: str | Path,
        config: ReportingConfig | None = None,
    ):
        self._output_dir = Path(output_dir)
        self._config = config or ReportingConfig()
        self._generator = ReportGenerator()

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    @property
    def config(self) -> ReportingConfig:
        return self._config

    def publish(self, execution: dict[str, Any]) -> PublishResult:
        """Generate and write all reports for an execution record.

        Args:
            execution: Raw execution record dict (from execution.json or loop output).

        Returns:
            PublishResult with paths to all generated files.
        """
        result = PublishResult(report_dir=str(self._output_dir))
        self._output_dir.mkdir(parents=True, exist_ok=True)

        result = self._generate_html_report(execution, result)
        result = self._generate_summary(execution, result)
        result = self._write_manifest(execution, result)

        return result

    def publish_from_file(self, execution_json_path: str | Path) -> PublishResult:
        """Publish reports from an execution.json file.

        Args:
            execution_json_path: Path to execution.json.

        Returns:
            PublishResult with paths to all generated files.

        Raises:
            FileNotFoundError: If execution_json_path does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        path = Path(execution_json_path)
        if not path.exists():
            raise FileNotFoundError(f"Execution file not found: {path}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return self.publish(raw)

    def _generate_html_report(
        self, execution: dict[str, Any], result: PublishResult
    ) -> PublishResult:
        """Generate main HTML report."""
        try:
            report_path = self._output_dir / "report.html"
            self._generator.generate(execution, output_path=report_path)
            result.report_path = str(report_path)
            result.files_generated.append(str(report_path))
        except Exception as exc:
            result.errors.append(f"Failed to generate report.html: {exc}")
            result.success = False
        return result

    def _generate_summary(self, execution: dict[str, Any], result: PublishResult) -> PublishResult:
        """Generate summary.md with key execution metrics."""
        try:
            summary_path = self._output_dir / "summary.md"
            summary_md = build_summary_markdown(execution, self._config)
            summary_path.write_text(summary_md, encoding="utf-8")
            result.summary_path = str(summary_path)
            result.files_generated.append(str(summary_path))
        except Exception as exc:
            result.errors.append(f"Failed to generate summary.md: {exc}")
        return result

    def _write_manifest(self, execution: dict[str, Any], result: PublishResult) -> PublishResult:
        """Write artifact manifest JSON."""
        try:
            manifest_path = self._output_dir / "artifact-manifest.json"
            manifest = build_artifact_manifest(execution, result.files_generated, self._config)
            manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
            result.manifest_path = str(manifest_path)
            result.files_generated.append(str(manifest_path))
        except Exception as exc:
            result.errors.append(f"Failed to write manifest: {exc}")
        return result


def build_summary_markdown(
    execution: dict[str, Any],
    config: ReportingConfig | None = None,
) -> str:
    """Generate a summary.md string with key execution metrics."""
    config = config or ReportingConfig()
    report_data = extract_report_data(execution)

    lines = [
        f"# Execution Summary: {report_data.execution_id[:12] or 'unknown'}",
        "",
        f"**Status**: `{report_data.status}`",
        f"**Iterations**: {report_data.total_iterations}",
    ]

    if report_data.trigger.get("source_url"):
        lines.append(f"**Issue**: {report_data.trigger['source_url']}")

    if report_data.started_at:
        lines.append(f"**Started**: {report_data.started_at}")
    if report_data.completed_at:
        lines.append(f"**Completed**: {report_data.completed_at}")

    if report_data.metrics:
        lines.extend(
            [
                "",
                "## Metrics",
                "",
                f"- LLM calls: {report_data.metrics.get('total_llm_calls', 0)}",
                f"- Tokens in: {report_data.metrics.get('total_tokens_in', 0):,}",
                f"- Tokens out: {report_data.metrics.get('total_tokens_out', 0):,}",
                f"- Tool executions: {report_data.metrics.get('total_tool_executions', 0)}",
            ]
        )

    if report_data.phases_summary:
        lines.extend(["", "## Phases", ""])
        lines.append("| Phase | Iterations | Status | Duration |")
        lines.append("|-------|------------|--------|----------|")
        for ps in report_data.phases_summary:
            status_mark = "PASS" if ps["successful"] else "FAIL"
            duration = f"{ps['duration_s']}s" if ps.get("duration_s") else "---"
            lines.append(f"| {ps['phase']} | {ps['iterations']} | {status_mark} | {duration} |")

    # Per-iteration trace
    exec_data = execution.get("execution", execution)
    iterations = exec_data.get("iterations", [])
    if iterations:
        lines.extend(["", "## Iteration Trace", ""])
        for it in iterations:
            res = it.get("result", {})
            status_icon = "PASS" if res.get("success") else "FAIL"
            esc_reason = res.get("escalation_reason", "")
            line = f"- **#{it['number']} {it['phase']}** — {status_icon} ({it.get('duration_ms', 0):.0f}ms)"
            if esc_reason:
                line += f"  \n  > Escalation: {esc_reason}"
            lines.append(line)

            findings = it.get("findings", {})
            if findings:
                for fk, fv in findings.items():
                    val = str(fv)[:200] + "..." if len(str(fv)) > 200 else str(fv)
                    lines.append(f"  - `{fk}`: {val}")

    if report_data.errors:
        lines.extend(["", "## Errors", ""])
        for err in report_data.errors:
            lines.append(f"- {err}")

    lines.extend(
        [
            "",
            "## Generated Reports",
            "",
            "- `report.html` --- Interactive HTML report with decision tree, action map",
        ]
    )
    if config.comparison_mode:
        lines.append("- Comparison report included (agent fix vs human fix)")

    lines.append("")
    return "\n".join(lines)


def build_artifact_manifest(
    execution: dict[str, Any],
    files: list[str],
    config: ReportingConfig | None = None,
) -> dict[str, Any]:
    """Build an artifact manifest dict."""
    config = config or ReportingConfig()
    exec_data = execution.get("execution", execution)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_id": exec_data.get("id", ""),
        "status": exec_data.get("result", {}).get("status", "unknown"),
        "config": {
            "decision_tree": config.decision_tree,
            "action_map": config.action_map,
            "comparison_mode": config.comparison_mode,
            "publish_to_pages": config.publish_to_pages,
            "artifact_retention_days": config.artifact_retention_days,
        },
        "files": files,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="report-publisher",
        description="Publish Ralph Loop execution reports",
    )
    parser.add_argument("--execution-log", required=True, help="Path to execution.json")
    parser.add_argument("--output-dir", required=True, help="Directory to write reports to")
    parser.add_argument("--comparison-mode", action="store_true", help="Enable comparison report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    config = ReportingConfig(comparison_mode=args.comparison_mode)
    publisher = ReportPublisher(output_dir=args.output_dir, config=config)

    try:
        result = publisher.publish_from_file(args.execution_log)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Error: invalid JSON in execution log: {exc}", file=sys.stderr)
        return 1

    if result.success:
        print(f"Reports published to {result.report_dir}")
        for f in result.files_generated:
            print(f"  - {f}")
    else:
        print("Report publishing completed with errors:", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err}", file=sys.stderr)

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
