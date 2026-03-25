"""Tests for the Triage Phase — classification, component verification, reproduction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engine.config import EngineConfig
from engine.integrations.llm import MockProvider
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.phases.triage import TriagePhase, parse_triage_response
from engine.tools.executor import ToolExecutor

# ------------------------------------------------------------------
# Helpers: canned LLM responses
# ------------------------------------------------------------------


def _bug_response(
    severity: str = "high",
    confidence: float = 0.9,
    components: list[str] | None = None,
) -> str:
    """Return a JSON string representing a successful bug triage."""
    return json.dumps(
        {
            "classification": "bug",
            "confidence": confidence,
            "severity": severity,
            "affected_components": components or ["pkg/controller/reconciler.go"],
            "reproduction": {
                "existing_tests": ["pkg/controller/reconciler_test.go"],
                "can_reproduce": True,
                "reproduction_steps": "Run go test ./pkg/controller/...",
            },
            "injection_detected": False,
            "recommendation": "proceed",
            "reasoning": "The issue describes a nil pointer dereference — clearly a bug.",
        }
    )


def _feature_response() -> str:
    return json.dumps(
        {
            "classification": "feature",
            "confidence": 0.85,
            "severity": "medium",
            "affected_components": [],
            "reproduction": {
                "existing_tests": [],
                "can_reproduce": False,
                "reproduction_steps": "",
            },
            "injection_detected": False,
            "recommendation": "escalate",
            "reasoning": "The issue requests new functionality, not a bug fix.",
        }
    )


def _ambiguous_response() -> str:
    return json.dumps(
        {
            "classification": "ambiguous",
            "confidence": 0.4,
            "severity": "medium",
            "affected_components": [],
            "reproduction": {
                "existing_tests": [],
                "can_reproduce": False,
                "reproduction_steps": "",
            },
            "injection_detected": False,
            "recommendation": "escalate",
            "reasoning": "Cannot determine if this is a bug or expected behavior.",
        }
    )


def _injection_response() -> str:
    return json.dumps(
        {
            "classification": "bug",
            "confidence": 0.7,
            "severity": "medium",
            "affected_components": [],
            "reproduction": {
                "existing_tests": [],
                "can_reproduce": False,
                "reproduction_steps": "",
            },
            "injection_detected": True,
            "recommendation": "escalate",
            "reasoning": "Issue body contains prompt injection attempt.",
        }
    )


# ------------------------------------------------------------------
# Helpers: phase instantiation
# ------------------------------------------------------------------


def _make_triage(
    responses: list[str] | None = None,
    repo_path: str = "/tmp/fake-repo",
    issue_data: dict[str, Any] | None = None,
    config: EngineConfig | None = None,
    tool_executor: ToolExecutor | None = None,
) -> TriagePhase:
    llm = MockProvider(responses=responses or [_bug_response()])
    return TriagePhase(
        llm=llm,
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path=repo_path,
        issue_data=issue_data
        or {
            "url": "https://github.com/test/repo/issues/42",
            "title": "nil pointer panic in reconciler",
            "body": "When reconciling a resource with no owner, the controller panics.",
        },
        config=config or EngineConfig(),
        tool_executor=tool_executor,
    )


def _make_triage_with_repo(tmp_path: Path, responses: list[str] | None = None) -> TriagePhase:
    """Create a TriagePhase with a real temp repo and ToolExecutor."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg" / "controller").mkdir(parents=True)
    (repo / "pkg" / "controller" / "reconciler.go").write_text("package controller\n")
    (repo / "pkg" / "controller" / "reconciler_test.go").write_text("package controller\n")
    (repo / "main.go").write_text("package main\n")

    tracer = Tracer()
    logger = StructuredLogger()
    metrics = LoopMetrics()
    tool_executor = ToolExecutor(
        repo_path=str(repo),
        logger=logger,
        tracer=tracer,
        metrics=metrics,
        allowed_tools=["file_read", "file_search", "shell_run"],
    )

    return TriagePhase(
        llm=MockProvider(responses=responses or [_bug_response()]),
        logger=logger,
        tracer=tracer,
        repo_path=str(repo),
        issue_data={
            "url": "https://github.com/test/repo/issues/42",
            "title": "nil pointer panic in reconciler",
            "body": "The reconciler crashes when reconciling a resource with no owner ref.",
        },
        config=EngineConfig(),
        tool_executor=tool_executor,
    )


