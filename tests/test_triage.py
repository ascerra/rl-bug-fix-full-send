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
from engine.phases.triage import (
    TriagePhase,
    _extract_triage_keywords,
    _suggest_components,
    parse_triage_response,
)
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
    async def test_ambiguous_with_confidence_proceeds(self):
        """Ambiguous with confidence >= 0.4 proceeds as bug."""
        phase = _make_triage()
        result = await phase.reflect(
            {
                "valid": True,
                "classification": "ambiguous",
                "injection_detected": False,
                "triage_result": json.loads(_ambiguous_response()),
            }
        )
        assert result.success is True
        assert result.next_phase == "implement"
        assert result.artifacts["classification"] == "ambiguous_as_bug"

    @pytest.mark.asyncio
    async def test_ambiguous_low_confidence_escalates(self):
        """Ambiguous with confidence < 0.4 still escalates."""
        phase = _make_triage()
        low_conf = json.loads(_ambiguous_response())
        low_conf["confidence"] = 0.2
        result = await phase.reflect(
            {
                "valid": True,
                "classification": "ambiguous",
                "injection_detected": False,
                "triage_result": low_conf,
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
async def test_execute_ambiguous_proceeds_with_confidence():
    """Full execute() with an ambiguous classification and decent confidence → proceeds."""
    phase = _make_triage(responses=[_ambiguous_response()])
    result = await phase.execute()
    assert result.success is True
    assert result.artifacts["classification"] == "ambiguous_as_bug"


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
    """TriagePhase can be registered in PipelineEngine and executed."""
    from engine.loop import PipelineEngine

    repo = tmp_path / "repo"
    repo.mkdir()
    out = tmp_path / "output"
    out.mkdir()

    loop = PipelineEngine(
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


# ------------------------------------------------------------------
# _extract_triage_keywords tests
# ------------------------------------------------------------------


class TestExtractTriageKeywords:
    def test_extracts_from_title(self):
        kws = _extract_triage_keywords("nil pointer in reconciler", "")
        assert "nil" in kws
        assert "pointer" in kws
        assert "reconciler" in kws

    def test_filters_stopwords(self):
        kws = _extract_triage_keywords("the error in the file", "")
        assert "the" not in kws
        assert "error" not in kws
        assert "file" not in kws

    def test_filters_short_words(self):
        kws = _extract_triage_keywords("go is broken", "")
        assert "go" not in kws
        assert "is" not in kws
        assert "broken" in kws

    def test_rejects_na(self):
        kws = _extract_triage_keywords("N/A", "N/A")
        assert kws == []

    def test_extracts_from_body(self):
        kws = _extract_triage_keywords("short", "the controller panics when owner is nil pointer")
        assert "controller" in kws
        assert "panics" in kws
        assert "owner" in kws

    def test_deduplicates(self):
        kws = _extract_triage_keywords("reconciler crash", "The reconciler panics")
        assert kws.count("reconciler") == 1

    def test_max_keywords_limit(self):
        long_title = " ".join(f"keyword{i}" for i in range(20))
        kws = _extract_triage_keywords(long_title, "", max_keywords=5)
        assert len(kws) == 5

    def test_empty_input(self):
        kws = _extract_triage_keywords("", "")
        assert kws == []


# ------------------------------------------------------------------
# _suggest_components tests
# ------------------------------------------------------------------


class TestSuggestComponents:
    _REPO_FILES = (
        "./pkg/controller/reconciler.go\n"
        "./pkg/controller/reconciler_test.go\n"
        "./pkg/api/server.go\n"
        "./pkg/api/handler.go\n"
        "./cmd/main.go\n"
        "./internal/utils/helpers.go\n"
    )

    def test_matches_keywords_to_files(self):
        result = _suggest_components(
            "nil pointer in reconciler",
            "The controller crashes",
            self._REPO_FILES,
        )
        assert any("reconciler.go" in f for f in result)
        assert any("controller" in f for f in result)

    def test_prefers_source_over_test(self):
        result = _suggest_components(
            "reconciler bug",
            "",
            self._REPO_FILES,
        )
        if len(result) >= 2:
            source_idx = next(
                (i for i, f in enumerate(result) if "reconciler.go" in f and "_test" not in f),
                None,
            )
            test_idx = next(
                (i for i, f in enumerate(result) if "reconciler_test.go" in f),
                None,
            )
            if source_idx is not None and test_idx is not None:
                assert source_idx < test_idx

    def test_empty_keywords_returns_empty(self):
        result = _suggest_components("N/A", "N/A", self._REPO_FILES)
        assert result == []

    def test_empty_repo_files_returns_empty(self):
        result = _suggest_components("reconciler bug", "", "")
        assert result == []

    def test_no_matching_files(self):
        result = _suggest_components(
            "completely unrelated zebra",
            "",
            self._REPO_FILES,
        )
        assert result == []

    def test_max_results_limit(self):
        big_repo = "\n".join(f"./pkg/reconciler/file{i}.go" for i in range(20))
        result = _suggest_components("reconciler", "", big_repo, max_results=3)
        assert len(result) <= 3

    def test_filename_match_scores_higher(self):
        repo = "./deep/nested/reconciler.go\n./other/handler.go\n"
        result = _suggest_components("reconciler", "", repo)
        assert result[0] == "./deep/nested/reconciler.go"

    def test_multiple_keyword_matches(self):
        repo = "./pkg/api/server.go\n./pkg/controller/reconciler.go\n./pkg/controller/server.go\n"
        result = _suggest_components(
            "controller server crash",
            "",
            repo,
        )
        assert result[0] == "./pkg/controller/server.go"


# ------------------------------------------------------------------
# TriagePhase.act component suggestion fallback tests
# ------------------------------------------------------------------


class TestTriageActComponentSuggestion:
    @pytest.mark.asyncio
    async def test_act_suggests_components_when_llm_returns_empty(self, tmp_path):
        """When LLM returns empty affected_components, act() uses keyword fallback."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pkg" / "controller").mkdir(parents=True)
        (repo / "pkg" / "controller" / "reconciler.go").write_text("package controller\n")
        (repo / "main.go").write_text("package main\n")

        llm_response = json.dumps(
            {
                "classification": "bug",
                "confidence": 0.9,
                "severity": "high",
                "affected_components": [],
                "reproduction": {"existing_tests": [], "can_reproduce": False},
                "injection_detected": False,
                "recommendation": "proceed",
                "reasoning": "Nil pointer dereference in controller reconciler.",
            }
        )

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

        phase = TriagePhase(
            llm=MockProvider(responses=[llm_response]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic in reconciler",
                "body": "The controller reconciler crashes on nil owner.",
            },
            config=EngineConfig(),
            tool_executor=tool_executor,
        )

        obs = await phase.observe()
        plan = await phase.plan(obs)

        assert plan["triage_result"]["affected_components"] == []

        act_result = await phase.act(plan)

        assert len(act_result["verified_components"]) > 0
        suggested_actions = [
            a for a in act_result["actions"] if a.get("action") == "suggest_components"
        ]
        assert len(suggested_actions) == 1
        assert act_result["triage_result"]["affected_components"] != []

    @pytest.mark.asyncio
    async def test_act_skips_suggestion_when_llm_returns_valid_components(self, tmp_path):
        """When LLM returns valid affected_components, no fallback is triggered."""
        phase = _make_triage_with_repo(tmp_path)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        act_result = await phase.act(plan)

        suggested_actions = [
            a for a in act_result["actions"] if a.get("action") == "suggest_components"
        ]
        assert len(suggested_actions) == 0

    @pytest.mark.asyncio
    async def test_act_suggestion_no_keywords_no_crash(self, tmp_path):
        """When issue has no useful keywords, suggestion returns empty gracefully."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "main.go").write_text("package main\n")

        llm_response = json.dumps(
            {
                "classification": "bug",
                "confidence": 0.9,
                "severity": "high",
                "affected_components": [],
                "reproduction": {"existing_tests": [], "can_reproduce": False},
                "injection_detected": False,
                "recommendation": "proceed",
                "reasoning": "Bug.",
            }
        )

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

        phase = TriagePhase(
            llm=MockProvider(responses=[llm_response]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={"url": "u", "title": "N/A", "body": "N/A"},
            config=EngineConfig(),
            tool_executor=tool_executor,
        )

        obs = await phase.observe()
        plan = await phase.plan(obs)
        act_result = await phase.act(plan)
        assert act_result["triage_result"]["affected_components"] == []

    @pytest.mark.asyncio
    async def test_suggested_components_appear_in_artifacts(self, tmp_path):
        """Suggested components should propagate through to reflect() artifacts."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "pkg" / "controller").mkdir(parents=True)
        (repo / "pkg" / "controller" / "reconciler.go").write_text("package controller\n")

        llm_response = json.dumps(
            {
                "classification": "bug",
                "confidence": 0.9,
                "severity": "high",
                "affected_components": [],
                "reproduction": {"existing_tests": [], "can_reproduce": False},
                "injection_detected": False,
                "recommendation": "proceed",
                "reasoning": "Nil pointer in controller reconciler.",
            }
        )

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

        phase = TriagePhase(
            llm=MockProvider(responses=[llm_response]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic in reconciler",
                "body": "Controller reconciler crashes on nil owner.",
            },
            config=EngineConfig(),
            tool_executor=tool_executor,
        )

        result = await phase.execute()
        assert result.success is True
        assert len(result.artifacts.get("verified_components", [])) > 0


# ------------------------------------------------------------------
# _fetch_issue tests — validates D1 (7.1) fix
# ------------------------------------------------------------------


def _make_triage_url_only(
    responses: list[str] | None = None,
    tool_executor: ToolExecutor | None = None,
) -> TriagePhase:
    """Create a TriagePhase with URL-only issue_data (production scenario)."""
    return TriagePhase(
        llm=MockProvider(responses=responses or [_bug_response()]),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake-repo",
        issue_data={"url": "https://github.com/org/my-repo/issues/42"},
        config=EngineConfig(),
        tool_executor=tool_executor,
    )


class _FakeToolExecutor:
    """Minimal stub that simulates ToolExecutor responses for issue-fetch tests."""

    def __init__(self, responses: dict[str, dict[str, Any]] | None = None):
        self._responses = responses or {}
        self._calls: list[tuple[str, dict[str, Any]]] = []
        self._available_tools = ["file_read", "file_search", "shell_run"]

    @property
    def available_tools(self) -> list[str]:
        return self._available_tools

    async def execute(self, tool_name: str, **kwargs: Any) -> dict[str, Any]:
        self._calls.append((tool_name, kwargs))
        key = kwargs.get("command", kwargs.get("endpoint", ""))
        for pattern, resp in self._responses.items():
            if pattern in key:
                return resp
        return {"success": False, "stdout": "", "stderr": "not configured"}


class TestFetchIssue:
    """Tests for TriagePhase._fetch_issue and its fallback paths."""

    @pytest.mark.asyncio
    async def test_gh_cli_success(self):
        """When gh CLI succeeds, issue data is populated."""
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {
                    "success": True,
                    "stdout": json.dumps({"title": "Real bug title", "body": "Real body text"}),
                    "stderr": "",
                },
            }
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        result = await phase._fetch_issue("https://github.com/org/my-repo/issues/42")
        assert result["title"] == "Real bug title"
        assert result["body"] == "Real body text"
        assert result["url"] == "https://github.com/org/my-repo/issues/42"

    @pytest.mark.asyncio
    async def test_gh_cli_fail_curl_fallback_succeeds(self):
        """When gh CLI fails, curl fallback is tried and succeeds."""
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {"success": False, "stdout": "", "stderr": "auth required"},
                "curl": {
                    "success": True,
                    "stdout": json.dumps({"title": "Curl title", "body": "Curl body"}),
                    "stderr": "",
                },
            }
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        result = await phase._fetch_issue("https://github.com/org/my-repo/issues/42")
        assert result["title"] == "Curl title"
        assert result["body"] == "Curl body"

    @pytest.mark.asyncio
    async def test_gh_cli_fail_api_fallback_succeeds(self):
        """When gh CLI fails and github_api is available, REST API fallback succeeds."""
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {"success": False, "stdout": "", "stderr": "auth required"},
                "/repos/org/my-repo/issues/42": {
                    "success": True,
                    "body": {"title": "API title", "body": "API body content"},
                },
            }
        )
        fake_exec._available_tools.append("github_api")
        phase = _make_triage_url_only(tool_executor=fake_exec)
        result = await phase._fetch_issue("https://github.com/org/my-repo/issues/42")
        assert result["title"] == "API title"
        assert result["body"] == "API body content"

    @pytest.mark.asyncio
    async def test_both_methods_fail_returns_na(self):
        """When both gh CLI and API fallback fail, N/A defaults are returned."""
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {"success": False, "stdout": "", "stderr": "auth fail"},
                "curl": {"success": False, "stdout": "", "stderr": "network error"},
            }
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        result = await phase._fetch_issue("https://github.com/org/my-repo/issues/42")
        assert result["title"] == "N/A"
        assert result["body"] == "N/A"

    @pytest.mark.asyncio
    async def test_invalid_url_returns_na(self):
        """Non-GitHub URLs return N/A without making any calls."""
        fake_exec = _FakeToolExecutor()
        phase = _make_triage_url_only(tool_executor=fake_exec)
        result = await phase._fetch_issue("https://example.com/not-github")
        assert result["title"] == "N/A"
        assert result["body"] == "N/A"
        assert len(fake_exec._calls) == 0

    @pytest.mark.asyncio
    async def test_no_tool_executor_returns_na(self):
        """Without a tool executor, returns N/A without crashing."""
        phase = _make_triage_url_only(tool_executor=None)
        result = await phase._fetch_issue("https://github.com/org/repo/issues/1")
        assert result["title"] == "N/A"
        assert result["body"] == "N/A"

    @pytest.mark.asyncio
    async def test_gh_cli_returns_invalid_json(self):
        """When gh CLI returns non-JSON, falls back to API."""
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {
                    "success": True,
                    "stdout": "not json at all",
                    "stderr": "",
                },
                "curl": {
                    "success": True,
                    "stdout": json.dumps({"title": "Fallback title", "body": "Fallback body"}),
                    "stderr": "",
                },
            }
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        result = await phase._fetch_issue("https://github.com/org/my-repo/issues/42")
        assert result["title"] == "Fallback title"
        assert result["body"] == "Fallback body"

    @pytest.mark.asyncio
    async def test_url_parsing_extracts_repo_and_number(self):
        """URL parsing correctly extracts org/repo and issue number."""
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view 99 --repo acme/widget": {
                    "success": True,
                    "stdout": json.dumps({"title": "Widget bug", "body": "Details"}),
                    "stderr": "",
                },
            }
        )

        phase = _make_triage_url_only(tool_executor=fake_exec)
        phase.issue_data = {"url": "https://github.com/acme/widget/issues/99"}
        await phase._fetch_issue("https://github.com/acme/widget/issues/99")
        cmds = [c[1].get("command", "") for c in fake_exec._calls]
        assert any("gh issue view 99 --repo acme/widget" in cmd for cmd in cmds)

    @pytest.mark.asyncio
    async def test_empty_url_returns_na(self):
        """Empty URL returns N/A immediately."""
        fake_exec = _FakeToolExecutor()
        phase = _make_triage_url_only(tool_executor=fake_exec)
        result = await phase._fetch_issue("")
        assert result["title"] == "N/A"
        assert result["body"] == "N/A"

    @pytest.mark.asyncio
    async def test_narration_on_success(self):
        """Successful fetch does not emit warning narration."""
        logger = StructuredLogger()
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {
                    "success": True,
                    "stdout": json.dumps({"title": "OK", "body": "All good"}),
                    "stderr": "",
                },
            }
        )
        phase = TriagePhase(
            llm=MockProvider(responses=[_bug_response()]),
            logger=logger,
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "https://github.com/org/repo/issues/1"},
            config=EngineConfig(),
            tool_executor=fake_exec,
        )
        result = await phase._fetch_issue("https://github.com/org/repo/issues/1")
        assert result["title"] == "OK"
        warning_narrations = [n for n in logger._narrations if "WARNING" in n.get("message", "")]
        assert len(warning_narrations) == 0

    @pytest.mark.asyncio
    async def test_narration_on_total_failure(self):
        """When both methods fail, a warning narration is emitted."""
        logger = StructuredLogger()
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {"success": False, "stdout": "", "stderr": "fail"},
                "curl": {"success": False, "stdout": "", "stderr": "fail"},
            }
        )
        phase = TriagePhase(
            llm=MockProvider(responses=[_bug_response()]),
            logger=logger,
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "https://github.com/org/repo/issues/1"},
            config=EngineConfig(),
            tool_executor=fake_exec,
        )
        await phase._fetch_issue("https://github.com/org/repo/issues/1")
        warning_narrations = [n for n in logger._narrations if "WARNING" in n.get("message", "")]
        assert len(warning_narrations) == 1
        assert "Could not fetch issue content" in warning_narrations[0]["message"]


class TestFetchIssueGhCli:
    """Direct tests for _fetch_issue_gh_cli helper."""

    @pytest.mark.asyncio
    async def test_success_populates_issue(self):
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {
                    "success": True,
                    "stdout": json.dumps({"title": "T", "body": "B"}),
                    "stderr": "",
                },
            }
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_gh_cli("org/repo", "1", issue) is True
        assert issue["title"] == "T"
        assert issue["body"] == "B"

    @pytest.mark.asyncio
    async def test_command_failure_returns_false(self):
        fake_exec = _FakeToolExecutor(
            responses={"gh issue view": {"success": False, "stdout": "", "stderr": "err"}}
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_gh_cli("org/repo", "1", issue) is False
        assert issue["title"] == "N/A"

    @pytest.mark.asyncio
    async def test_json_parse_error_returns_false(self):
        fake_exec = _FakeToolExecutor(
            responses={"gh issue view": {"success": True, "stdout": "not-json", "stderr": ""}}
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_gh_cli("org/repo", "1", issue) is False

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_false(self):
        fake_exec = _FakeToolExecutor(
            responses={"gh issue view": {"success": True, "stdout": "", "stderr": ""}}
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_gh_cli("org/repo", "1", issue) is False


class TestFetchIssueApi:
    """Direct tests for _fetch_issue_api helper (curl and github_api paths)."""

    @pytest.mark.asyncio
    async def test_curl_fallback_success(self):
        fake_exec = _FakeToolExecutor(
            responses={
                "curl": {
                    "success": True,
                    "stdout": json.dumps({"title": "CT", "body": "CB"}),
                    "stderr": "",
                },
            }
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_api("org/repo", "1", issue) is True
        assert issue["title"] == "CT"

    @pytest.mark.asyncio
    async def test_curl_fallback_failure(self):
        fake_exec = _FakeToolExecutor(
            responses={"curl": {"success": False, "stdout": "", "stderr": "fail"}}
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_api("org/repo", "1", issue) is False

    @pytest.mark.asyncio
    async def test_github_api_tool_success(self):
        fake_exec = _FakeToolExecutor(
            responses={
                "/repos/org/repo/issues/1": {
                    "success": True,
                    "body": {"title": "API T", "body": "API B"},
                },
            }
        )
        fake_exec._available_tools.append("github_api")
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_api("org/repo", "1", issue) is True
        assert issue["title"] == "API T"

    @pytest.mark.asyncio
    async def test_github_api_tool_failure(self):
        fake_exec = _FakeToolExecutor(
            responses={
                "/repos/org/repo/issues/1": {"success": False, "body": {}},
            }
        )
        fake_exec._available_tools.append("github_api")
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_api("org/repo", "1", issue) is False

    @pytest.mark.asyncio
    async def test_curl_bad_json_returns_false(self):
        fake_exec = _FakeToolExecutor(
            responses={
                "curl": {"success": True, "stdout": "not-json", "stderr": ""},
            }
        )
        phase = _make_triage_url_only(tool_executor=fake_exec)
        issue: dict[str, Any] = {"url": "u", "title": "N/A", "body": "N/A"}
        assert await phase._fetch_issue_api("org/repo", "1", issue) is False


class TestObserveIssueFetch:
    """Tests verifying observe() triggers _fetch_issue for URL-only issue_data."""

    @pytest.mark.asyncio
    async def test_observe_fetches_issue_when_title_missing(self):
        """observe() calls _fetch_issue when issue_data has no title/body."""
        fake_exec = _FakeToolExecutor(
            responses={
                "gh issue view": {
                    "success": True,
                    "stdout": json.dumps({"title": "Fetched title", "body": "Fetched body"}),
                    "stderr": "",
                },
                "find": {"success": True, "stdout": "./main.go\n", "stderr": ""},
            }
        )
        phase = _make_triage_url_only(
            responses=[_bug_response()],
            tool_executor=fake_exec,
        )
        obs = await phase.observe()
        assert obs["issue"]["title"] == "Fetched title"
        assert obs["issue"]["body"] == "Fetched body"
        assert phase.issue_data["title"] == "Fetched title"

    @pytest.mark.asyncio
    async def test_observe_skips_fetch_when_title_present(self):
        """observe() does NOT call _fetch_issue when issue_data already has title+body."""
        fake_exec = _FakeToolExecutor(
            responses={
                "find": {"success": True, "stdout": "./main.go\n", "stderr": ""},
            }
        )
        phase = TriagePhase(
            llm=MockProvider(responses=[_bug_response()]),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Already set",
                "body": "Already set body",
            },
            config=EngineConfig(),
            tool_executor=fake_exec,
        )
        obs = await phase.observe()
        assert obs["issue"]["title"] == "Already set"
        gh_calls = [c for c in fake_exec._calls if "gh issue view" in c[1].get("command", "")]
        assert len(gh_calls) == 0

    @pytest.mark.asyncio
    async def test_loop_passes_url_only_issue_data(self):
        """RL Engine passes URL-only issue_data, which triggers _fetch_issue in triage."""
        from engine.loop import PipelineEngine

        phase_issue_data_captured: list[dict] = []

        class SpyTriagePhase(TriagePhase):
            async def observe(self_inner) -> dict[str, Any]:
                phase_issue_data_captured.append(dict(self_inner.issue_data))
                return await super().observe()

        loop = PipelineEngine(
            config=EngineConfig(),
            llm=MockProvider(responses=[_bug_response()]),
            issue_url="https://github.com/org/repo/issues/99",
            repo_path="/tmp/fake-repo",
            output_dir="/tmp/test-output",
        )
        loop.register_phase("triage", SpyTriagePhase)
        await loop.run()

        assert len(phase_issue_data_captured) >= 1
        initial_data = phase_issue_data_captured[0]
        assert initial_data.get("url") == "https://github.com/org/repo/issues/99"
        assert "title" not in initial_data or initial_data.get("title") == "N/A"
