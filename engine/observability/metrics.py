"""Simple metrics collection for Ralph Loop execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class LoopMetrics:
    """Counters and gauges for a single loop execution."""

    total_iterations: int = 0
    total_llm_calls: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0
    total_tool_executions: int = 0
    time_per_phase_ms: dict[str, float] = field(default_factory=dict)
    phase_iteration_counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def record_iteration(self, phase: str) -> None:
        self.total_iterations += 1
        self.phase_iteration_counts[phase] = self.phase_iteration_counts.get(phase, 0) + 1

    def record_llm_call(self, tokens_in: int, tokens_out: int) -> None:
        self.total_llm_calls += 1
        self.total_tokens_in += tokens_in
        self.total_tokens_out += tokens_out

    def record_phase_time(self, phase: str, duration_ms: float) -> None:
        self.time_per_phase_ms[phase] = self.time_per_phase_ms.get(phase, 0) + duration_ms

    def record_tool_execution(self) -> None:
        self.total_tool_executions += 1

    def record_error(self, error: str) -> None:
        self.errors.append(error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_iterations": self.total_iterations,
            "total_llm_calls": self.total_llm_calls,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "total_tool_executions": self.total_tool_executions,
            "time_per_phase_ms": self.time_per_phase_ms,
            "phase_iteration_counts": self.phase_iteration_counts,
            "errors": self.errors,
        }
