"""Ralph Loop engine — the core iterative execution cycle.

Implements: OBSERVE → PLAN → ACT → VALIDATE → REFLECT
Manages phase transitions, iteration caps, time budgets, and escalation.
"""

from __future__ import annotations

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
from engine.phases.base import PhaseResult


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
    """

    def __init__(
        self,
        config: EngineConfig,
        llm: LLMProvider,
        issue_url: str,
        repo_path: str,
        output_dir: str = "./output",
        comparison_ref: str = "",
    ):
        self.config = config
        self.llm = llm
        self.issue_url = issue_url
        self.repo_path = repo_path
        self.output_dir = Path(output_dir)
        self.comparison_ref = comparison_ref

        self.logger = StructuredLogger(
            output_path=self.output_dir / "log.json",
        )
        self.tracer = Tracer()
        self.metrics = LoopMetrics()
        self.execution = ExecutionRecord(
            trigger={"type": "github_issue", "source_url": issue_url},
            target={"repo_path": repo_path, "comparison_ref": comparison_ref},
        )

        self._start_time: float = 0
        self._total_iterations: int = 0

    async def run(self) -> ExecutionRecord:
        """Execute the full Ralph Loop."""
        self._start_time = time.monotonic()
        self.logger.info(f"Starting Ralph Loop for issue: {self.issue_url}")
        self.logger.info(f"Target repo: {self.repo_path}")
        self.logger.info(
            f"Max iterations: {self.config.loop.max_iterations}, "
            f"Time budget: {self.config.loop.time_budget_minutes}m"
        )

        phase_results: list[PhaseResult] = []
        current_phase_idx = 0
        status = "success"

        while current_phase_idx < len(PHASE_ORDER):
            if self._check_time_budget():
                self.logger.warn("Time budget exceeded — escalating to human")
                status = "timeout"
                break

            if self._total_iterations >= self.config.loop.max_iterations:
                self.logger.warn("Iteration cap reached — escalating to human")
                status = "escalated"
                break

            phase_name = PHASE_ORDER[current_phase_idx]
            self._total_iterations += 1
            self.logger.set_iteration(self._total_iterations)
            self.tracer.set_iteration(self._total_iterations)
            self.metrics.record_iteration(phase_name)

            # TODO: Phase 1 — instantiate and execute actual phase implementations
            self.logger.info(f"Would execute phase: {phase_name} (not yet implemented)")
            result = PhaseResult(
                phase=phase_name,
                success=True,
                should_continue=True,
                next_phase=PHASE_ORDER[current_phase_idx + 1]
                if current_phase_idx + 1 < len(PHASE_ORDER)
                else "",
            )
            phase_results.append(result)

            if result.escalate:
                status = "escalated"
                break
            if not result.success and not result.should_continue:
                status = "failure"
                break

            current_phase_idx += 1

        self.execution.completed_at = datetime.now(UTC).isoformat()
        self.execution.result = {"status": status, "phase_results": len(phase_results)}
        self.execution.metrics = self.metrics.to_dict()
        self.execution.actions = self.tracer.get_actions_as_dicts()

        self._write_outputs(status)
        self.logger.info(f"Ralph Loop complete: status={status}")
        self.logger.flush()

        return self.execution

    def _check_time_budget(self) -> bool:
        elapsed_minutes = (time.monotonic() - self._start_time) / 60
        return elapsed_minutes > self.config.loop.time_budget_minutes

    def _write_outputs(self, status: str) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)

        with (self.output_dir / "execution.json").open("w") as f:
            json.dump(self.execution.to_dict(), f, indent=2)

        with (self.output_dir / "status.txt").open("w") as f:
            f.write(status)