# ------------------------------------------------------------------
# parse_triage_response tests
# ------------------------------------------------------------------


class TestParseTriage:
    def test_direct_json(self):
        raw = _bug_response()
        result = parse_triage_response(raw)
        assert result["classification"] == "bug"
        assert result["confidence"] == 0.9

    def test_json_code_block(self):
        raw = "Here is my analysis:\n```json\n" + _bug_response() + "\n```\nDone."
        result = parse_triage_response(raw)
        assert result["classification"] == "bug"

    def test_generic_code_block(self):
        raw = "Analysis:\n```\n" + _bug_response() + "\n```"
        result = parse_triage_response(raw)
        assert result["classification"] == "bug"

    def test_malformed_returns_default(self):
        result = parse_triage_response("This is not JSON at all.")
        assert result["classification"] == "ambiguous"
        assert result["confidence"] == 0.0
        assert result["recommendation"] == "escalate"
        assert "Failed to parse" in result["reasoning"]

    def test_empty_string(self):
        result = parse_triage_response("")
        assert result["classification"] == "ambiguous"
        assert result["recommendation"] == "escalate"

    def test_partial_json(self):
        result = parse_triage_response('{"classification": "bug"')
        assert result["classification"] == "ambiguous"

    def test_multiple_code_blocks_picks_valid(self):
        raw = "```\nnot json\n```\n\n```json\n" + _bug_response() + "\n```"
        result = parse_triage_response(raw)
        assert result["classification"] == "bug"


# ------------------------------------------------------------------
# TriagePhase.observe tests
# ------------------------------------------------------------------


class TestTriageObserve:
    @pytest.mark.asyncio
    async def test_observe_without_tools(self):
        phase = _make_triage(tool_executor=None)
        obs = await phase.observe()
        assert obs["issue"]["url"] == "https://github.com/test/repo/issues/42"
        assert obs["repo_files"] == ""
        assert obs["test_files"] == ""

    @pytest.mark.asyncio
    async def test_observe_with_tools(self, tmp_path):
        phase = _make_triage_with_repo(tmp_path)
        obs = await phase.observe()
        assert "reconciler.go" in obs["repo_files"]
        assert "reconciler_test.go" in obs["test_files"]


# ------------------------------------------------------------------
# TriagePhase.plan tests
# ------------------------------------------------------------------


class TestTriagePlan:
    @pytest.mark.asyncio
    async def test_plan_calls_llm(self):
        phase = _make_triage()
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["triage_result"]["classification"] == "bug"
        assert "raw_llm_response" in plan

    @pytest.mark.asyncio
    async def test_plan_records_llm_call(self):
        tracer = Tracer()
        phase = TriagePhase(
            llm=MockProvider(responses=[_bug_response()]),
            logger=StructuredLogger(),
            tracer=tracer,
            repo_path="/tmp/fake",
            issue_data={"url": "https://example.com", "title": "test", "body": "test body"},
            config=EngineConfig(),
        )
        obs = await phase.observe()
        await phase.plan(obs)
        llm_actions = [a for a in tracer.get_actions() if a.action_type == "llm_query"]
        assert len(llm_actions) == 1
        assert llm_actions[0].llm_context["provider"] == "mock"

    @pytest.mark.asyncio
    async def test_plan_wraps_untrusted_content(self):
        llm = MockProvider(responses=[_bug_response()])
        phase = TriagePhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "https://example.com", "title": "t", "body": "untrusted body"},
            config=EngineConfig(),
        )
        obs = await phase.observe()
        await phase.plan(obs)
        call = llm.call_log[0]
        msg = call["messages"][0]["content"]
        assert "UNTRUSTED CONTENT BELOW" in msg
        assert "END UNTRUSTED CONTENT" in msg
        assert "untrusted body" in msg


# ------------------------------------------------------------------
# TriagePhase.act tests
# ------------------------------------------------------------------


