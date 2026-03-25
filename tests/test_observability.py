"""Tests for observability components (logger, tracer, metrics)."""

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
