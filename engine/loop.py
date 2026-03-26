"""Ralph Loop engine — the core iterative execution cycle.

Implements: OBSERVE → PLAN → ACT → VALIDATE → REFLECT
Manages phase transitions, iteration caps, time budgets, and escalation.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from engine.config import EngineConfig
from engine.integrations.llm import LLMProvider
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.observability.transcript import TranscriptWriter
from engine.phases.base import Phase, PhaseResult
from engine.secrets import SecretRedactor
from engine.tools.executor import ToolExecutor
from engine.workflow.monitor import WorkflowMonitor


@dataclass
class ExecutionRecord:
    """Complete record of a Ralph Loop execution."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str = ""
    trigger: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    iterations: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution": {
                "id": self.id,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "trigger": self.trigger,
                "target": self.target,
                "config": self.config,
                "iterations": self.iterations,
                "result": self.result,
                "metrics": self.metrics,
                "actions": self.actions,
            }
        }


PHASE_ORDER = ["triage", "implement", "review", "validate", "report"]


class RalphLoop:
    """The Ralph Loop engine.

    Executes phases in sequence, managing iteration within and across phases.
    Enforces iteration caps and time budgets. Escalates when limits are reached.

    Phases are registered via a registry mapping phase name → Phase subclass.
    Unregistered phases are skipped with a warning (allows incremental development).
    """

    def __init__(
        self,
        config: EngineConfig,
        llm: LLMProvider,
        issue_url: str,
        repo_path: str,
        output_dir: str = "./output",
        comparison_ref: str = "",
        phase_registry: dict[str, type[Phase]] | None = None,
        workflow_monitor: WorkflowMonitor | None = None,
        redactor: SecretRedactor | None = None,
    ):
        self.config = config
        self.llm = llm
        self.issue_url = issue_url
        self.repo_path = repo_path
        self.output_dir = Path(output_dir)
        self.comparison_ref = comparison_ref
        self._phase_registry: dict[str, type[Phase]] = dict(phase_registry or {})
        self._monitor = workflow_monitor
        self._redactor = redactor

        self.logger = StructuredLogger(
            output_path=self.output_dir / "log.json",
            progress_path=self.output_dir / "progress.md",
            redactor=redactor,
        )
        self.tracer = Tracer(redactor=redactor)
        self.metrics = LoopMetrics()
        self.transcript = TranscriptWriter(
            output_path=self.output_dir / "transcripts" / "transcript.html",
            redactor=redactor,
        )
        self.execution = ExecutionRecord(
            trigger={"type": "github_issue", "source_url": issue_url},
            target={"repo_path": repo_path, "comparison_ref": comparison_ref},
        )

        self._start_time: float = 0
        self._total_iterations: int = 0
        self._consecutive_retries: int = 0

    def register_phase(self, name: str, phase_class: type[Phase]) -> None:
        """Register a phase implementation by name."""
        self._phase_registry[name] = phase_class

    async def run(self) -> ExecutionRecord:
        """Execute the full Ralph Loop.

        Runs phases in PHASE_ORDER sequence. Each iteration:
        1. Check time budget and iteration cap
        2. Execute the current phase
        3. Handle the result (escalate, fail, transition, retry, or advance)

        Returns the complete ExecutionRecord with all iterations, metrics, and actions.
        """
        self._start_time = time.monotonic()
        self.logger.info(f"Starting Ralph Loop for issue: {self.issue_url}")
        self.logger.info(f"Target repo: {self.repo_path}")
        self.logger.info(
            f"Max iterations: {self.config.loop.max_iterations}, "
            f"Time budget: {self.config.loop.time_budget_minutes}m"
        )

        self.logger.write_progress_heading("# Ralph Loop Progress")
        self.logger.narrate(
            f"Starting Ralph Loop for {self.issue_url} "
            f"(max {self.config.loop.max_iterations} iterations, "
            f"{self.config.loop.time_budget_minutes}m budget)"
        )

        if self._monitor:
            self.logger.info(
                f"Self-monitoring active: {self._monitor.run_url}",
                workflow_run_id=self._monitor._run_id,
            )
            self.execution.target["workflow"] = self._monitor.context.to_dict()
        else:
            self.logger.debug("Self-monitoring not available (not in GitHub Actions)")

        phase_results: list[PhaseResult] = []
        current_phase_idx = 0
        status = "success"
        review_rejections = 0

        while current_phase_idx < len(PHASE_ORDER):
            if self._check_time_budget():
                self.logger.warn("Time budget exceeded — escalating to human")
                self.logger.narrate("Time budget exceeded. Escalating to human review.")
                status = "timeout"
                self._record_escalation("Time budget exceeded", phase_results)
                break

            if self._total_iterations >= self.config.loop.max_iterations:
                self.logger.warn("Iteration cap reached — escalating to human")
                self.logger.narrate(
                    f"Iteration cap ({self.config.loop.max_iterations}) reached. "
                    "Escalating to human review."
                )
                status = "escalated"
                self._record_escalation("Iteration cap reached", phase_results)
                break

            if self._monitor:
                health = await self._check_workflow_health()
                if health and not health.get("healthy", True):
                    self.logger.warn(
                        "Workflow health check failed — recording context",
                        failed_steps=health.get("failed_steps", []),
                    )
                    self.tracer.record_action(
                        action_type="workflow_health_check",
                        description="CI step failure detected",
                        input_context=health,
                        output_success=False,
                    )

            phase_name = PHASE_ORDER[current_phase_idx]
            self._total_iterations += 1
            self.logger.set_iteration(self._total_iterations)
            self.tracer.set_iteration(self._total_iterations)
            self.metrics.record_iteration(phase_name)

            self.logger.write_progress_heading(
                f"## Iteration {self._total_iterations} — {phase_name}"
            )
            self.logger.narrate(f"Starting {phase_name} phase (iteration {self._total_iterations})")

            if phase_name == "report":
                self._populate_execution_record_for_snapshot(status, phase_results)

            iteration_started = datetime.now(UTC).isoformat()
            phase_start = time.monotonic()

            result = await self._execute_phase(phase_name, phase_results)

            phase_duration_ms = (time.monotonic() - phase_start) * 1000
            self.metrics.record_phase_time(phase_name, phase_duration_ms)

            self.execution.iterations.append(
                {
                    "number": self._total_iterations,
                    "phase": phase_name,
                    "started_at": iteration_started,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "duration_ms": round(phase_duration_ms, 2),
                    "result": {
                        "success": result.success,
                        "should_continue": result.should_continue,
                        "next_phase": result.next_phase,
                        "escalate": result.escalate,
                        "escalation_reason": result.escalation_reason or "",
                    },
                    "findings": _truncate_dict(result.findings, max_str_len=2000),
                    "artifacts": _truncate_dict(result.artifacts, max_str_len=2000),
                }
            )
            phase_results.append(result)

            elapsed_min = (time.monotonic() - self._start_time) / 60
            self.logger.narrate(
                f"Phase {phase_name} "
                f"{'succeeded' if result.success else 'failed'} "
                f"({phase_duration_ms:.0f}ms, {elapsed_min:.1f}m elapsed)"
            )

            if result.escalate:
                reason = result.escalation_reason or f"Escalated during {phase_name}"
                self.logger.narrate(f"ESCALATION: {reason[:200]}")
                status = "escalated"
                self._record_escalation(reason, phase_results)
                break

            if not result.success and not result.should_continue:
                status = "failure"
                self.logger.error(f"Phase {phase_name} failed — loop stopping")
                break

            # Explicit next-phase transition (e.g., review → implement backtrack)
            if result.next_phase:
                target_idx = self._phase_index(result.next_phase)
                if target_idx is not None:
                    if phase_name == "review" and result.next_phase == "implement":
                        review_rejections += 1
                        threshold = self.config.loop.escalation_on_review_block_after
                        if review_rejections >= threshold:
                            self.logger.warn(
                                f"Review rejected {review_rejections} times "
                                f"(threshold={threshold}) — escalating"
                            )
                            self.logger.narrate(
                                f"Review rejected {review_rejections} times "
                                f"(limit {threshold}). Escalating to human."
                            )
                            status = "escalated"
                            self._record_escalation(
                                f"Review blocked after {review_rejections} rejections",
                                phase_results,
                            )
                            break
                    if target_idx <= current_phase_idx:
                        self._consecutive_retries += 1
                        delay = self._compute_backoff_delay()
                        self.logger.info(
                            f"Phase transition: {phase_name} → {result.next_phase} "
                            f"(backoff {delay:.1f}s)"
                        )
                        self.logger.narrate(
                            f"Transitioning: {phase_name} → {result.next_phase} "
                            f"(backing off {delay:.1f}s)"
                        )
                        await asyncio.sleep(delay)
                    else:
                        self._consecutive_retries = 0
                        self.logger.info(f"Phase transition: {phase_name} → {result.next_phase}")
                        self.logger.narrate(f"Transitioning: {phase_name} → {result.next_phase}")
                    current_phase_idx = target_idx
                    continue

            if result.success:
                current_phase_idx += 1
                self._consecutive_retries = 0
            else:
                self._consecutive_retries += 1
                delay = self._compute_backoff_delay()
                self.logger.info(
                    f"Phase {phase_name} failed but retryable — retrying after {delay:.1f}s backoff"
                )
                self.logger.narrate(
                    f"Phase {phase_name} failed but retryable "
                    f"— backing off {delay:.1f}s before retry."
                )
                await asyncio.sleep(delay)

        self.execution.completed_at = datetime.now(UTC).isoformat()
        self.execution.result = {
            "status": status,
            "total_iterations": self._total_iterations,
            "phase_results": [
                {"phase": r.phase, "success": r.success, "escalate": r.escalate}
                for r in phase_results
            ],
        }
        self.execution.metrics = self.metrics.to_dict()
        self.execution.actions = self.tracer.get_actions_as_dicts()

        self._write_transcript_data()
        self.transcript.finalize()
        self._write_outputs(status)

        total_min = (time.monotonic() - self._start_time) / 60
        self.logger.narrate(
            f"Ralph Loop complete: status={status}, "
            f"{self._total_iterations} iterations, {total_min:.1f}m elapsed"
        )
        self.logger.info(
            f"Ralph Loop complete: status={status}, iterations={self._total_iterations}"
        )
        self.logger.flush()

        return self.execution

    async def _execute_phase(
        self,
        phase_name: str,
        prior_results: list[PhaseResult],
    ) -> PhaseResult:
        """Instantiate and execute a single phase.

        If no implementation is registered for this phase, returns a success result
        that advances to the next phase (allows incremental development).
        """
        phase_cls = self._phase_registry.get(phase_name)
        if phase_cls is None:
            self.logger.warn(f"No implementation registered for phase: {phase_name} — skipping")
            return PhaseResult(
                phase=phase_name,
                success=True,
                should_continue=True,
                next_phase=self._next_phase_name(phase_name),
                findings={"skipped": True, "reason": "No implementation registered"},
            )

        try:
            phase_tools = phase_cls.get_allowed_tools() or None
            tool_executor = ToolExecutor(
                repo_path=self.repo_path,
                logger=self.logger,
                tracer=self.tracer,
                metrics=self.metrics,
                allowed_tools=phase_tools,
                redactor=self._redactor,
            )

            issue_data: dict[str, Any] = {"url": self.issue_url}
            if phase_name == "report":
                issue_data["_execution_snapshot"] = self.execution.to_dict()
                issue_data["_output_dir"] = str(self.output_dir)
                issue_data["_transcript_calls"] = self.transcript.get_calls()

            phase = phase_cls(
                llm=self.llm,
                logger=self.logger,
                tracer=self.tracer,
                repo_path=self.repo_path,
                issue_data=issue_data,
                prior_phase_results=prior_results,
                tool_executor=tool_executor,
                config=self.config,
                metrics=self.metrics,
                transcript=self.transcript,
            )

            self.logger.info(f"Executing phase: {phase_name}")
            result = await phase.execute()
            result.phase = phase_name
            return result
        except Exception as exc:
            self.logger.error(f"Phase {phase_name} raised: {exc}")
            self.metrics.record_error(str(exc))
            return PhaseResult(
                phase=phase_name,
                success=False,
                should_continue=False,
                escalate=True,
                escalation_reason=f"Phase {phase_name} raised: {exc}",
            )

    def _next_phase_name(self, current_phase: str) -> str:
        """Return the next phase name in sequence, or empty string if at the end."""
        try:
            idx = PHASE_ORDER.index(current_phase)
            return PHASE_ORDER[idx + 1] if idx + 1 < len(PHASE_ORDER) else ""
        except ValueError:
            return ""

    def _phase_index(self, phase_name: str) -> int | None:
        """Return the index of a phase in PHASE_ORDER, or None if not found."""
        try:
            return PHASE_ORDER.index(phase_name)
        except ValueError:
            return None

    def _populate_execution_record_for_snapshot(
        self,
        status: str,
        phase_results: list[PhaseResult],
    ) -> None:
        """Write current metrics, actions, and result into the execution record.

        Called before the report phase snapshot so the generated HTML report
        has access to the real execution data instead of empty defaults.
        """
        self.execution.result = {
            "status": status,
            "total_iterations": self._total_iterations,
            "phase_results": [
                {"phase": r.phase, "success": r.success, "escalate": r.escalate}
                for r in phase_results
            ],
        }
        self.execution.metrics = self.metrics.to_dict()
        self.execution.actions = self.tracer.get_actions_as_dicts()

    def _record_escalation(self, reason: str, phase_results: list[PhaseResult]) -> None:
        """Record escalation context for human review."""
        elapsed_min = (time.monotonic() - self._start_time) / 60
        self.tracer.record_action(
            action_type="escalation",
            description=f"Escalated: {reason}",
            input_context={
                "reason": reason,
                "total_iterations": self._total_iterations,
                "elapsed_minutes": round(elapsed_min, 2),
                "phases_completed": [r.phase for r in phase_results if r.success],
            },
            output_success=False,
        )
        self.logger.warn(f"ESCALATION: {reason}")

    def _compute_backoff_delay(self) -> float:
        """Compute exponential backoff delay: base * 2^(retries-1), capped at max."""
        base = self.config.loop.retry_backoff_base_seconds
        cap = self.config.loop.retry_backoff_max_seconds
        exponent = max(0, self._consecutive_retries - 1)
        return min(base * (2**exponent), cap)

    def _check_time_budget(self) -> bool:
        """Check if the time budget has been exceeded."""
        elapsed_minutes = (time.monotonic() - self._start_time) / 60
        return elapsed_minutes > self.config.loop.time_budget_minutes

    async def _check_workflow_health(self) -> dict[str, Any] | None:
        """Check CI workflow health via the WorkflowMonitor, if available.

        Returns the health check dict, or None if monitoring is unavailable
        or the check itself fails.
        """
        if not self._monitor:
            return None
        try:
            health = await self._monitor.check_health()
            return health.to_dict()
        except Exception as exc:
            self.logger.debug(f"Workflow health check failed: {exc}")
            return None

    def _write_outputs(self, status: str) -> None:
        """Write execution record, status, and reports to output directory."""
        self.output_dir.mkdir(parents=True, exist_ok=True)

        with (self.output_dir / "execution.json").open("w") as f:
            json.dump(self.execution.to_dict(), f, indent=2)

        with (self.output_dir / "status.txt").open("w") as f:
            f.write(status)

        self._publish_reports()

    def _write_transcript_data(self) -> None:
        """Write full transcript calls as a separate JSON file.

        Kept separate from execution.json so full prompts/responses aren't
        truncated.  The report generator loads this file to render the
        LLM Inference Log with complete content.
        """
        calls = self.transcript.get_calls()
        if not calls:
            return
        transcript_dir = self.output_dir / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        with (transcript_dir / "transcript-calls.json").open("w") as f:
            json.dump(calls, f, indent=2)

    def _publish_reports(self) -> None:
        """Generate visual reports from the finalized execution record.

        Always regenerates reports even if the ReportPhase already published,
        because the post-loop execution record contains complete metrics,
        actions, and final status that were unavailable during the report phase.
        Failures are logged but never block loop completion.
        """
        try:
            from engine.visualization.publisher import ReportPublisher

            reports_dir = self.output_dir / "reports"
            publisher = ReportPublisher(
                output_dir=reports_dir,
                config=self.config.reporting,
            )
            result = publisher.publish(
                self.execution.to_dict(),
                transcript_calls=self.transcript.get_calls(),
            )
            if result.success:
                self.logger.info(
                    f"Reports published: {len(result.files_generated)} files",
                    report_dir=str(reports_dir),
                )
            else:
                for err in result.errors:
                    self.logger.warn(f"Report publishing error: {err}")
        except ImportError:
            self.logger.debug("Visualization module not available — skipping reports")
        except Exception as exc:
            self.logger.warn(f"Report publishing failed (non-blocking): {exc}")


def _truncate_dict(d: dict[str, Any], max_str_len: int = 2000) -> dict[str, Any]:
    """Deep-copy a dict, truncating any string values beyond *max_str_len*.

    Prevents execution.json from ballooning when a phase dumps large file
    contents or LLM responses into findings/artifacts.
    """
    if not isinstance(d, dict):
        return d
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > max_str_len:
            out[k] = v[:max_str_len] + f"... [truncated, {len(v)} chars total]"
        elif isinstance(v, dict):
            out[k] = _truncate_dict(v, max_str_len)
        elif isinstance(v, list):
            out[k] = [
                _truncate_dict(item, max_str_len)
                if isinstance(item, dict)
                else (
                    item[:max_str_len] + "... [truncated]"
                    if isinstance(item, str) and len(item) > max_str_len
                    else item
                )
                for item in v
            ]
        else:
            out[k] = v
    return out