class TestTriageAct:
    @pytest.mark.asyncio
    async def test_act_verifies_components_with_tools(self, tmp_path):
        phase = _make_triage_with_repo(tmp_path)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        verified = result["verified_components"]
        assert len(verified) >= 1
        found_any = any(v["found"] for v in verified)
        assert found_any, "Should find at least one matching component"

    @pytest.mark.asyncio
    async def test_act_without_tools(self):
        phase = _make_triage(tool_executor=None)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["verified_components"] == []
        assert result["reproduction"]["attempted"] is False

    @pytest.mark.asyncio
    async def test_act_reproduction_skipped_when_disabled(self, tmp_path):
        cfg = EngineConfig()
        cfg.phases.triage.attempt_reproduction = False

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "test_example.py").write_text("def test_foo(): pass\n")

        phase = TriagePhase(
            llm=MockProvider(responses=[_bug_response(components=["test_example.py"])]),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path=str(repo),
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=cfg,
            tool_executor=ToolExecutor(
                repo_path=str(repo),
                logger=StructuredLogger(),
                tracer=Tracer(),
                metrics=LoopMetrics(),
            ),
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["reproduction"]["attempted"] is False


# ------------------------------------------------------------------
# TriagePhase.validate tests
# ------------------------------------------------------------------


class TestTriageValidate:
    @pytest.mark.asyncio
    async def test_validate_valid_triage(self):
        phase = _make_triage()
        validation = await phase.validate(
            {
                "triage_result": json.loads(_bug_response()),
                "verified_components": [],
                "reproduction": {"attempted": False},
            }
        )
        assert validation["valid"] is True
        assert validation["issues"] == []
        assert validation["classification"] == "bug"

    @pytest.mark.asyncio
    async def test_validate_invalid_classification(self):
        phase = _make_triage()
        triage = json.loads(_bug_response())
        triage["classification"] = "unknown"
        validation = await phase.validate({"triage_result": triage})
        assert validation["valid"] is False
        assert any("Invalid classification" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_invalid_confidence(self):
        phase = _make_triage()
        triage = json.loads(_bug_response())
        triage["confidence"] = 1.5
        validation = await phase.validate({"triage_result": triage})
        assert validation["valid"] is False
        assert any("Invalid confidence" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_negative_confidence(self):
        phase = _make_triage()
        triage = json.loads(_bug_response())
        triage["confidence"] = -0.1
        validation = await phase.validate({"triage_result": triage})
        assert validation["valid"] is False

    @pytest.mark.asyncio
    async def test_validate_invalid_severity(self):
        phase = _make_triage()
        triage = json.loads(_bug_response())
        triage["severity"] = "extreme"
        validation = await phase.validate({"triage_result": triage})
        assert validation["valid"] is False
        assert any("Invalid severity" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_missing_reasoning(self):
        phase = _make_triage()
        triage = json.loads(_bug_response())
        triage["reasoning"] = ""
        validation = await phase.validate({"triage_result": triage})
        assert validation["valid"] is False
        assert any("Missing reasoning" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_detects_injection_flag(self):
        phase = _make_triage()
        triage = json.loads(_injection_response())
        validation = await phase.validate({"triage_result": triage})
        assert validation["injection_detected"] is True


# ------------------------------------------------------------------
# TriagePhase.reflect tests
# ------------------------------------------------------------------


class TestTriageReflect:
    @pytest.mark.asyncio
    async def test_bug_proceeds_to_implement(self):
        phase = _make_triage()
        result = await phase.reflect(
            {
                "valid": True,
                "classification": "bug",
                "injection_detected": False,
                "triage_result": json.loads(_bug_response()),
                "verified_components": [],
                "reproduction": {},
            }
        )
        assert result.success is True
        assert result.next_phase == "implement"
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_feature_escalates(self):
        phase = _make_triage()
        result = await phase.reflect(
            {
                "valid": True,
                "classification": "feature",
                "injection_detected": False,
                "triage_result": json.loads(_feature_response()),
            }
        )
        assert result.success is False
        assert result.escalate is True
        assert "feature" in result.escalation_reason

    @pytest.mark.asyncio
    async def test_ambiguous_escalates(self):
        phase = _make_triage()
        result = await phase.reflect(
            {
                "valid": True,
                "classification": "ambiguous",
                "injection_detected": False,
                "triage_result": json.loads(_ambiguous_response()),
            }
        )
        assert result.success is False
        assert result.escalate is True
        assert "ambiguous" in result.escalation_reason

    @pytest.mark.asyncio
    async def test_injection_detected_escalates(self):
        phase = _make_triage()
        result = await phase.reflect(
            {
                "valid": True,
                "classification": "bug",
                "injection_detected": True,
                "triage_result": json.loads(_injection_response()),
            }
        )
        assert result.success is False
        assert result.escalate is True
        assert "injection" in result.escalation_reason.lower()

    @pytest.mark.asyncio
    async def test_validation_issues_trigger_retry(self):
        phase = _make_triage()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Invalid classification: 'garbage'"],
                "classification": "garbage",
                "injection_detected": False,
                "triage_result": {},
            }
        )
        assert result.success is False
        assert result.should_continue is True
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_recommendation_escalate(self):
        phase = _make_triage()
        triage = json.loads(_bug_response())
        triage["recommendation"] = "escalate"
        triage["reasoning"] = "Too complex for automated fix"
        result = await phase.reflect(
            {
                "valid": True,
                "classification": "bug",
                "injection_detected": False,
                "triage_result": triage,
            }
        )
        assert result.success is False
        assert result.escalate is True
        assert "escalation" in result.escalation_reason.lower()


# ------------------------------------------------------------------
# Full execute lifecycle tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_bug_success():
    """Full execute() with a bug classification → success, next_phase=implement."""
    phase = _make_triage()
    result = await phase.execute()
    assert result.success is True
    assert result.next_phase == "implement"
    assert result.findings["classification"] == "bug"


@pytest.mark.asyncio
async def test_execute_feature_escalates():
    """Full execute() with a feature classification → escalate."""
    phase = _make_triage(responses=[_feature_response()])
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is True


@pytest.mark.asyncio
async def test_execute_ambiguous_escalates():
    """Full execute() with an ambiguous classification → escalate."""
    phase = _make_triage(responses=[_ambiguous_response()])
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is True


@pytest.mark.asyncio
async def test_execute_injection_escalates():
    """Full execute() when injection is detected → escalate."""
    phase = _make_triage(responses=[_injection_response()])
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is True
    assert "injection" in result.escalation_reason.lower()


@pytest.mark.asyncio
async def test_execute_malformed_llm_response_retries():
    """Full execute() with unparseable LLM output → retry (should_continue=True)."""
    phase = _make_triage(responses=["This is not JSON, sorry!"])
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is True


@pytest.mark.asyncio
async def test_execute_with_real_repo(tmp_path):
    """Full execute() against a temp repo with real ToolExecutor."""
    phase = _make_triage_with_repo(tmp_path)
    result = await phase.execute()
    assert result.success is True
    assert result.next_phase == "implement"
    assert "triage_report" in result.artifacts


@pytest.mark.asyncio
async def test_execute_artifacts_populated(tmp_path):
    """Success result includes triage_report, verified_components, reproduction."""
    phase = _make_triage_with_repo(tmp_path)
    result = await phase.execute()
    assert "triage_report" in result.artifacts
    assert "verified_components" in result.artifacts
    assert "reproduction" in result.artifacts


# ------------------------------------------------------------------
# Integration with loop
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_triage_phase_in_loop(tmp_path):
    """TriagePhase can be registered in RalphLoop and executed."""
    from engine.loop import RalphLoop

    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "output"
    out.mkdir()

    loop = RalphLoop(
        config=EngineConfig(),
        llm=MockProvider(responses=[_bug_response()]),
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(repo),
        output_dir=str(out),
    )
    loop.register_phase("triage", TriagePhase)

    execution = await loop.run()
    phases_run = [it["phase"] for it in execution.iterations]
    assert "triage" in phases_run
    triage_iteration = execution.iterations[0]
    assert triage_iteration["result"]["success"] is True


# ------------------------------------------------------------------
# ClassVar and config tests
# ------------------------------------------------------------------


class TestTriageClassProperties:
    def test_name(self):
        assert TriagePhase.name == "triage"

    def test_allowed_tools_empty_falls_back(self):
        tools = TriagePhase.get_allowed_tools()
        assert "file_read" in tools
        assert "file_search" in tools
        assert "shell_run" in tools
        assert "file_write" not in tools
        assert "git_commit" not in tools

    def test_config_triage_flags(self):
        cfg = EngineConfig()
        assert cfg.phases.triage.classify_bug_vs_feature is True
        assert cfg.phases.triage.attempt_reproduction is True
        assert cfg.phases.triage.write_failing_test is True
