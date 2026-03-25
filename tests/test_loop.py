"""Tests for the Ralph Loop orchestrator — phase dispatch, transitions, escalation.

Phase 5.2: Comprehensive loop behavior testing covering iteration cap enforcement,
time budget enforcement, escalation behavior, and phase validation independence.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import pytest

from engine.config import EngineConfig, LoopConfig
from engine.integrations.llm import MockProvider
from engine.loop import PHASE_ORDER, ExecutionRecord, RalphLoop
from engine.phases.base import (
    IMPLEMENT_TOOLS,
    PHASE_TOOL_SETS,
    REPORT_TOOLS,
    REVIEW_TOOLS,
    TRIAGE_TOOLS,
    VALIDATE_TOOLS,
    Phase,
    PhaseResult,
)

# ------------------------------------------------------------------
# Stub phase helpers
# ------------------------------------------------------------------


def _success_result(phase: str, next_phase: str = "") -> PhaseResult:
    return PhaseResult(phase=phase, success=True, should_continue=True, next_phase=next_phase)


def _make_stub(phase_name: str, result: PhaseResult) -> type[Phase]:
    """Create a Phase subclass that always returns the given result."""

    class _Stub(Phase):
        name = phase_name
        allowed_tools: ClassVar[list[str]] = []

        async def observe(self) -> dict[str, Any]:
            return {}

        async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
            return PhaseResult(
                phase=result.phase or phase_name,
                success=result.success,
                should_continue=result.should_continue,
                next_phase=result.next_phase,
                escalate=result.escalate,
                escalation_reason=result.escalation_reason,
                findings=dict(result.findings),
                artifacts=dict(result.artifacts),
            )

    return _Stub


def _make_cycling_stub(phase_name: str, results: list[PhaseResult]) -> type[Phase]:
    """Create a Phase subclass that cycles through results on successive calls."""
    counter = {"n": 0}

    class _CyclingStub(Phase):
        name = phase_name
        allowed_tools: ClassVar[list[str]] = []

        async def observe(self) -> dict[str, Any]:
            return {}

        async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
            idx = min(counter["n"], len(results) - 1)
            counter["n"] += 1
            r = results[idx]
            return PhaseResult(
                phase=r.phase or phase_name,
                success=r.success,
                should_continue=r.should_continue,
                next_phase=r.next_phase,
                escalate=r.escalate,
                escalation_reason=r.escalation_reason,
                findings=dict(r.findings),
                artifacts=dict(r.artifacts),
            )

    return _CyclingStub


def _all_success_registry() -> dict[str, type[Phase]]:
    """Create a registry where every phase returns success and advances normally."""
    registry: dict[str, type[Phase]] = {}
    for i, name in enumerate(PHASE_ORDER):
        next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
        registry[name] = _make_stub(name, _success_result(name, next_p))
    return registry


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    out = tmp_path / "output"
    out.mkdir()
    return out


@pytest.fixture()
def config() -> EngineConfig:
    return EngineConfig()


@pytest.fixture()
def mock_llm() -> MockProvider:
    return MockProvider()


# ------------------------------------------------------------------
# Core lifecycle tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_runs_all_phases_in_order(tmp_repo, output_dir, config, mock_llm):
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=_all_success_registry(),
    )
    execution = await loop.run()

    assert execution.result["status"] == "success"
    assert execution.result["total_iterations"] == 5
    phases_run = [it["phase"] for it in execution.iterations]
    assert phases_run == PHASE_ORDER


@pytest.mark.asyncio
async def test_loop_skips_unregistered_phases(tmp_repo, output_dir, config, mock_llm):
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
    )
    execution = await loop.run()

    assert execution.result["status"] == "success"
    assert execution.result["total_iterations"] == 5
    phases_run = [it["phase"] for it in execution.iterations]
    assert phases_run == PHASE_ORDER


# ------------------------------------------------------------------
# Iteration cap and time budget
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_iteration_cap(tmp_repo, output_dir, mock_llm):
    cfg = EngineConfig(loop=LoopConfig(max_iterations=2))
    loop = RalphLoop(
        config=cfg,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=_all_success_registry(),
    )
    execution = await loop.run()

    assert execution.result["status"] == "escalated"
    assert execution.result["total_iterations"] == 2


@pytest.mark.asyncio
async def test_loop_time_budget_exceeded(tmp_repo, output_dir, mock_llm):
    cfg = EngineConfig(loop=LoopConfig(time_budget_minutes=0))
    loop = RalphLoop(
        config=cfg,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=_all_success_registry(),
    )
    execution = await loop.run()

    assert execution.result["status"] == "timeout"


# ------------------------------------------------------------------
# Escalation and failure
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_escalation_from_phase(tmp_repo, output_dir, config, mock_llm):
    registry = {
        "triage": _make_stub(
            "triage",
            PhaseResult(
                phase="triage",
                escalate=True,
                escalation_reason="Cannot classify issue",
            ),
        ),
    }
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=registry,
    )
    execution = await loop.run()

    assert execution.result["status"] == "escalated"
    assert execution.result["total_iterations"] == 1
    escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
    assert len(escalation_actions) >= 1


@pytest.mark.asyncio
async def test_loop_failure_stops(tmp_repo, output_dir, config, mock_llm):
    registry = dict(_all_success_registry())
    registry["implement"] = _make_stub(
        "implement",
        PhaseResult(phase="implement", success=False, should_continue=False),
    )
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=registry,
    )
    execution = await loop.run()

    assert execution.result["status"] == "failure"
    phases_run = [it["phase"] for it in execution.iterations]
    assert "triage" in phases_run
    assert "implement" in phases_run
    assert "review" not in phases_run


# ------------------------------------------------------------------
# Phase transitions: review rejection backtrack
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_review_rejection_backtracks(tmp_repo, output_dir, mock_llm):
    cfg = EngineConfig(loop=LoopConfig(max_iterations=15))
    registry = dict(_all_success_registry())
    registry["review"] = _make_cycling_stub(
        "review",
        [
            PhaseResult(
                phase="review",
                success=False,
                should_continue=True,
                next_phase="implement",
            ),
            _success_result("review", "validate"),
        ],
    )
    loop = RalphLoop(
        config=cfg,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=registry,
    )
    execution = await loop.run()

    assert execution.result["status"] == "success"
    phases_run = [it["phase"] for it in execution.iterations]
    assert phases_run.count("implement") == 2
    assert phases_run.count("review") == 2
    assert execution.result["total_iterations"] == 7


@pytest.mark.asyncio
async def test_loop_review_rejection_escalates_after_threshold(tmp_repo, output_dir, mock_llm):
    cfg = EngineConfig(loop=LoopConfig(max_iterations=20, escalation_on_review_block_after=2))
    registry = dict(_all_success_registry())
    registry["review"] = _make_stub(
        "review",
        PhaseResult(
            phase="review",
            success=False,
            should_continue=True,
            next_phase="implement",
        ),
    )
    loop = RalphLoop(
        config=cfg,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=registry,
    )
    execution = await loop.run()

    assert execution.result["status"] == "escalated"


# ------------------------------------------------------------------
# Soft failure retry
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_retry_on_soft_failure(tmp_repo, output_dir, mock_llm):
    """Phase fails with should_continue=True but no next_phase → retries same phase."""
    cfg = EngineConfig(loop=LoopConfig(max_iterations=10))
    registry: dict[str, type[Phase]] = {}
    registry["triage"] = _make_cycling_stub(
        "triage",
        [
            PhaseResult(phase="triage", success=False, should_continue=True),
            _success_result("triage", "implement"),
        ],
    )
    for i, name in enumerate(PHASE_ORDER[1:], 1):
        next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
        registry[name] = _make_stub(name, _success_result(name, next_p))

    loop = RalphLoop(
        config=cfg,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=registry,
    )
    execution = await loop.run()

    assert execution.result["status"] == "success"
    phases_run = [it["phase"] for it in execution.iterations]
    assert phases_run.count("triage") == 2
    assert execution.result["total_iterations"] == 6


# ------------------------------------------------------------------
# Phase exception handling
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_handles_phase_exception(tmp_repo, output_dir, config, mock_llm):
    """Phase that raises an exception is caught and escalated."""

    class _BrokenPhase(Phase):
        name = "triage"
        allowed_tools: ClassVar[list[str]] = []

        async def observe(self) -> dict[str, Any]:
            raise RuntimeError("Boom")

        async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
            return _success_result("triage")

    registry: dict[str, type[Phase]] = {"triage": _BrokenPhase}
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=registry,
    )
    execution = await loop.run()

    assert execution.result["status"] == "escalated"


# ------------------------------------------------------------------
# Output file verification
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_writes_execution_json(tmp_repo, output_dir, config, mock_llm):
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=_all_success_registry(),
    )
    await loop.run()

    execution_file = output_dir / "execution.json"
    assert execution_file.exists()
    data = json.loads(execution_file.read_text())
    assert "execution" in data
    assert data["execution"]["result"]["status"] == "success"
    assert len(data["execution"]["iterations"]) == 5


@pytest.mark.asyncio
async def test_loop_writes_status_txt(tmp_repo, output_dir, config, mock_llm):
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=_all_success_registry(),
    )
    await loop.run()

    status_file = output_dir / "status.txt"
    assert status_file.exists()
    assert status_file.read_text() == "success"


# ------------------------------------------------------------------
# Metrics and records
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_metrics_populated(tmp_repo, output_dir, config, mock_llm):
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=_all_success_registry(),
    )
    execution = await loop.run()

    metrics = execution.metrics
    assert metrics["total_iterations"] == 5
    assert "triage" in metrics["phase_iteration_counts"]
    assert "implement" in metrics["phase_iteration_counts"]
    assert "report" in metrics["phase_iteration_counts"]
    for phase in PHASE_ORDER:
        assert phase in metrics["time_per_phase_ms"]
        assert metrics["time_per_phase_ms"][phase] > 0


@pytest.mark.asyncio
async def test_iterations_recorded_with_timing(tmp_repo, output_dir, config, mock_llm):
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
        phase_registry=_all_success_registry(),
    )
    execution = await loop.run()

    for it in execution.iterations:
        assert "number" in it
        assert "phase" in it
        assert "started_at" in it
        assert "completed_at" in it
        assert "duration_ms" in it
        assert it["duration_ms"] >= 0
        assert "result" in it


def test_execution_record_to_dict():
    record = ExecutionRecord(
        id="test-id",
        trigger={"type": "github_issue", "source_url": "https://example.com"},
        target={"repo_path": "/tmp/test"},
    )
    d = record.to_dict()

    assert d["execution"]["id"] == "test-id"
    assert d["execution"]["trigger"]["type"] == "github_issue"
    assert d["execution"]["target"]["repo_path"] == "/tmp/test"
    assert d["execution"]["iterations"] == []


# ------------------------------------------------------------------
# register_phase API
# ------------------------------------------------------------------


def test_register_phase(tmp_repo, output_dir, config, mock_llm):
    loop = RalphLoop(
        config=config,
        llm=mock_llm,
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(tmp_repo),
        output_dir=str(output_dir),
    )
    stub = _make_stub("triage", _success_result("triage", "implement"))
    loop.register_phase("triage", stub)

    assert "triage" in loop._phase_registry
    assert loop._phase_registry["triage"] is stub


# ====================================================================
# Phase 5.2: Comprehensive Loop Behavior Testing
# ====================================================================
#
# Four areas:
#   1. Iteration cap enforcement (boundary conditions, retries, backtrack budget)
#   2. Time budget enforcement (monkeypatched time, mid-loop expiry)
#   3. Escalation behavior (context recording, all escalation paths, status values)
#   4. Phase validation independence (tool filtering, prior results, per-phase executors)
# ====================================================================


# ------------------------------------------------------------------
# Spy phase helper — records what the phase was instantiated with
# ------------------------------------------------------------------

_spy_log: list[dict[str, Any]] = []


def _make_spy_stub(phase_name: str, result: PhaseResult) -> type[Phase]:
    """Phase stub that records its constructor args to the module-level _spy_log."""

    class _SpyStub(Phase):
        name = phase_name
        allowed_tools: ClassVar[list[str]] = PHASE_TOOL_SETS.get(phase_name, [])

        def __init__(self, **kwargs: Any):
            super().__init__(**kwargs)
            te = kwargs.get("tool_executor")
            _spy_log.append(
                {
                    "phase": phase_name,
                    "tool_executor_id": id(te) if te else None,
                    "available_tools": te.available_tools if te else [],
                    "prior_results_count": len(kwargs.get("prior_phase_results", []) or []),
                    "config_type": type(kwargs.get("config")).__name__,
                }
            )

        async def observe(self) -> dict[str, Any]:
            return {}

        async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
            return PhaseResult(
                phase=result.phase or phase_name,
                success=result.success,
                should_continue=result.should_continue,
                next_phase=result.next_phase,
                escalate=result.escalate,
                escalation_reason=result.escalation_reason,
                findings=dict(result.findings),
                artifacts=dict(result.artifacts),
            )

    return _SpyStub


def _spy_success_registry() -> dict[str, type[Phase]]:
    """Registry with spy stubs that record constructor args."""
    registry: dict[str, type[Phase]] = {}
    for i, name in enumerate(PHASE_ORDER):
        next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
        registry[name] = _make_spy_stub(name, _success_result(name, next_p))
    return registry


@pytest.fixture(autouse=False)
def clear_spy_log():
    """Clear the module-level spy log before each test that uses it."""
    _spy_log.clear()
    yield
    _spy_log.clear()


# ==================================================================
# 1. ITERATION CAP ENFORCEMENT (boundary conditions, budget consumption)
# ==================================================================


class TestIterationCapEnforcement:
    """Verify the loop strictly enforces iteration caps under various conditions."""

    @pytest.mark.asyncio
    async def test_iteration_cap_exact_boundary(self, tmp_repo, output_dir, mock_llm):
        """When cap equals number of phases, all phases run and loop succeeds."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=5))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        assert execution.result["status"] == "success"
        assert execution.result["total_iterations"] == 5

    @pytest.mark.asyncio
    async def test_iteration_cap_one(self, tmp_repo, output_dir, mock_llm):
        """Cap=1 executes only the first phase, then escalates."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=1))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        assert execution.result["total_iterations"] == 1
        phases_run = [it["phase"] for it in execution.iterations]
        assert phases_run == ["triage"]

    @pytest.mark.asyncio
    async def test_iteration_cap_with_retries_consuming_budget(
        self, tmp_repo, output_dir, mock_llm
    ):
        """Soft retries consume from the iteration budget; cap hit before completion."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=3))
        registry: dict[str, type[Phase]] = {}
        registry["triage"] = _make_cycling_stub(
            "triage",
            [
                PhaseResult(phase="triage", success=False, should_continue=True),
                PhaseResult(phase="triage", success=False, should_continue=True),
                _success_result("triage", "implement"),
            ],
        )
        for i, name in enumerate(PHASE_ORDER[1:], 1):
            next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
            registry[name] = _make_stub(name, _success_result(name, next_p))

        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        assert execution.result["total_iterations"] == 3
        phases_run = [it["phase"] for it in execution.iterations]
        assert phases_run == ["triage", "triage", "triage"]

    @pytest.mark.asyncio
    async def test_iteration_cap_with_backtrack_consuming_budget(
        self, tmp_repo, output_dir, mock_llm
    ):
        """Review→implement backtrack consumes iterations; cap hit mid-backtrack."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=4, escalation_on_review_block_after=10))
        registry = dict(_all_success_registry())
        registry["review"] = _make_stub(
            "review",
            PhaseResult(
                phase="review",
                success=False,
                should_continue=True,
                next_phase="implement",
            ),
        )
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        assert execution.result["total_iterations"] == 4
        phases_run = [it["phase"] for it in execution.iterations]
        assert "triage" in phases_run
        assert "implement" in phases_run
        assert "review" in phases_run

    @pytest.mark.asyncio
    async def test_iteration_count_monotonically_increases(self, tmp_repo, output_dir, mock_llm):
        """Iteration numbers in execution record always increase by 1."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=10))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        numbers = [it["number"] for it in execution.iterations]
        assert numbers == list(range(1, len(numbers) + 1))

    @pytest.mark.asyncio
    async def test_iteration_cap_zero_immediately_escalates(self, tmp_repo, output_dir, mock_llm):
        """Cap=0 means no iterations are executed — immediately escalated."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=0))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        assert execution.result["total_iterations"] == 0
        assert len(execution.iterations) == 0


# ==================================================================
# 2. TIME BUDGET ENFORCEMENT (monkeypatched time, mid-loop expiry)
# ==================================================================


class TestTimeBudgetEnforcement:
    """Verify the loop correctly checks and enforces time budgets."""

    @pytest.mark.asyncio
    async def test_time_budget_zero_immediately_times_out(self, tmp_repo, output_dir, mock_llm):
        """Budget=0 means the very first check finds time exceeded."""
        cfg = EngineConfig(loop=LoopConfig(time_budget_minutes=0))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        assert execution.result["status"] == "timeout"

    @pytest.mark.asyncio
    async def test_time_budget_expires_mid_loop(self, tmp_repo, output_dir, mock_llm):
        """Time budget checked between iterations — loop stops mid-way."""
        cfg = EngineConfig(loop=LoopConfig(time_budget_minutes=1, max_iterations=20))

        call_count = {"n": 0}
        real_monotonic = time.monotonic

        def advancing_monotonic():
            call_count["n"] += 1
            return real_monotonic() + (call_count["n"] * 40)

        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )

        with patch("engine.loop.time.monotonic", side_effect=advancing_monotonic):
            execution = await loop.run()

        assert execution.result["status"] == "timeout"
        assert execution.result["total_iterations"] < 5

    @pytest.mark.asyncio
    async def test_time_budget_status_is_timeout_not_escalated(
        self, tmp_repo, output_dir, mock_llm
    ):
        """Time budget produces status='timeout', distinct from iteration cap's 'escalated'."""
        cfg_time = EngineConfig(loop=LoopConfig(time_budget_minutes=0))
        cfg_iter = EngineConfig(loop=LoopConfig(max_iterations=0))

        loop_time = RalphLoop(
            config=cfg_time,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        loop_iter = RalphLoop(
            config=cfg_iter,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )

        exec_time = await loop_time.run()
        exec_iter = await loop_iter.run()

        assert exec_time.result["status"] == "timeout"
        assert exec_iter.result["status"] == "escalated"
        assert exec_time.result["status"] != exec_iter.result["status"]

    @pytest.mark.asyncio
    async def test_time_budget_escalation_records_context(self, tmp_repo, output_dir, mock_llm):
        """Time budget escalation produces a tracer action with timing info."""
        cfg = EngineConfig(loop=LoopConfig(time_budget_minutes=0))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
        assert len(escalation_actions) >= 1
        esc = escalation_actions[0]
        assert "Time budget exceeded" in esc["input"]["description"]
        assert "elapsed_minutes" in esc["input"]["context"]

    @pytest.mark.asyncio
    async def test_time_budget_checked_before_each_iteration(self, tmp_repo, output_dir, mock_llm):
        """Time budget is checked before every phase execution, not just at start."""
        cfg = EngineConfig(loop=LoopConfig(time_budget_minutes=5, max_iterations=20))

        mono_base = time.monotonic()
        calls = {"n": 0}

        def progressive_monotonic():
            calls["n"] += 1
            if calls["n"] <= 3:
                return mono_base + 60
            return mono_base + 600

        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )

        with patch("engine.loop.time.monotonic", side_effect=progressive_monotonic):
            execution = await loop.run()

        assert execution.result["status"] == "timeout"
        assert execution.result["total_iterations"] > 0
        assert execution.result["total_iterations"] < 5


