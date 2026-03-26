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

    def publish(
        self,
        execution: dict[str, Any],
        transcript_calls: list[dict[str, Any]] | None = None,
    ) -> PublishResult:
        """Generate and write all reports for an execution record.

        Args:
            execution: Raw execution record dict (from execution.json or loop output).
            transcript_calls: Full LLM call records for the inference log.

        Returns:
            PublishResult with paths to all generated files.
        """
        result = PublishResult(report_dir=str(self._output_dir))
        self._output_dir.mkdir(parents=True, exist_ok=True)

        result = self._generate_html_report(execution, result, transcript_calls)
        result = self._generate_summary(execution, result)
        result = self._write_manifest(execution, result)

        return result

    def publish_from_file(self, execution_json_path: str | Path) -> PublishResult:
        """Publish reports from an execution.json file.

        Automatically loads ``transcript-calls.json`` from the sibling
        ``transcripts/`` directory if present.

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

        transcript_calls: list[dict[str, Any]] | None = None
        transcript_path = path.parent / "transcripts" / "transcript-calls.json"
        if transcript_path.exists():
            try:
                transcript_calls = json.loads(transcript_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

        return self.publish(raw, transcript_calls=transcript_calls)

    def _generate_html_report(
        self,
        execution: dict[str, Any],
        result: PublishResult,
        transcript_calls: list[dict[str, Any]] | None = None,
    ) -> PublishResult:
        """Generate main HTML report."""
        try:
            report_path = self._output_dir / "report.html"
            self._generator.generate(
                execution, output_path=report_path, transcript_calls=transcript_calls,
            )
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


def build_narrative(execution: dict[str, Any]) -> str:
    """Build a plain-English narrative summarising an execution.

    Deterministic and template-based - no LLM call.  Produces 2-5 sentences
    covering: what issue was processed, triage classification, implementation
    attempts, review outcome, and final status.
    """
    exec_data = execution.get("execution", execution)
    trigger = exec_data.get("trigger", {})
    target = exec_data.get("target", {})
    result = exec_data.get("result", {})
    iterations = exec_data.get("iterations", [])
    status = result.get("status", "unknown")

    # --- issue / repo identification ----------------------------------------
    source_url = trigger.get("source_url", "")
    repo = target.get("repo", "") or target.get("repo_path", "")
    if repo:
        repo = repo.rstrip("/").rsplit("/", 1)[-1]

    parts: list[str] = []
    if source_url and repo:
        parts.append(f"The engine processed an issue from {repo} ({source_url}).")
    elif source_url:
        parts.append(f"The engine processed issue {source_url}.")
    elif repo:
        parts.append(f"The engine ran against {repo}.")
    else:
        parts.append("The engine executed a bug-fix loop.")

    # --- triage summary ------------------------------------------------------
    triage_iters = [i for i in iterations if i.get("phase") == "triage"]
    if triage_iters:
        last_triage = triage_iters[-1]
        findings = last_triage.get("findings", {})
        classification = findings.get("classification", "")
        confidence = findings.get("confidence")
        tr = last_triage.get("result", {})

        if classification:
            conf_str = f" with {confidence:.2f} confidence" if confidence else ""
            parts.append(f"Triage classified it as {classification}{conf_str}.")
        elif tr.get("escalate"):
            esc = tr.get("escalation_reason", "unknown reason")
            parts.append(f"Triage escalated: {esc}.")

    # --- implementation summary ----------------------------------------------
    impl_iters = [i for i in iterations if i.get("phase") == "implement"]
    if impl_iters:
        n = len(impl_iters)
        word = "attempt" if n == 1 else "attempts"
        succeeded = any(i.get("result", {}).get("success") for i in impl_iters)
        if succeeded:
            parts.append(f"Implementation succeeded after {n} {word}.")
        else:
            parts.append(f"Implementation failed after {n} {word}.")

    # --- review summary ------------------------------------------------------
    review_iters = [i for i in iterations if i.get("phase") == "review"]
    if review_iters:
        last_review = review_iters[-1]
        rf = last_review.get("findings", {})
        verdict = rf.get("verdict", "")
        rr = last_review.get("result", {})
        if verdict == "approve" or rr.get("success"):
            parts.append("The review phase approved the fix.")
        elif verdict == "block":
            reason = rf.get("summary", "security or injection concern")
            parts.append(f"The review phase blocked the fix: {reason}.")
        elif verdict == "request_changes":
            parts.append("The review phase requested changes.")

    # --- final status --------------------------------------------------------
    status_phrases = {
        "success": "Final status: success.",
        "failure": "Final status: failure.",
        "escalated": "Final status: escalated to human.",
        "timeout": "Final status: timed out.",
    }
    parts.append(status_phrases.get(status, f"Final status: {status}."))

    return " ".join(parts)


def _format_finding_value(value: Any, *, max_len: int = 200) -> str:
    """Format a finding value as human-readable text for markdown summaries.

    Renders dicts as ``Key: value`` items, lists as comma-separated or
    sub-items, and scalars as plain strings.  Long values are truncated.
    """
    if value is None:
        return "—"

    if isinstance(value, bool):
        return "yes" if value else "no"

    if isinstance(value, (int, float)):
        return str(value)

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return "—"
        if len(value) > max_len:
            return value[:max_len] + "…"
        return value

    if isinstance(value, list):
        if not value:
            return "none"
        if all(isinstance(v, str) for v in value):
            joined = ", ".join(value)
            if len(joined) > max_len:
                return joined[:max_len] + "…"
            return joined
        parts: list[str] = []
        for item in value[:10]:
            if isinstance(item, dict):
                summary = _summarise_dict(item, max_len=max_len)
                parts.append(summary)
            else:
                parts.append(str(item)[:max_len])
        if len(value) > 10:
            parts.append(f"… and {len(value) - 10} more")
        return "; ".join(parts)

    if isinstance(value, dict):
        if not value:
            return "—"
        return _summarise_dict(value, max_len=max_len)

    text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text


def _summarise_dict(d: dict[str, Any], *, max_len: int = 200) -> str:
    """Summarise a dict as ``key1: val1, key2: val2`` with truncation."""
    parts: list[str] = []
    for k, v in d.items():
        if isinstance(v, (dict, list)):
            v_str = f"({len(v)} items)" if isinstance(v, list) else f"({len(v)} keys)"
        elif isinstance(v, bool):
            v_str = "yes" if v else "no"
        else:
            v_str = str(v).strip() if v is not None else "—"
        parts.append(f"{k}: {v_str}")
    joined = ", ".join(parts)
    if len(joined) > max_len:
        return joined[:max_len] + "…"
    return joined


def build_summary_markdown(
    execution: dict[str, Any],
    config: ReportingConfig | None = None,
) -> str:
    """Generate a summary.md string with key execution metrics."""
    config = config or ReportingConfig()
    report_data = extract_report_data(execution)

    narrative = build_narrative(execution)

    lines = [
        f"# Execution Summary: {report_data.execution_id[:12] or 'unknown'}",
        "",
        narrative,
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
            dur = it.get("duration_ms", 0)
            line = f"- **#{it['number']} {it['phase']}** — {status_icon} ({dur:.0f}ms)"
            if esc_reason:
                line += f"  \n  > Escalation: {esc_reason}"
            lines.append(line)

            findings = it.get("findings", {})
            if findings:
                for fk, fv in findings.items():
                    lines.append(f"  - **{fk}**: {_format_finding_value(fv)}")

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
