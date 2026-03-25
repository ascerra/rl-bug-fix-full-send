"""Action tracing — records every action taken by the engine with full provenance."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass
class ActionRecord:
    """A single action taken by the engine."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    iteration: int = 0
    phase: str = ""
    action_type: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    input_description: str = ""
    input_context: dict[str, Any] = field(default_factory=dict)
    output_success: bool = False
    output_data: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    llm_context: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "iteration": self.iteration,
            "phase": self.phase,
            "action_type": self.action_type,
            "timestamp": self.timestamp,
            "input": {
                "description": self.input_description,
                "context": self.input_context,
            },
            "output": {
                "success": self.output_success,
                "data": self.output_data,
            },
            "duration_ms": self.duration_ms,
            "llm_context": self.llm_context,
            "provenance": self.provenance,
        }


class Tracer:
    """Records actions and builds the execution trace."""

    def __init__(self):
        self._actions: list[ActionRecord] = []
        self._current_phase: str = "init"
        self._current_iteration: int = 0

    def set_phase(self, phase: str) -> None:
        self._current_phase = phase

    def set_iteration(self, iteration: int) -> None:
        self._current_iteration = iteration

    def record_action(
        self,
        action_type: str,
        description: str,
        input_context: dict[str, Any] | None = None,
        output_success: bool = True,
        output_data: dict[str, Any] | None = None,
        duration_ms: float = 0.0,
        llm_context: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> ActionRecord:
        record = ActionRecord(
            iteration=self._current_iteration,
            phase=self._current_phase,
            action_type=action_type,
            input_description=description,
            input_context=input_context or {},
            output_success=output_success,
            output_data=output_data or {},
            duration_ms=duration_ms,
            llm_context=llm_context or {},
            provenance=provenance or {},
        )
        self._actions.append(record)
        return record

    def record_llm_call(
        self,
        description: str,
        model: str,
        provider: str,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        prompt_summary: str = "",
        response_summary: str = "",
    ) -> ActionRecord:
        return self.record_action(
            action_type="llm_query",
            description=description,
            output_success=True,
            duration_ms=latency_ms,
            llm_context={
                "model": model,
                "provider": provider,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "prompt_summary": prompt_summary,
                "response_summary": response_summary[:500],
            },
        )

    def get_actions(self) -> list[ActionRecord]:
        return list(self._actions)

    def get_actions_as_dicts(self) -> list[dict[str, Any]]:
        return [a.to_dict() for a in self._actions]

    class Timer:
        """Context manager for timing actions."""

        def __init__(self):
            self.start_time: float = 0
            self.elapsed_ms: float = 0

        def __enter__(self):
            self.start_time = time.monotonic()
            return self

        def __exit__(self, *_):
            self.elapsed_ms = (time.monotonic() - self.start_time) * 1000

    @staticmethod
    def timer() -> Tracer.Timer:
        return Tracer.Timer()
