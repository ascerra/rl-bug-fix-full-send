"""Tests for observability components (logger, tracer, metrics)."""

import pytest

from engine.config import EngineConfig
from engine.integrations.llm import MockProvider
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer


def test_logger_records_entries():
    logger = StructuredLogger(execution_id="test-exec-001")
    logger.set_phase("triage")
    logger.set_iteration(1)
    logger.info("Starting triage", component="controller")

    entries = logger.get_entries()
    assert len(entries) == 1
    assert entries[0]["level"] == "INFO"
    assert entries[0]["phase"] == "triage"
    assert entries[0]["iteration"] == 1
    assert entries[0]["component"] == "controller"


def test_tracer_records_actions():
    tracer = Tracer()
    tracer.set_phase("implement")
    tracer.set_iteration(2)

    record = tracer.record_action(
        action_type="file_edit",
        description="Edit controller.go",
        output_success=True,
        output_data={"file": "controller.go"},
    )
    assert record.phase == "implement"
    assert record.iteration == 2
    assert record.action_type == "file_edit"

    actions = tracer.get_actions_as_dicts()
    assert len(actions) == 1
    assert actions[0]["phase"] == "implement"


def test_tracer_records_llm_calls():
    tracer = Tracer()
    tracer.set_phase("review")
    tracer.set_iteration(3)

    record = tracer.record_llm_call(
        description="Analyze diff for correctness",
        model="gemini-2.5-pro",
        provider="gemini",
        tokens_in=5000,
        tokens_out=1000,
        latency_ms=2500.0,
    )
    assert record.llm_context["model"] == "gemini-2.5-pro"
    assert record.llm_context["tokens_in"] == 5000


def test_tracer_timer():
    import time

    with Tracer.timer() as t:
        time.sleep(0.01)
    assert t.elapsed_ms > 5  # at least 5ms


def test_metrics_tracking():
    metrics = LoopMetrics()
    metrics.record_iteration("triage")
    metrics.record_iteration("implement")
    metrics.record_llm_call(tokens_in=100, tokens_out=50)
    metrics.record_tool_execution()
    metrics.record_phase_time("triage", 5000.0)

    data = metrics.to_dict()
    assert data["total_iterations"] == 2
    assert data["total_llm_calls"] == 1
    assert data["total_tokens_in"] == 100
    assert data["total_tool_executions"] == 1
    assert data["time_per_phase_ms"]["triage"] == 5000.0
    assert data["phase_iteration_counts"]["triage"] == 1
    assert data["phase_iteration_counts"]["implement"] == 1


# ---------------------------------------------------------------------------
# Phase.record_llm_call helper — wires tracer + metrics in one call (D2 fix)
# ---------------------------------------------------------------------------


def _bug_triage_json() -> str:
    import json

    return json.dumps(
        {
            "classification": "bug",
            "confidence": 0.9,
            "severity": "high",
            "affected_components": [],
            "reproduction": {"existing_tests": [], "can_reproduce": False},
            "injection_detected": False,
            "recommendation": "proceed",
            "reasoning": "test",
        }
    )


def _impl_json() -> str:
    import json

    return json.dumps(
        {
            "root_cause": "test",
            "fix_description": "test fix",
            "file_changes": [{"path": "pkg/main.go", "content": "package main\n"}],
            "tests_passing": True,
            "linters_passing": True,
            "confidence": 0.8,
        }
    )


def _review_json() -> str:
    import json

    return json.dumps(
        {
            "verdict": "approve",
            "findings": [],
            "scope_assessment": "bug_fix",
            "injection_detected": False,
            "confidence": 0.9,
            "summary": "LGTM",
        }
    )


def _validate_json() -> str:
    import json

    return json.dumps(
        {
            "tests_passing": True,
            "linters_passing": True,
            "diff_is_minimal": True,
            "ready_to_submit": False,
            "blocking_issues": [],
            "pr_description": "Fix bug",
            "confidence": 0.9,
        }
    )