# ==================================================================
# 3. ESCALATION BEHAVIOR (context, all paths, status values)
# ==================================================================


class TestEscalationBehavior:
    """Verify all escalation paths record correct context and produce correct status."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("phase_name", ["triage", "implement", "review", "validate"])
    async def test_escalation_from_each_phase(
        self, phase_name, tmp_repo, output_dir, config, mock_llm
    ):
        """Any phase can trigger escalation; verify it produces the right status."""
        registry = dict(_all_success_registry())
        registry[phase_name] = _make_stub(
            phase_name,
            PhaseResult(
                phase=phase_name,
                escalate=True,
                escalation_reason=f"Test escalation from {phase_name}",
            ),
        )
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        phases_run = [it["phase"] for it in execution.iterations]
        assert phase_name in phases_run

    @pytest.mark.asyncio
    async def test_escalation_action_record_structure(self, tmp_repo, output_dir, mock_llm):
        """Escalation action records contain all required context fields."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=2))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
        assert len(escalation_actions) >= 1

        esc = escalation_actions[0]
        assert "input" in esc
        assert "description" in esc["input"]
        assert "context" in esc["input"]
        ctx = esc["input"]["context"]
        assert "reason" in ctx
        assert "total_iterations" in ctx
        assert "elapsed_minutes" in ctx
        assert "phases_completed" in ctx
        assert isinstance(ctx["phases_completed"], list)
        assert isinstance(ctx["elapsed_minutes"], float)

    @pytest.mark.asyncio
    async def test_escalation_records_phases_completed(self, tmp_repo, output_dir, mock_llm):
        """Escalation context correctly lists which phases completed before escalation."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=10))
        registry = dict(_all_success_registry())
        registry["review"] = _make_stub(
            "review",
            PhaseResult(
                phase="review",
                escalate=True,
                escalation_reason="blocking issue found",
            ),
        )
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )
        execution = await loop.run()

        escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
        ctx = escalation_actions[0]["input"]["context"]
        assert "triage" in ctx["phases_completed"]
        assert "implement" in ctx["phases_completed"]
        assert "review" not in ctx["phases_completed"]

    @pytest.mark.asyncio
    async def test_escalation_records_elapsed_minutes(self, tmp_repo, output_dir, mock_llm):
        """Elapsed minutes in escalation context is a non-negative float."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=1))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
        elapsed = escalation_actions[0]["input"]["context"]["elapsed_minutes"]
        assert isinstance(elapsed, float)
        assert elapsed >= 0

    @pytest.mark.asyncio
    async def test_all_escalation_status_values_distinct(self, tmp_repo, output_dir, mock_llm):
        """The four terminal statuses (success, failure, escalated, timeout) are distinct."""
        results: dict[str, str] = {}

        cfg_success = EngineConfig()
        loop_s = RalphLoop(
            config=cfg_success,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        results["success"] = (await loop_s.run()).result["status"]

        cfg_failure = EngineConfig()
        registry_f = dict(_all_success_registry())
        registry_f["triage"] = _make_stub(
            "triage",
            PhaseResult(phase="triage", success=False, should_continue=False),
        )
        loop_f = RalphLoop(
            config=cfg_failure,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry_f,
        )
        results["failure"] = (await loop_f.run()).result["status"]

        cfg_escalated = EngineConfig(loop=LoopConfig(max_iterations=0))
        loop_e = RalphLoop(
            config=cfg_escalated,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        results["escalated"] = (await loop_e.run()).result["status"]

        cfg_timeout = EngineConfig(loop=LoopConfig(time_budget_minutes=0))
        loop_t = RalphLoop(
            config=cfg_timeout,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        results["timeout"] = (await loop_t.run()).result["status"]

        assert results["success"] == "success"
        assert results["failure"] == "failure"
        assert results["escalated"] == "escalated"
        assert results["timeout"] == "timeout"
        assert len(set(results.values())) == 4

    @pytest.mark.asyncio
    async def test_review_block_escalation_includes_rejection_count(
        self, tmp_repo, output_dir, mock_llm
    ):
        """Review block escalation mentions the number of rejections in its reason."""
        threshold = 2
        cfg = EngineConfig(
            loop=LoopConfig(max_iterations=20, escalation_on_review_block_after=threshold)
        )
        registry = dict(_all_success_registry())
        registry["review"] = _make_stub(
            "review",
            PhaseResult(
                phase="review",
                success=False,
                should_continue=True,
                next_phase="implement",
            ),
        )
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
        assert any(str(threshold) in a["input"]["description"] for a in escalation_actions)

    @pytest.mark.asyncio
    async def test_phase_exception_escalation_includes_error_message(
        self, tmp_repo, output_dir, config, mock_llm
    ):
        """When a phase raises an exception, the escalation reason includes the error."""

        class _ExplodingPhase(Phase):
            name = "implement"
            allowed_tools: ClassVar[list[str]] = []

            async def observe(self) -> dict[str, Any]:
                raise ValueError("database connection lost")

            async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
                return {}

            async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
                return {}

            async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
                return {}

            async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
                return _success_result("implement")

        registry = dict(_all_success_registry())
        registry["implement"] = _ExplodingPhase
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        phases = execution.result["phase_results"]
        impl_result = next(p for p in phases if p["phase"] == "implement")
        assert impl_result["escalate"] is True

    @pytest.mark.asyncio
    async def test_multiple_escalation_paths_produce_single_escalation_action(
        self, tmp_repo, output_dir, mock_llm
    ):
        """Only one escalation action is recorded regardless of escalation source."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=1))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
        assert len(escalation_actions) == 1

    @pytest.mark.asyncio
    async def test_escalation_output_written_to_execution_json(
        self, tmp_repo, output_dir, mock_llm
    ):
        """Escalation result is persisted in the execution.json file."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=1))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        await loop.run()

        data = json.loads((output_dir / "execution.json").read_text())
        assert data["execution"]["result"]["status"] == "escalated"

    @pytest.mark.asyncio
    async def test_escalation_status_written_to_status_txt(self, tmp_repo, output_dir, mock_llm):
        """Status.txt reflects the escalation status."""
        cfg = EngineConfig(loop=LoopConfig(max_iterations=1))
        loop = RalphLoop(
            config=cfg,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        await loop.run()

        assert (output_dir / "status.txt").read_text() == "escalated"


# ==================================================================
# 4. PHASE VALIDATION INDEPENDENCE
# ==================================================================


class TestPhaseValidationIndependence:
    """Verify each phase gets independent tool executors, correct tool filtering,
    prior results, and fresh config — enforcing zero trust between phases.
    """

    @pytest.mark.asyncio
    async def test_each_phase_gets_own_tool_executor(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Every phase instantiation receives a distinct ToolExecutor instance."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        te_ids = [entry["tool_executor_id"] for entry in _spy_log]
        assert all(tid is not None for tid in te_ids)
        assert len(set(te_ids)) == len(te_ids), "Each phase must get a unique ToolExecutor"

    @pytest.mark.asyncio
    async def test_triage_phase_gets_triage_tools(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Triage phase receives exactly the TRIAGE_TOOLS set."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        triage_entry = next(e for e in _spy_log if e["phase"] == "triage")
        assert sorted(triage_entry["available_tools"]) == sorted(TRIAGE_TOOLS)

    @pytest.mark.asyncio
    async def test_implement_phase_gets_implement_tools(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Implement phase receives exactly the IMPLEMENT_TOOLS set."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        impl_entry = next(e for e in _spy_log if e["phase"] == "implement")
        assert sorted(impl_entry["available_tools"]) == sorted(IMPLEMENT_TOOLS)

    @pytest.mark.asyncio
    async def test_review_phase_gets_review_tools(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Review phase receives exactly the REVIEW_TOOLS set (read-only, no shell)."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        review_entry = next(e for e in _spy_log if e["phase"] == "review")
        assert sorted(review_entry["available_tools"]) == sorted(REVIEW_TOOLS)
        assert "file_write" not in review_entry["available_tools"]
        assert "shell_run" not in review_entry["available_tools"]
        assert "git_commit" not in review_entry["available_tools"]

    @pytest.mark.asyncio
    async def test_validate_phase_gets_validate_tools(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Validate phase receives exactly the VALIDATE_TOOLS set (no file_write)."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        validate_entry = next(e for e in _spy_log if e["phase"] == "validate")
        assert sorted(validate_entry["available_tools"]) == sorted(VALIDATE_TOOLS)
        assert "file_write" not in validate_entry["available_tools"]
        assert "git_commit" not in validate_entry["available_tools"]

    @pytest.mark.asyncio
    async def test_report_phase_gets_report_tools(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Report phase receives exactly the REPORT_TOOLS set (minimal read-only)."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        report_entry = next(e for e in _spy_log if e["phase"] == "report")
        assert sorted(report_entry["available_tools"]) == sorted(REPORT_TOOLS)

    @pytest.mark.asyncio
    async def test_phase_receives_accumulating_prior_results(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Each phase receives all prior phase results, accumulating as loop progresses."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        for i, entry in enumerate(_spy_log):
            assert entry["prior_results_count"] == i, (
                f"Phase {entry['phase']} (index {i}) should receive {i} prior results, "
                f"got {entry['prior_results_count']}"
            )

    @pytest.mark.asyncio
    async def test_all_phases_receive_engine_config(
        self, tmp_repo, output_dir, config, mock_llm, clear_spy_log
    ):
        """Every phase receives an EngineConfig instance."""
        loop = RalphLoop(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_spy_success_registry(),
        )
        await loop.run()

        for entry in _spy_log:
            assert entry["config_type"] == "EngineConfig"

    @pytest.mark.asyncio
    async def test_tool_restrictions_are_enforced_by_executor(
        self, tmp_repo, output_dir, config, mock_llm
    ):
        """Tool restrictions actually prevent calling blocked tools via the executor."""
        from engine.observability.logger import StructuredLogger
        from engine.observability.metrics import LoopMetrics
        from engine.observability.tracer import Tracer
        from engine.tools.executor import ToolError, ToolExecutor

        review_executor = ToolExecutor(
            repo_path=str(tmp_repo),
            logger=StructuredLogger(),
            tracer=Tracer(),
            metrics=LoopMetrics(),
            allowed_tools=REVIEW_TOOLS,
        )

        assert "file_write" not in review_executor.available_tools
        assert "shell_run" not in review_executor.available_tools
        assert "git_commit" not in review_executor.available_tools

        with pytest.raises(ToolError, match="Unknown tool"):
            await review_executor.execute("file_write", path="x.py", content="hack")

        with pytest.raises(ToolError, match="Unknown tool"):
            await review_executor.execute("shell_run", command="rm -rf /")

    @pytest.mark.asyncio
    async def test_triage_cannot_write_files_via_executor(
        self, tmp_repo, output_dir, config, mock_llm
    ):
        """Triage phase's tool set excludes file_write and git_commit."""
        from engine.observability.logger import StructuredLogger
        from engine.observability.metrics import LoopMetrics
        from engine.observability.tracer import Tracer
        from engine.tools.executor import ToolError, ToolExecutor

        triage_executor = ToolExecutor(
            repo_path=str(tmp_repo),
            logger=StructuredLogger(),
            tracer=Tracer(),
            metrics=LoopMetrics(),
            allowed_tools=TRIAGE_TOOLS,
        )

        assert "file_write" not in triage_executor.available_tools
        assert "git_commit" not in triage_executor.available_tools
        assert "file_read" in triage_executor.available_tools

        with pytest.raises(ToolError, match="Unknown tool"):
            await triage_executor.execute("file_write", path="x.py", content="code")

    @pytest.mark.asyncio
    async def test_tool_sets_match_phase_tool_sets_mapping(self):
        """PHASE_TOOL_SETS mapping is consistent with the named tool set constants."""
        assert PHASE_TOOL_SETS["triage"] == TRIAGE_TOOLS
        assert PHASE_TOOL_SETS["implement"] == IMPLEMENT_TOOLS
        assert PHASE_TOOL_SETS["review"] == REVIEW_TOOLS
        assert PHASE_TOOL_SETS["validate"] == VALIDATE_TOOLS
        assert PHASE_TOOL_SETS["report"] == REPORT_TOOLS

    @pytest.mark.asyncio
    async def test_review_phase_is_read_only(self):
        """Review tools contain no write, commit, or execution capabilities."""
        write_tools = {"file_write", "git_commit", "shell_run", "github_api"}
        assert not set(REVIEW_TOOLS) & write_tools

    @pytest.mark.asyncio
    async def test_triage_phase_is_read_only(self):
        """Triage tools contain no write or commit capabilities."""
        write_tools = {"file_write", "git_commit", "github_api"}
        assert not set(TRIAGE_TOOLS) & write_tools

    @pytest.mark.asyncio
    async def test_implement_phase_has_write_access(self):
        """Implement phase has file_write and git_commit — it needs to write code."""
        assert "file_write" in IMPLEMENT_TOOLS
        assert "git_commit" in IMPLEMENT_TOOLS

    @pytest.mark.asyncio
    async def test_validate_phase_has_github_api(self):
        """Validate phase has github_api for PR creation."""
        assert "github_api" in VALIDATE_TOOLS
        assert "file_write" not in VALIDATE_TOOLS
