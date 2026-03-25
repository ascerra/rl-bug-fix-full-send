"""Report generator — reads execution records and produces self-contained HTML reports.

Uses Jinja2 templates from templates/visual-report/ with embedded CSS/JS.
Produces a single HTML file that can be viewed in any browser without a server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from engine.visualization.action_map import build_action_map
from engine.visualization.comparison import build_comparison
from engine.visualization.decision_tree import build_decision_tree

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "visual-report"


@dataclass
class ReportData:
    """Structured data extracted from an execution record for template rendering."""

    execution_id: str = ""
    started_at: str = ""
    completed_at: str = ""
    status: str = "unknown"
    total_iterations: int = 0
    trigger: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    iterations: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    phase_results: list[dict[str, Any]] = field(default_factory=list)
    phases_summary: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    decision_tree: dict[str, Any] = field(default_factory=dict)
    action_map: dict[str, Any] = field(default_factory=dict)
    comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "total_iterations": self.total_iterations,
            "trigger": self.trigger,
            "target": self.target,
            "config": self.config,
            "iterations": self.iterations,
            "actions": self.actions,
            "metrics": self.metrics,
            "phase_results": self.phase_results,
            "phases_summary": self.phases_summary,
            "errors": self.errors,
            "decision_tree": self.decision_tree,
            "action_map": self.action_map,
            "comparison": self.comparison,
        }


def extract_report_data(execution: dict[str, Any]) -> ReportData:
    """Extract structured ReportData from a raw execution record dict.

    Accepts the full execution.json structure (with top-level "execution" key)
    or a flat execution dict.
    """
    exec_data = execution.get("execution", execution)

    result = exec_data.get("result", {})
    metrics = exec_data.get("metrics", {})
    iterations = exec_data.get("iterations", [])
    actions = exec_data.get("actions", [])

    phases_summary = _build_phases_summary(iterations, actions, metrics)

    tree = build_decision_tree(execution)
    action_map = build_action_map(execution)
    comparison = build_comparison(execution)

    return ReportData(
        execution_id=exec_data.get("id", ""),
        started_at=exec_data.get("started_at", ""),
        completed_at=exec_data.get("completed_at", ""),
        status=result.get("status", "unknown"),
        total_iterations=result.get("total_iterations", len(iterations)),
        trigger=exec_data.get("trigger", {}),
        target=exec_data.get("target", {}),
        config=exec_data.get("config", {}),
        iterations=iterations,
        actions=actions,
        metrics=metrics,
        phase_results=result.get("phase_results", []),
        phases_summary=phases_summary,
        errors=metrics.get("errors", []),
        decision_tree=tree.to_dict(),
        action_map=action_map.to_dict(),
        comparison=comparison.to_dict(),
    )


def _build_phases_summary(
    iterations: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build per-phase summary aggregating iterations, actions, and timing."""
    phase_order: list[str] = []
    phase_iters: dict[str, list[dict[str, Any]]] = {}
    for it in iterations:
        phase = it.get("phase", "unknown")
        if phase not in phase_iters:
            phase_order.append(phase)
            phase_iters[phase] = []
        phase_iters[phase].append(it)

    phase_actions: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        phase = action.get("phase", "unknown")
        phase_actions.setdefault(phase, []).append(action)

    time_per_phase = metrics.get("time_per_phase_ms", {})
    iter_counts = metrics.get("phase_iteration_counts", {})

    summaries = []
    for phase in phase_order:
        iters = phase_iters.get(phase, [])
        acts = phase_actions.get(phase, [])
        successful = any(it.get("result", {}).get("success") for it in iters)
        duration_ms = time_per_phase.get(phase, 0.0)

        llm_calls = [a for a in acts if a.get("action_type") == "llm_query"]
        tool_calls = [a for a in acts if a.get("action_type") not in ("llm_query", "escalation")]

        summaries.append(
            {
                "phase": phase,
                "iterations": iter_counts.get(phase, len(iters)),
                "successful": successful,
                "duration_ms": round(duration_ms, 2),
                "duration_s": round(duration_ms / 1000, 1) if duration_ms else 0.0,
                "action_count": len(acts),
                "llm_call_count": len(llm_calls),
                "tool_call_count": len(tool_calls),
            }
        )

    return summaries


