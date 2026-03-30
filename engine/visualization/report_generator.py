"""Report generator — reads execution records and produces self-contained HTML reports.

Uses Jinja2 templates from templates/visual-report/ with embedded CSS/JS.
Produces a single HTML file that can be viewed in any browser without a server.

When ``visualization_engine`` is ``"threejs"`` (default), Three.js + OrbitControls
are inlined from vendored files (``templates/visual-report/vendor/``), producing a
fully self-contained report with no external dependencies.  When ``"d3"``, the 3D
section is omitted and only the 2D D3.js decision tree + action map are included.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

from engine.config import ReportingConfig
from engine.phases.base import PHASE_TOOL_SETS
from engine.visualization.action_map import build_action_map
from engine.visualization.comparison import build_comparison
from engine.visualization.decision_tree import build_decision_tree
from engine.visualization.narrative.formatter import enrich_scene_with_narratives
from engine.visualization.narrative.summary import build_landing
from engine.visualization.scene.builder import SceneBuilder
from engine.visualization.scene.timeline import build_timeline

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "templates" / "visual-report"
_VENDOR_DIR = _TEMPLATES_DIR / "vendor"

_VENDOR_FILES: dict[str, str] = {
    "three_js": "three.min.js",
    "orbit_controls_js": "orbit-controls.min.js",
    "d3_js": "d3.v7.min.js",
}

_AGENT_DESCRIPTIONS: dict[str, str] = {
    "triage": (
        "Analyzes the bug report, identifies root cause location, "
        "and locates relevant source files."
    ),
    "implement": (
        "Writes and applies code changes to fix the identified bug using the OODA cycle."
    ),
    "review": ("Reviews the implementation for correctness, style, edge cases, and completeness."),
    "validate": ("Runs tests, creates the pull request, and validates the fix works end-to-end."),
    "report": ("Generates the execution report with visualizations and publishes artifacts."),
    "ci_remediate": ("Monitors CI after PR creation and automatically remediates any failures."),
}

_AGENT_ICONS: dict[str, str] = {
    "triage": "&#x1F50D;",
    "implement": "&#x1F6E0;",
    "review": "&#x1F50E;",
    "validate": "&#x2705;",
    "report": "&#x1F4CA;",
    "ci_remediate": "&#x1F527;",
}


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
    narrative: str = ""
    transcript_calls: list[dict[str, Any]] = field(default_factory=list)
    scene_data: dict[str, Any] = field(default_factory=dict)
    timeline_data: dict[str, Any] = field(default_factory=dict)
    landing_data: dict[str, Any] = field(default_factory=dict)
    agents: list[dict[str, Any]] = field(default_factory=list)

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
            "narrative": self.narrative,
            "transcript_calls": self.transcript_calls,
            "scene_data": self.scene_data,
            "timeline_data": self.timeline_data,
            "landing_data": self.landing_data,
            "agents": self.agents,
        }


def extract_report_data(
    execution: dict[str, Any],
    transcript_calls: list[dict[str, Any]] | None = None,
    *,
    visualization_engine: str = "threejs",
) -> ReportData:
    """Extract structured ReportData from a raw execution record dict.

    Accepts the full execution.json structure (with top-level "execution" key)
    or a flat execution dict.  ``transcript_calls`` is the list of LLM call
    records from ``transcript-calls.json`` (full prompts and responses).

    When *visualization_engine* is ``"d3"``, the 3D scene, timeline, and landing
    data are skipped (empty dicts), saving computation for legacy-mode reports.
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

    scene_dict: dict[str, Any] = {}
    timeline_dict: dict[str, Any] = {}
    landing_dict: dict[str, Any] = {}

    if visualization_engine != "d3":
        builder = SceneBuilder()
        scene = builder.build(execution)
        comparison_dict = comparison.to_dict()
        if comparison_dict.get("enabled"):
            builder.add_comparison_ghosts(scene, comparison_dict)
        scene_dict = scene.to_dict()
        enrich_scene_with_narratives(scene_dict, actions)
        timeline_dict = build_timeline(execution).to_dict()
        landing_dict = build_landing(execution).to_dict()

    from engine.visualization.publisher import build_narrative  # local to avoid cycle

    narrative = build_narrative(execution)

    agents = _build_agents_data(phases_summary, iterations)

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
        narrative=narrative,
        transcript_calls=transcript_calls or [],
        scene_data=scene_dict,
        timeline_data=timeline_dict,
        landing_data=landing_dict,
        agents=agents,
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