class TestPhaseRecordLlmCallHelper:
    """Phase.record_llm_call() must update both tracer and metrics."""

    def _make_phase(self, *, responses, metrics=None, phase_cls=None):
        from engine.phases.triage import TriagePhase

        cls = phase_cls or TriagePhase
        return cls(
            llm=MockProvider(responses=responses),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "https://example.com", "title": "t", "body": "b"},
            config=EngineConfig(),
            metrics=metrics,
        )

    def test_helper_updates_both_tracer_and_metrics(self):
        metrics = LoopMetrics()
        phase = self._make_phase(responses=[_bug_triage_json()], metrics=metrics)

        record = phase.record_llm_call(
            description="test",
            model="mock",
            provider="mock",
            tokens_in=100,
            tokens_out=50,
            latency_ms=10.0,
        )

        assert record.llm_context["tokens_in"] == 100
        assert metrics.total_llm_calls == 1
        assert metrics.total_tokens_in == 100
        assert metrics.total_tokens_out == 50

    def test_helper_works_without_metrics(self):
        phase = self._make_phase(responses=[_bug_triage_json()], metrics=None)

        record = phase.record_llm_call(
            description="test",
            model="mock",
            provider="mock",
            tokens_in=200,
            tokens_out=80,
            latency_ms=5.0,
        )

        assert record.llm_context["tokens_in"] == 200
        tracer_actions = phase.tracer.get_actions()
        assert len(tracer_actions) == 1

    def test_multiple_calls_accumulate(self):
        metrics = LoopMetrics()
        phase = self._make_phase(responses=[_bug_triage_json()], metrics=metrics)

        phase.record_llm_call(
            description="call 1",
            model="m",
            provider="p",
            tokens_in=100,
            tokens_out=50,
            latency_ms=10.0,
        )
        phase.record_llm_call(
            description="call 2",
            model="m",
            provider="p",
            tokens_in=200,
            tokens_out=100,
            latency_ms=20.0,
        )

        assert metrics.total_llm_calls == 2
        assert metrics.total_tokens_in == 300
        assert metrics.total_tokens_out == 150
        assert len(phase.tracer.get_actions()) == 2

    @pytest.mark.asyncio
    async def test_triage_plan_updates_metrics(self):
        metrics = LoopMetrics()
        phase = self._make_phase(responses=[_bug_triage_json()], metrics=metrics)

        obs = await phase.observe()
        await phase.plan(obs)

        assert metrics.total_llm_calls == 1
        assert metrics.total_tokens_in > 0

    @pytest.mark.asyncio
    async def test_implement_plan_updates_metrics(self):
        from engine.phases.implement import ImplementPhase

        metrics = LoopMetrics()
        phase = self._make_phase(
            responses=[_impl_json()],
            metrics=metrics,
            phase_cls=ImplementPhase,
        )

        obs = await phase.observe()
        await phase.plan(obs)

        assert metrics.total_llm_calls == 1
        assert metrics.total_tokens_in > 0

    @pytest.mark.asyncio
    async def test_review_plan_updates_metrics(self):
        from engine.phases.review import ReviewPhase

        metrics = LoopMetrics()
        phase = self._make_phase(
            responses=[_review_json()],
            metrics=metrics,
            phase_cls=ReviewPhase,
        )

        obs = await phase.observe()
        await phase.plan(obs)

        assert metrics.total_llm_calls == 1
        assert metrics.total_tokens_in > 0

    @pytest.mark.asyncio
    async def test_validate_plan_updates_metrics(self):
        from engine.phases.validate import ValidatePhase

        metrics = LoopMetrics()
        phase = self._make_phase(
            responses=[_validate_json()],
            metrics=metrics,
            phase_cls=ValidatePhase,
        )

        obs = await phase.observe()
        await phase.plan(obs)

        assert metrics.total_llm_calls == 1
        assert metrics.total_tokens_in > 0
