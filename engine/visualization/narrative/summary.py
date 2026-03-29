"""Narrative summary builder — generates landing page data for the report.

Produces:
- A one-paragraph plain-English story of the execution
- Key metrics cards data (time, iterations, LLM calls, files modified, status)
- Phase timeline bar data (CSS-rendered horizontal bar chart, time per phase)

All output is deterministic and template-based — no LLM calls.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricCard:
    """A single metric card shown on the landing page."""

    label: str = ""
    value: str = ""
    unit: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
        }


@dataclass
class PhaseBar:
    """A segment in the phase timeline bar chart."""

    phase: str = ""
    duration_ms: float = 0.0
    percent: float = 0.0
    color: str = "#6b7280"
    status: str = "unknown"
    iterations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "duration_ms": round(self.duration_ms, 2),
            "percent": round(self.percent, 1),
            "color": self.color,
            "status": self.status,
            "iterations": self.iterations,
        }


@dataclass
class LandingData:
    """Complete landing page data for the report template."""

    story: str = ""
    metrics_cards: list[dict[str, Any]] = field(default_factory=list)
    phase_bars: list[dict[str, Any]] = field(default_factory=list)
    total_duration_display: str = ""
    comparison_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "story": self.story,
            "metrics_cards": self.metrics_cards,
            "phase_bars": self.phase_bars,
            "total_duration_display": self.total_duration_display,
            "comparison_summary": self.comparison_summary,
        }


PHASE_COLORS: dict[str, str] = {
    "triage": "#58a6ff",
    "implement": "#3fb950",
    "review": "#d29922",
    "validate": "#bc8cff",
    "report": "#56d4dd",
    "ci_remediate": "#f0883e",
}

_FALLBACK_COLOR = "#6b7280"


class NarrativeSummaryBuilder:
    """Builds landing page data from execution records.

    All methods are deterministic and template-based — no LLM calls.
    The builder extracts data from the execution record structure
    (``execution.json`` format) and produces display-ready data.
    """

    def build_landing(self, execution: dict[str, Any]) -> LandingData:
        """Build complete landing page data from an execution record."""
        exec_data = execution.get("execution", execution)
        story = self.build_story(exec_data)
        metrics = self.build_metrics_cards(exec_data)
        phase_bars = self.build_phase_timeline(exec_data)
        total_display = _format_duration_display(exec_data)
        comparison = self._build_comparison_summary(exec_data)

        return LandingData(
            story=story,
            metrics_cards=[m.to_dict() for m in metrics],
            phase_bars=[b.to_dict() for b in phase_bars],
            total_duration_display=total_display,
            comparison_summary=comparison,
        )

    def build_story(self, exec_data: dict[str, Any]) -> str:
        """Build a one-paragraph plain-English story of the execution."""
        trigger = exec_data.get("trigger", {})
        target = exec_data.get("target", {})
        result = exec_data.get("result", {})
        iterations = exec_data.get("iterations", [])
        metrics = exec_data.get("metrics", {})
        status = result.get("status", "unknown")

        source_url = trigger.get("source_url", "")
        repo = target.get("repo", "") or target.get("repo_path", "")
        if repo:
            repo = repo.rstrip("/").rsplit("/", 1)[-1]

        parts: list[str] = []

        # Opening — what issue, what repo
        issue_desc = _extract_issue_desc(source_url, repo)
        triage_info = _extract_triage_info(iterations)
        if triage_info:
            parts.append(f"The agent received {issue_desc}{triage_info}.")
        else:
            parts.append(f"The agent processed {issue_desc}.")

        # Implementation attempts
        impl_info = _extract_impl_info(iterations)
        if impl_info:
            parts.append(impl_info)

        # Review outcome
        review_info = _extract_review_info(iterations)
        if review_info:
            parts.append(review_info)

        # PR creation
        pr_url = result.get("pr_url", "")
        if pr_url:
            parts.append(f"Opened {pr_url}.")

        # Timing
        total_ms = metrics.get("total_duration_ms", 0)
        phase_count = len({it.get("phase") for it in iterations if it.get("phase")})
        if total_ms > 0 and phase_count > 0:
            parts.append(
                f"Total time: {_format_ms(total_ms)} across {phase_count} "
                f"phase{'s' if phase_count != 1 else ''}."
            )

        # Final status
        status_text = {
            "success": "The run completed successfully.",
            "failure": "The run ended in failure.",
            "escalated": "The agent escalated to human review.",
            "timeout": "The run timed out.",
        }.get(status, f"Final status: {status}.")
        parts.append(status_text)

        return " ".join(parts)

    def build_metrics_cards(self, exec_data: dict[str, Any]) -> list[MetricCard]:
        """Build key metrics cards for the landing page."""
        result = exec_data.get("result", {})
        metrics = exec_data.get("metrics", {})
        iterations = exec_data.get("iterations", [])
        actions = exec_data.get("actions", [])
        status = result.get("status", "unknown")

        total_ms = metrics.get("total_duration_ms", 0)
        total_iters = result.get("total_iterations", len(iterations))
        llm_calls = metrics.get("total_llm_calls", 0) or sum(
            1 for a in actions if a.get("action_type") == "llm_query"
        )
        files_modified = _count_files_modified(actions)
        tests_run = _count_tests_run(actions)
        tokens_total = (metrics.get("total_tokens_in", 0) or 0) + (
            metrics.get("total_tokens_out", 0) or 0
        )

        cards = [
            MetricCard(
                label="Total Time",
                value=_format_ms(total_ms) if total_ms else "—",
                unit="",
                status=status,
            ),
            MetricCard(
                label="Iterations",
                value=str(total_iters),
                unit="",
                status=status,
            ),
            MetricCard(
                label="LLM Calls",
                value=str(llm_calls),
                unit="",
                status=status,
            ),
            MetricCard(
                label="Files Modified",
                value=str(files_modified),
                unit="",
                status=status,
            ),
            MetricCard(
                label="Tests Run",
                value=str(tests_run),
                unit="",
                status=status,
            ),
            MetricCard(
                label="Status",
                value=status.title(),
                unit="",
                status=status,
            ),
        ]

        if tokens_total > 0:
            cards.insert(
                3,
                MetricCard(
                    label="Total Tokens",
                    value=f"{tokens_total:,}",
                    unit="",
                    status=status,
                ),
            )

        return cards

    def build_phase_timeline(self, exec_data: dict[str, Any]) -> list[PhaseBar]:
        """Build horizontal bar chart data showing time per phase."""
        metrics = exec_data.get("metrics", {})
        iterations = exec_data.get("iterations", [])
        time_per_phase = metrics.get("time_per_phase_ms", {})
        iter_counts = metrics.get("phase_iteration_counts", {})

        phase_order: list[str] = []
        for it in iterations:
            phase = it.get("phase", "")
            if phase and phase not in phase_order:
                phase_order.append(phase)

        if not phase_order and time_per_phase:
            phase_order = list(time_per_phase.keys())

        total_ms = sum(time_per_phase.get(p, 0) for p in phase_order)
        if total_ms <= 0:
            total_ms = sum(it.get("duration_ms", 0) for it in iterations)

        bars: list[PhaseBar] = []
        for phase in phase_order:
            duration = time_per_phase.get(phase, 0)
            if duration <= 0:
                duration = sum(
                    it.get("duration_ms", 0) for it in iterations if it.get("phase") == phase
                )
            percent = (duration / total_ms * 100) if total_ms > 0 else 0
            phase_iters = [it for it in iterations if it.get("phase") == phase]
            successful = any(it.get("result", {}).get("success") for it in phase_iters)
            phase_iter_count = iter_counts.get(phase, len(phase_iters))

            bars.append(
                PhaseBar(
                    phase=phase,
                    duration_ms=duration,
                    percent=percent,
                    color=PHASE_COLORS.get(phase, _FALLBACK_COLOR),
                    status="success" if successful else "failure",
                    iterations=phase_iter_count,
                )
            )

        return bars

    def _build_comparison_summary(self, exec_data: dict[str, Any]) -> str:
        """Build a short comparison summary if comparison mode is active."""
        result = exec_data.get("result", {})
        comparison = result.get("comparison", {})
        if not comparison:
            return ""

        score = comparison.get("similarity_score", 0)
        analysis = comparison.get("analysis", "")

        if score > 0:
            summary = f"Similarity to human fix: {score:.0%}."
            if analysis:
                first_sentence = analysis.split(".")[0].strip()
                if first_sentence:
                    summary += f" {first_sentence}."
            return summary
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_issue_desc(source_url: str, repo: str) -> str:
    """Build a concise issue/repo description fragment."""
    if source_url and repo:
        issue_num = ""
        if "/issues/" in source_url:
            issue_num = source_url.rsplit("/issues/", 1)[-1].split("/")[0]
        if issue_num:
            return f"issue #{issue_num} from {repo}"
        return f"an issue from {repo}"
    if source_url:
        return f"issue {source_url}"
    if repo:
        return f"an issue in {repo}"
    return "a bug-fix loop"


def _extract_triage_info(iterations: list[dict[str, Any]]) -> str:
    """Extract triage classification as an inline clause."""
    triage_iters = [i for i in iterations if i.get("phase") == "triage"]
    if not triage_iters:
        return ""

    last = triage_iters[-1]
    findings = last.get("findings", {})
    classification = findings.get("classification", "")
    components = findings.get("affected_components", [])

    if not classification:
        res = last.get("result", {})
        if res.get("escalate"):
            reason = res.get("escalation_reason", "")
            if reason:
                return f", which triage escalated: {reason}"
            return ", which triage escalated for human review"
        return ""

    confidence = findings.get("confidence")
    conf_str = ""
    if confidence is not None:
        with contextlib.suppress(TypeError, ValueError):
            conf_str = f" with {float(confidence):.2f} confidence"

    parts = []
    if classification == "bug":
        if components:
            comp_str = ", ".join(str(c) for c in components[:3])
            parts.append(f" (a {classification} in {comp_str}{conf_str})")
        else:
            parts.append(f" (classified as {classification}{conf_str})")
    else:
        parts.append(f" (classified as {classification}{conf_str})")

    return "".join(parts)


def _extract_impl_info(iterations: list[dict[str, Any]]) -> str:
    """Summarize implementation attempts."""
    impl_iters = [i for i in iterations if i.get("phase") == "implement"]
    if not impl_iters:
        return ""

    n = len(impl_iters)
    succeeded = any(i.get("result", {}).get("success") for i in impl_iters)
    word = "attempt" if n == 1 else "attempts"

    files_changed: set[str] = set()
    for it in impl_iters:
        arts = it.get("artifacts", {})
        fc = arts.get("files_changed", [])
        if isinstance(fc, list):
            files_changed.update(str(f) for f in fc)

    desc = f"It implemented a fix in {n} {word}"
    if files_changed:
        flist = ", ".join(sorted(files_changed)[:3])
        desc += f" (modifying {flist})"
    if succeeded:
        desc += "."
    else:
        desc += ", but implementation failed to converge."
    return desc


def _extract_review_info(iterations: list[dict[str, Any]]) -> str:
    """Summarize review outcome."""
    review_iters = [i for i in iterations if i.get("phase") == "review"]
    if not review_iters:
        return ""

    last = review_iters[-1]
    findings = last.get("findings", {})
    verdict = findings.get("verdict", "")
    res = last.get("result", {})

    if verdict == "approve" or res.get("success"):
        n = len(review_iters)
        if n > 1:
            return f"Self-review approved the fix on attempt {n}."
        return "Self-review approved the fix."
    if verdict == "block":
        reason = findings.get("summary", "security or injection concern")
        return f"Self-review blocked the fix: {reason}."
    if verdict == "request_changes":
        return "Self-review requested changes."
    return ""


def _count_files_modified(actions: list[dict[str, Any]]) -> int:
    """Count unique files written by the agent."""
    paths: set[str] = set()
    for a in actions:
        if a.get("action_type") in ("file_write", "file_edit"):
            inp = a.get("input", {})
            path = inp.get("path", "") or (inp.get("context") or {}).get("path", "")
            if path:
                paths.add(path)
    return len(paths)


def _count_tests_run(actions: list[dict[str, Any]]) -> int:
    """Count test/command runs that look like test executions."""
    count = 0
    test_keywords = ("test", "pytest", "go test", "npm test", "jest", "cargo test")
    for a in actions:
        if a.get("action_type") in ("shell_run", "command_run", "tool_execution"):
            desc = (a.get("input", {}).get("description", "") or "").lower()
            cmd = (a.get("input", {}).get("command", "") or "").lower()
            if any(kw in desc or kw in cmd for kw in test_keywords):
                count += 1
    return count


def _format_ms(ms: float) -> str:
    """Format milliseconds as human-readable duration."""
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


def _format_duration_display(exec_data: dict[str, Any]) -> str:
    """Build a display string for total execution duration."""
    metrics = exec_data.get("metrics", {})
    total_ms = metrics.get("total_duration_ms", 0)
    if total_ms > 0:
        return _format_ms(total_ms)
    return "—"


def build_landing(execution: dict[str, Any]) -> LandingData:
    """Module-level convenience — build landing data from an execution record."""
    return NarrativeSummaryBuilder().build_landing(execution)
