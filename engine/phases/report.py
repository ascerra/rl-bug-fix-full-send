"""Report Phase — generate visual evidence of the execution.

Implements SPEC §5.5:
1. Generate decision tree visualization (HTML/SVG)
2. Generate action map visualization (HTML/SVG)
3. If comparison mode: generate side-by-side diff with analysis
4. Package all artifacts (logs, transcripts, visualizations)

The actual report generation is delegated to ``ReportPublisher``.  This
phase wraps the publisher in the standard OODA cycle so that reporting
shows up in the execution trace, gets narrated to the live progress log,
and respects the phase tool restrictions.

Report failures never block the loop — ``reflect()`` always returns
``success=True`` so downstream outputs (execution.json, status.txt) are
still written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from engine.phases.base import REPORT_TOOLS, Phase, PhaseResult


class ReportPhase(Phase):
    """Generate and publish visual reports from the execution record.

    Receives execution data via ``issue_data["_execution_snapshot"]`` and
    writes reports to ``issue_data["_output_dir"] / "reports"``.  If the
    snapshot or output dir is missing the phase logs a warning and returns
    success with an explanation (report generation is non-blocking).
    """

    name = "report"
    allowed_tools: ClassVar[list[str]] = REPORT_TOOLS

    async def observe(self) -> dict[str, Any]:
        """Gather reporting context: config flags, data availability, output path."""
        self.logger.narrate("Checking reporting configuration and available data.")
        cfg = self.config.reporting
        snapshot = self.issue_data.get("_execution_snapshot")
        output_dir = self.issue_data.get("_output_dir", "")

        return {
            "decision_tree_enabled": cfg.decision_tree,
            "action_map_enabled": cfg.action_map,
            "comparison_mode": cfg.comparison_mode,
            "publish_to_pages": cfg.publish_to_pages,
            "has_execution_data": snapshot is not None,
            "output_dir": output_dir,
        }

    async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Determine which reports to generate."""
        reports: list[str] = []
        if observation["decision_tree_enabled"]:
            reports.append("decision_tree")
        if observation["action_map_enabled"]:
            reports.append("action_map")
        if observation["comparison_mode"]:
            reports.append("comparison")

        self.logger.narrate(f"Planning report generation: {', '.join(reports) or 'none'}.")
        return {
            "reports_to_generate": reports,
            "execution_available": observation["has_execution_data"],
            "output_dir": observation["output_dir"],
        }

    async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Generate reports via ReportPublisher."""
        if not plan["execution_available"]:
            self.logger.narrate("No execution snapshot available — skipping report generation.")
            return {
                "published": False,
                "reason": "No execution snapshot provided to report phase",
                "files_generated": [],
            }

        snapshot = self.issue_data["_execution_snapshot"]
        output_dir = plan.get("output_dir", "")
        if not output_dir:
            self.logger.narrate("No output directory configured — skipping report generation.")
            return {
                "published": False,
                "reason": "No output directory provided",
                "files_generated": [],
            }

        reports_dir = Path(output_dir) / "reports"

        try:
            from engine.visualization.publisher import ReportPublisher

            publisher = ReportPublisher(
                output_dir=reports_dir,
                config=self.config.reporting,
            )
            result = publisher.publish(snapshot)
        except ImportError:
            self.logger.narrate("Visualization module not available — skipping.")
            return {
                "published": False,
                "reason": "Visualization module not importable",
                "files_generated": [],
            }
        except Exception as exc:
            self.logger.narrate(f"Report generation failed: {exc}")
            return {
                "published": False,
                "reason": f"Publisher error: {exc}",
                "files_generated": [],
                "errors": [str(exc)],
            }

        if result.success:
            self.logger.narrate(
                f"Published {len(result.files_generated)} report files to {reports_dir}."
            )
        else:
            self.logger.narrate(
                f"Report publishing completed with errors: {', '.join(result.errors)}"
            )

        return {
            "published": result.success,
            "files_generated": result.files_generated,
            "errors": result.errors,
            "report_dir": str(reports_dir),
        }

    async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
        """Check that expected report files were created."""
        if not action_result.get("published"):
            return action_result

        report_dir = action_result.get("report_dir", "")
        expected = ["report.html", "summary.md", "artifact-manifest.json"]
        missing = [f for f in expected if report_dir and not (Path(report_dir) / f).exists()]
        action_result["missing_files"] = missing
        if missing:
            self.logger.narrate(f"Missing expected files: {', '.join(missing)}")

        return action_result

    async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
        """Always succeed — report failures must never block the loop."""
        published = validation.get("published", False)
        files = validation.get("files_generated", [])
        missing = validation.get("missing_files", [])
        errors = validation.get("errors", [])

        if published and not missing:
            self.logger.narrate(f"Report phase complete: {len(files)} files published.")
        elif published and missing:
            self.logger.narrate(f"Report phase done but {len(missing)} expected files missing.")
        else:
            reason = validation.get("reason", "unknown")
            self.logger.narrate(f"Report phase done (no reports generated: {reason}).")

        return PhaseResult(
            phase="report",
            success=True,
            should_continue=True,
            findings={
                "published": published,
                "files_count": len(files),
                "errors": errors,
            },
            artifacts={
                "files_generated": files,
                "report_dir": validation.get("report_dir", ""),
            },
        )