class ReportGenerator:
    """Generates self-contained HTML reports from execution records.

    The generator loads Jinja2 templates from ``templates/visual-report/``
    and renders them with extracted execution data. The output is a single
    HTML file with embedded CSS — no external dependencies required.
    """

    def __init__(self, templates_dir: Path | str | None = None):
        self._templates_dir = Path(templates_dir) if templates_dir else _TEMPLATES_DIR
        self._env: Environment | None = None

    @property
    def _jinja_env(self) -> Environment:
        if self._env is None:
            self._env = Environment(
                loader=FileSystemLoader(str(self._templates_dir)),
                undefined=StrictUndefined,
                autoescape=True,
            )
            self._env.filters["to_json"] = _to_json_filter
            self._env.filters["to_json_safe"] = _to_json_safe_filter
            self._env.filters["format_duration"] = _format_duration_filter
            self._env.filters["status_color"] = _status_color_filter
            self._env.filters["status_icon"] = _status_icon_filter
        return self._env

    def generate(
        self,
        execution: dict[str, Any],
        output_path: Path | str | None = None,
        template_name: str = "report.html",
    ) -> str:
        """Generate an HTML report from an execution record.

        Args:
            execution: Raw execution record dict (from execution.json).
            output_path: If provided, write the HTML to this file path.
            template_name: Jinja2 template to render. Defaults to "report.html".

        Returns:
            The rendered HTML string.
        """
        report_data = extract_report_data(execution)
        html = self._render(report_data, template_name)

        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")

        return html

    def generate_from_file(
        self,
        execution_json_path: Path | str,
        output_path: Path | str | None = None,
        template_name: str = "report.html",
    ) -> str:
        """Generate an HTML report from an execution.json file.

        Args:
            execution_json_path: Path to execution.json.
            output_path: If provided, write the HTML to this file path.
            template_name: Jinja2 template to render.

        Returns:
            The rendered HTML string.

        Raises:
            FileNotFoundError: If execution_json_path does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        path = Path(execution_json_path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return self.generate(raw, output_path=output_path, template_name=template_name)

    def available_templates(self) -> list[str]:
        """List available template files in the templates directory."""
        if not self._templates_dir.is_dir():
            return []
        return sorted(p.name for p in self._templates_dir.glob("*.html"))

    def _render(self, report_data: ReportData, template_name: str) -> str:
        """Render a template with the given report data."""
        try:
            template = self._jinja_env.get_template(template_name)
        except TemplateNotFound as exc:
            raise FileNotFoundError(
                f"Report template '{template_name}' not found in {self._templates_dir}"
            ) from exc
        return template.render(report=report_data, **report_data.to_dict())


def _to_json_filter(value: Any, indent: int = 2) -> str:
    """Jinja2 filter: serialize value to pretty-printed JSON."""
    return json.dumps(value, indent=indent, default=str)


def _to_json_safe_filter(value: Any) -> str:
    """Jinja2 filter: serialize to JSON safe for embedding in ``<script>`` tags.

    Escapes ``</`` sequences to prevent premature script tag closure.
    The output is intended to be used with ``|safe`` in the template.
    """
    s = json.dumps(value, indent=None, default=str)
    return s.replace("</", r"<\/")


def _format_duration_filter(ms: float) -> str:
    """Jinja2 filter: format milliseconds as human-readable duration."""
    if ms < 1000:
        return f"{ms:.0f}ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    return f"{hours:.1f}h"


def _status_color_filter(status: str) -> str:
    """Jinja2 filter: return a CSS color class for a given status."""
    return {
        "success": "status-success",
        "failure": "status-failure",
        "escalated": "status-escalated",
        "timeout": "status-timeout",
    }.get(status, "status-unknown")


def _status_icon_filter(status: str) -> str:
    """Jinja2 filter: return a status indicator character."""
    return {
        "success": "PASS",
        "failure": "FAIL",
        "escalated": "ESCALATED",
        "timeout": "TIMEOUT",
    }.get(status, "?")
