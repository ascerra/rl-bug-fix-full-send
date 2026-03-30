"""Core phased pipeline engine — orchestrates the bug fix workflow.

Runs a sequential pipeline of phases (triage → implement → review → validate → report),
each executing an OODA cycle (observe → plan → act → validate → reflect).
Manages phase transitions, bounded backtracking (implement↔review), iteration caps,
time budgets, and escalation. After validate creates a PR, the engine monitors the
target repo's CI and enters a CI remediation sub-loop if failures are detected.
Developed and maintained using the Ralph Loop methodology (see README.md).
"""

from __future__ import annotations

import asyncio
import json
import os
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
    """Complete record of a pipeline execution."""

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


class PipelineEngine:
    """The phased pipeline engine.

    Executes phases in sequence, managing transitions and bounded backtracking.
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
        """Execute the full phased pipeline.

        Runs phases in PHASE_ORDER sequence. Each iteration:
        1. Check time budget and iteration cap
        2. Execute the current phase
        3. Handle the result (escalate, fail, transition, retry, or advance)

        Returns the complete ExecutionRecord with all iterations, metrics, and actions.
        """
        self._start_time = time.monotonic()
        self.logger.info(f"Starting RL Engine for issue: {self.issue_url}")
        self.logger.info(f"Target repo: {self.repo_path}")
        self.logger.info(
            f"Max iterations: {self.config.loop.max_iterations}, "
            f"Time budget: {self.config.loop.time_budget_minutes}m"
        )

        self.logger.write_progress_heading("# RL Engine Progress")
        self.logger.narrate(
            f"Starting RL Engine for {self.issue_url} "
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
                if phase_name == "validate" and self._pr_was_created(result):
                    ci_outcome = await self._run_ci_monitoring_loop(result, phase_results)
                    if ci_outcome == "escalated":
                        status = "escalated"
                        break
                    if ci_outcome == "timeout":
                        status = "timeout"
                        break
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
            f"RL Engine complete: status={status}, "
            f"{self._total_iterations} iterations, {total_min:.1f}m elapsed"
        )
        self.logger.info(
            f"RL Engine complete: status={status}, iterations={self._total_iterations}"
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

    # ------------------------------------------------------------------
    # CI monitoring and remediation sub-loop
    # ------------------------------------------------------------------

    @staticmethod
    def _pr_was_created(validate_result: PhaseResult) -> bool:
        """Check whether the validate phase successfully created a PR."""
        return bool(validate_result.artifacts.get("pr_created"))

    async def _run_ci_monitoring_loop(
        self,
        validate_result: PhaseResult,
        phase_results: list[PhaseResult],
    ) -> str:
        """Monitor CI on the PR branch and remediate failures.

        Runs independently of the main loop with its own iteration cap
        and time budget from ``CIRemediationConfig``.

        Returns:
          ``"success"`` — CI passed (or CI monitoring disabled/unavailable)
          ``"escalated"`` — CI remediation cap exceeded, escalated to human
          ``"timeout"`` — CI remediation time budget exceeded
        """
        ci_config = self.config.ci_remediation
        if not ci_config.enabled:
            self.logger.info("CI remediation disabled — skipping CI monitoring")
            return "success"

        gh_token = os.environ.get("GH_PAT", "") or os.environ.get("GITHUB_TOKEN", "")
        if not gh_token:
            self.logger.info("No GitHub token — skipping CI monitoring")
            return "success"

        pr_url = validate_result.artifacts.get("pr_url", "")
        branch_name = self._extract_branch_from_pr(validate_result)
        repo_parts = self._extract_repo_parts_from_url(pr_url)

        if not branch_name or not repo_parts:
            self.logger.info("Cannot determine branch/repo for CI monitoring — skipping")
            return "success"

        owner, repo = repo_parts

        from engine.workflow.ci_monitor import (
            CIMonitor,
            CIRemediationAttempt,
            CIRemediationHistory,
            build_ci_pr_comment,
        )

        ci_monitor = CIMonitor(
            token=gh_token,
            owner=owner,
            repo=repo,
            config=ci_config,
            logger=self.logger,
        )

        original_diff = validate_result.artifacts.get("diff", "")
        original_desc = validate_result.artifacts.get("pr_description", "")

        ci_start = time.monotonic()
        ci_time_budget_s = ci_config.time_budget_minutes * 60
        flake_reruns = 0
        ci_attempts: list[CIRemediationAttempt] = []
        last_failure_details = None

        self.logger.write_progress_heading("## CI Monitoring")
        self.logger.narrate(
            f"Monitoring CI for branch {branch_name} "
            f"(max {ci_config.max_iterations} remediation iterations, "
            f"{ci_config.time_budget_minutes}m budget)"
        )

        pr_number = self._extract_pr_number_from_url(pr_url)
        outcome = "success"

        for ci_iter in range(1, ci_config.max_iterations + 1):
            ci_elapsed = time.monotonic() - ci_start
            if ci_elapsed > ci_time_budget_s:
                self.logger.narrate("CI remediation time budget exceeded.")
                self._record_escalation(
                    f"CI remediation time budget exceeded ({ci_config.time_budget_minutes}m)",
                    phase_results,
                )
                outcome = "timeout"
                break

            if self._check_time_budget():
                self.logger.narrate("Main loop time budget exceeded during CI monitoring.")
                self._record_escalation(
                    "Main time budget exceeded during CI monitoring",
                    phase_results,
                )
                outcome = "timeout"
                break

            self.logger.narrate(f"Polling CI status (attempt {ci_iter})...")
            ci_result = await ci_monitor.poll_ci_status(branch_name)

            self.tracer.record_action(
                action_type="ci_poll",
                description=f"CI poll iteration {ci_iter}: {ci_result.overall_state}",
                input_context={"ref": branch_name, "iteration": ci_iter},
                output_success=ci_result.passed,
            )

            if ci_result.passed:
                self.logger.narrate("CI passed. Continuing to report phase.")
                outcome = "success"
                break

            if not ci_result.completed:
                self.logger.narrate(
                    f"CI poll timed out (not all checks completed). "
                    f"State: {ci_result.overall_state}"
                )
                self._record_escalation(
                    f"CI poll timeout — checks did not complete within "
                    f"{ci_config.ci_poll_timeout_minutes}m",
                    phase_results,
                )
                outcome = "escalated"
                break

            category = ci_monitor.categorize_failure(ci_result)
            failure_details = ci_monitor.extract_failure_details(ci_result, category)
            last_failure_details = failure_details

            self.logger.narrate(f"CI failed: {category.value} — {failure_details.summary[:200]}")

            action = ci_config.failure_categories.get(
                category.value,
                failure_details.recommended_action,
            )

            if action == "escalate":
                self.logger.narrate(f"Escalating: {category.value} failure.")
                self._record_escalation(
                    f"CI failure escalated: {category.value} — {failure_details.summary[:200]}",
                    phase_results,
                )
                outcome = "escalated"
                break

            if action == "rerun":
                if flake_reruns >= ci_config.max_flake_reruns:
                    self.logger.narrate(f"Max flake reruns ({ci_config.max_flake_reruns}) reached.")
                    self._record_escalation(
                        f"CI flake rerun limit reached ({ci_config.max_flake_reruns})",
                        phase_results,
                    )
                    outcome = "escalated"
                    break

                flake_reruns += 1
                for run_id in ci_result.workflow_run_ids[:1]:
                    self.logger.narrate(
                        f"Triggering CI rerun for workflow {run_id} "
                        f"(flake rerun {flake_reruns}/{ci_config.max_flake_reruns})"
                    )
                    await ci_monitor.trigger_rerun(run_id)
                continue

            remediation_result = await self._execute_ci_remediation(
                failure_details=failure_details,
                category=category,
                branch_name=branch_name,
                original_diff=original_diff,
                original_desc=original_desc,
                ci_iter=ci_iter,
                phase_results=phase_results,
            )
            phase_results.append(remediation_result)

            ci_attempts.append(
                CIRemediationAttempt(
                    iteration=ci_iter,
                    category=category.value,
                    summary=failure_details.summary[:300],
                    failing_checks=failure_details.failing_checks[:10],
                    failing_tests=failure_details.failing_tests[:20],
                    action_taken=remediation_result.findings.get("action", ""),
                    files_changed=remediation_result.artifacts.get("files_changed", []),
                    fix_pushed=remediation_result.artifacts.get("pushed", False),
                    success=remediation_result.success,
                )
            )

            if not remediation_result.success:
                self.logger.narrate(
                    "CI remediation attempt failed — "
                    f"will retry ({ci_iter}/{ci_config.max_iterations})"
                )

            needs_rerun = remediation_result.artifacts.get("needs_rerun", False)
            if needs_rerun:
                if flake_reruns >= ci_config.max_flake_reruns:
                    self.logger.narrate("Max flake reruns reached after remediation.")
                    self._record_escalation(
                        "CI flake rerun limit reached after remediation",
                        phase_results,
                    )
                    outcome = "escalated"
                    break
                flake_reruns += 1
                for run_id in ci_result.workflow_run_ids[:1]:
                    await ci_monitor.trigger_rerun(run_id)
        else:
            self.logger.narrate(
                f"CI remediation iteration cap ({ci_config.max_iterations}) reached."
            )
            self._record_escalation(
                f"CI remediation iteration cap reached ({ci_config.max_iterations})",
                phase_results,
            )
            outcome = "escalated"

        ci_elapsed_total = time.monotonic() - ci_start

        if ci_attempts or outcome != "success":
            escalation_reason = ""
            if outcome in ("escalated", "timeout"):
                for action in reversed(self.tracer.get_actions_as_dicts()):
                    if action.get("type") == "escalation":
                        escalation_reason = action.get("description", "")[:300]
                        break

            history = CIRemediationHistory(
                outcome=outcome,
                total_iterations=len(ci_attempts),
                flake_reruns=flake_reruns,
                elapsed_seconds=ci_elapsed_total,
                attempts=ci_attempts,
                final_failure=last_failure_details,
                escalation_reason=escalation_reason,
            )

            await self._post_ci_pr_comment(
                owner=owner,
                repo=repo,
                pr_number=pr_number,
                history=history,
                gh_token=gh_token,
                build_comment_fn=build_ci_pr_comment,
            )

        return outcome

    async def _execute_ci_remediation(
        self,
        failure_details: Any,
        category: Any,
        branch_name: str,
        original_diff: str,
        original_desc: str,
        ci_iter: int,
        phase_results: list[PhaseResult],
    ) -> PhaseResult:
        """Execute the CI remediation phase with failure context."""
        phase_cls = self._phase_registry.get("ci_remediate")
        if phase_cls is None:
            self.logger.warn("No ci_remediate phase registered — cannot remediate")
            return PhaseResult(
                phase="ci_remediate",
                success=False,
                should_continue=False,
                findings={"skipped": True, "reason": "Phase not registered"},
            )

        self._total_iterations += 1
        self.logger.set_iteration(self._total_iterations)
        self.tracer.set_iteration(self._total_iterations)
        self.metrics.record_iteration("ci_remediate")

        self.logger.write_progress_heading(
            f"## Iteration {self._total_iterations} — ci_remediate (CI iter {ci_iter})"
        )

        issue_data: dict[str, Any] = {
            "url": self.issue_url,
            "ci_failure_details": failure_details.to_dict(),
            "ci_failure_category": str(category.value),
            "branch_name": branch_name,
            "original_diff": original_diff,
            "original_description": original_desc,
            "remediation_iteration": ci_iter,
        }

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

            phase = phase_cls(
                llm=self.llm,
                logger=self.logger,
                tracer=self.tracer,
                repo_path=self.repo_path,
                issue_data=issue_data,
                prior_phase_results=phase_results,
                tool_executor=tool_executor,
                config=self.config,
                metrics=self.metrics,
                transcript=self.transcript,
            )

            iteration_started = datetime.now(UTC).isoformat()
            phase_start = time.monotonic()

            result = await phase.execute()
            result.phase = "ci_remediate"

            phase_duration_ms = (time.monotonic() - phase_start) * 1000
            self.metrics.record_phase_time("ci_remediate", phase_duration_ms)

            self.execution.iterations.append(
                {
                    "number": self._total_iterations,
                    "phase": "ci_remediate",
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

            elapsed_min = (time.monotonic() - self._start_time) / 60
            self.logger.narrate(
                f"CI remediation "
                f"{'succeeded' if result.success else 'failed'} "
                f"({phase_duration_ms:.0f}ms, {elapsed_min:.1f}m elapsed)"
            )

            return result

        except Exception as exc:
            self.logger.error(f"CI remediation phase raised: {exc}")
            self.metrics.record_error(str(exc))
            return PhaseResult(
                phase="ci_remediate",
                success=False,
                should_continue=False,
                escalate=True,
                escalation_reason=f"CI remediation raised: {exc}",
            )

    @staticmethod
    def _extract_branch_from_pr(validate_result: PhaseResult) -> str:
        """Extract the branch name from validate phase artifacts.

        The validate phase records git actions; we look for the branch
        checkout action or fall back to scanning the PR URL.
        """
        pr_url = validate_result.artifacts.get("pr_url", "")
        if not pr_url:
            return ""
        import re

        branch_match = re.search(r"rl/fix-[\w-]+", pr_url)
        if branch_match:
            return branch_match.group(0)

        for key in ("branch_name", "branch"):
            val = validate_result.artifacts.get(key, "")
            if val:
                return val

        return ""

    @staticmethod
    def _extract_pr_number_from_url(url: str) -> int:
        """Extract the PR number from a GitHub PR URL.

        Returns 0 if the URL does not contain a parseable PR number.
        """
        if "/pull/" not in url:
            return 0
        try:
            segment = url.split("/pull/")[1].split("/")[0].split("?")[0]
            return int(segment)
        except (IndexError, ValueError):
            return 0

    async def _post_ci_pr_comment(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        history: Any,
        gh_token: str,
        build_comment_fn: Any,
    ) -> None:
        """Post the CI remediation summary as a PR comment.

        Failures are logged but never block loop completion.
        """
        if not pr_number:
            self.logger.info("No PR number — skipping CI comment")
            return

        comment_body = build_comment_fn(history)

        try:
            from engine.integrations.github import GitHubAdapter

            adapter = GitHubAdapter(owner=owner, repo=repo, token=gh_token)
            result = await adapter.post_comment(pr_number, comment_body)
            if result.get("success"):
                self.logger.info("Posted CI remediation comment on PR", pr_number=pr_number)
                self.logger.narrate(f"Posted CI remediation summary as comment on PR #{pr_number}.")
            else:
                self.logger.warn(
                    f"Failed to post CI comment: {result.get('error', 'unknown')}",
                    pr_number=pr_number,
                )
        except Exception as exc:
            self.logger.warn(f"CI comment posting failed (non-blocking): {exc}")

    @staticmethod
    def _extract_repo_parts_from_url(url: str) -> tuple[str, str] | None:
        """Extract (owner, repo) from a GitHub URL."""
        if "github.com/" not in url:
            return None
        try:
            parts = url.split("github.com/")[1].split("/")
            if len(parts) >= 2:
                return (parts[0], parts[1])
        except (IndexError, ValueError):
            pass
        return None

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