def _build_agents_data(
    phases_summary: list[dict[str, Any]],
    iterations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build per-agent metadata for the sidebar, including timing and source paths."""
    first_started: dict[str, str] = {}
    last_completed: dict[str, str] = {}
    for it in iterations:
        phase = it.get("phase", "unknown")
        started = it.get("started_at", "")
        completed = it.get("completed_at", "")
        if started and (phase not in first_started or started < first_started[phase]):
            first_started[phase] = started
        if completed and (phase not in last_completed or completed > last_completed[phase]):
            last_completed[phase] = completed

    agents: list[dict[str, Any]] = []
    for phase_info in phases_summary:
        name = phase_info["phase"]
        agents.append(
            {
                "name": name,
                "display_name": name.replace("_", " ").title(),
                "description": _AGENT_DESCRIPTIONS.get(name, ""),
                "icon": _AGENT_ICONS.get(name, "&#x2699;"),
                "source_file": f"engine/phases/{name}.py",
                "prompt_file": f"templates/prompts/{name}.md",
                "tools": PHASE_TOOL_SETS.get(name, []),
                "status": "success" if phase_info["successful"] else "failure",
                "duration_ms": phase_info["duration_ms"],
                "iterations": phase_info["iterations"],
                "llm_calls": phase_info["llm_call_count"],
                "tool_calls": phase_info["tool_call_count"],
                "started_at": first_started.get(name, ""),
                "completed_at": last_completed.get(name, ""),
            }
        )
    return agents


class ReportGenerator:
    """Generates self-contained HTML reports from execution records.

    The generator loads Jinja2 templates from ``templates/visual-report/``
    and renders them with extracted execution data. The output is a single
    HTML file with embedded CSS and JavaScript — no external dependencies.

    Args:
        templates_dir: Override for the templates directory.
        config: Reporting configuration. Controls ``visualization_engine``
            (``"threejs"`` or ``"d3"``), which determines whether Three.js
            and the 3D scene are included in the report.
    """

    def __init__(
        self,
        templates_dir: Path | str | None = None,
        config: ReportingConfig | None = None,
    ):
        self._templates_dir = Path(templates_dir) if templates_dir else _TEMPLATES_DIR
        self._config = config or ReportingConfig()
        self._env: Environment | None = None
        self._vendor_cache: dict[str, str] = {}

    @property
    def visualization_engine(self) -> str:
        return self._config.visualization_engine

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
        transcript_calls: list[dict[str, Any]] | None = None,
    ) -> str:
        """Generate an HTML report from an execution record.

        Args:
            execution: Raw execution record dict (from execution.json).
            output_path: If provided, write the HTML to this file path.
            template_name: Jinja2 template to render. Defaults to "report.html".
            transcript_calls: Full LLM call records (from transcript-calls.json).

        Returns:
            The rendered HTML string.
        """
        report_data = extract_report_data(
            execution,
            transcript_calls=transcript_calls,
            visualization_engine=self.visualization_engine,
        )
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

        Automatically loads ``transcript-calls.json`` from the sibling
        ``transcripts/`` directory if it exists, so that the report includes
        full LLM inference content.

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

        transcript_calls: list[dict[str, Any]] | None = None
        transcript_path = path.parent / "transcripts" / "transcript-calls.json"
        if transcript_path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                transcript_calls = json.loads(transcript_path.read_text(encoding="utf-8"))

        return self.generate(
            raw,
            output_path=output_path,
            template_name=template_name,
            transcript_calls=transcript_calls,
        )

    def available_templates(self) -> list[str]:
        """List available template files in the templates directory."""
        if not self._templates_dir.is_dir():
            return []
        return sorted(p.name for p in self._templates_dir.glob("*.html"))

    def _load_vendor_file(self, name: str) -> str:
        """Load a vendored JS file from the vendor directory, with caching."""
        if name in self._vendor_cache:
            return self._vendor_cache[name]
        filename = _VENDOR_FILES.get(name, "")
        if not filename:
            return ""
        vendor_dir = self._templates_dir / "vendor"
        path = vendor_dir / filename
        if not path.is_file():
            return ""
        content = path.read_text(encoding="utf-8")
        self._vendor_cache[name] = content
        return content

    def _render(self, report_data: ReportData, template_name: str) -> str:
        """Render a template with the given report data.

        Loads vendored JS libraries and passes them as template context so
        they can be inlined directly in ``<script>`` tags.  The template never
        references external CDN URLs.
        """
        try:
            template = self._jinja_env.get_template(template_name)
        except TemplateNotFound as exc:
            raise FileNotFoundError(
                f"Report template '{template_name}' not found in {self._templates_dir}"
            ) from exc

        engine = self.visualization_engine
        vendor_d3 = self._load_vendor_file("d3_js")
        vendor_three = ""
        vendor_orbit = ""
        if engine != "d3":
            vendor_three = self._load_vendor_file("three_js")
            vendor_orbit = self._load_vendor_file("orbit_controls_js")

        engine_repo_url = self._config.engine_repo_url

        return template.render(
            report=report_data,
            **report_data.to_dict(),
            visualization_engine=engine,
            engine_repo_url=engine_repo_url,
            vendor_d3_js=vendor_d3,
            vendor_three_js=vendor_three,
            vendor_orbit_controls_js=vendor_orbit,
        )


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
