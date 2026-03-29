"""Neutral observer — independent verification of agent execution.

Reconstructs the agent's execution timeline from artifacts, cross-checks
claims against evidence, and (in later phases) builds signed attestations.

Shared types used across the observer sub-modules live here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TimelineEvent:
    """A single event in the reconstructed execution timeline.

    Represents one discrete action the agent performed (LLM call, file
    operation, shell command, phase transition, etc.).
    """

    timestamp: str = ""
    event_type: str = ""
    phase: str = ""
    iteration: int = 0
    description: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "phase": self.phase,
            "iteration": self.iteration,
            "description": self.description,
            "details": self.details,
        }


@dataclass
class CrossCheckResult:
    """Result of a single cross-check verification."""

    check_name: str = ""
    passed: bool = False
    details: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "passed": self.passed,
            "details": self.details,
            "evidence": self.evidence,
        }


@dataclass
class CrossCheckReport:
    """Aggregated results of all cross-check verifications."""

    checks: list[CrossCheckResult] = field(default_factory=list)
    all_passed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "checks": [c.to_dict() for c in self.checks],
            "all_passed": self.all_passed,
        }

    def add(self, result: CrossCheckResult) -> None:
        self.checks.append(result)
        self.all_passed = all(c.passed for c in self.checks)


@dataclass
class ModelInfo:
    """Deduplicated model identity extracted from execution artifacts."""

    model: str = ""
    provider: str = ""
    temperature: float = 0.0
    total_calls: int = 0
    total_tokens_in: int = 0
    total_tokens_out: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "temperature": self.temperature,
            "total_calls": self.total_calls,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
        }
