"""Tests for the Implementation Phase — fix generation, inner iteration, test/lint validation."""

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
from engine.phases.base import PhaseResult
from engine.phases.implement import (
    ImplementPhase,
    _collect_previously_tried_files,
    _extract_keywords,
    _format_retry_context,
    _format_review_feedback,
    is_parse_failure,
    parse_implement_response,
    validate_impl_plan,
)
from engine.tools.executor import ToolExecutor

# ------------------------------------------------------------------
# Helpers: canned LLM responses
# ------------------------------------------------------------------


def _fix_response(
    files: list[dict[str, str]] | None = None,
    tests_passing: bool = True,
    confidence: float = 0.9,
) -> str:
    """Return a JSON string representing a successful implementation plan."""
    return json.dumps(
        {
            "root_cause": "Nil pointer dereference when owner ref is nil",
            "fix_description": "Added nil check before accessing owner reference",
            "files_changed": ["pkg/controller/reconciler.go"],
            "file_changes": files
            or [
                {
                    "path": "pkg/controller/reconciler.go",
                    "content": "package controller\n\nfunc Reconcile() error {\n\tif owner == nil"
                    " {\n\t\treturn nil\n\t}\n\treturn nil\n}\n",
                }
            ],
            "test_added": "pkg/controller/reconciler_test.go",
            "tests_passing": tests_passing,
            "linters_passing": True,
            "confidence": confidence,
            "diff_summary": "Added nil check for owner reference in Reconcile()",
        }
    )


def _fix_response_no_changes() -> str:
    return json.dumps(
        {
            "root_cause": "unknown",
            "fix_description": "Could not determine fix",
            "files_changed": [],
            "file_changes": [],
            "test_added": "",
            "tests_passing": False,
            "linters_passing": False,
            "confidence": 0.1,
            "diff_summary": "",
        }
    )


def _triage_phase_result(
    components: list[str] | None = None,
) -> PhaseResult:
    """Create a mock successful triage PhaseResult."""
    return PhaseResult(
        phase="triage",
        success=True,
        should_continue=True,
        next_phase="implement",
        findings={
            "classification": "bug",
            "severity": "high",
            "affected_components": components or ["pkg/controller/reconciler.go"],
            "reasoning": "Nil pointer dereference in reconciler",
        },
        artifacts={
            "triage_report": {
                "classification": "bug",
                "severity": "high",
                "affected_components": components or ["pkg/controller/reconciler.go"],
                "reasoning": "Nil pointer dereference in reconciler",
            },
            "verified_components": [{"path": "pkg/controller/reconciler.go", "found": True}],
            "reproduction": {"attempted": True, "test_success": False},
        },
    )


# ------------------------------------------------------------------
# Helpers: phase instantiation
# ------------------------------------------------------------------


def _make_implement(
    responses: list[str] | None = None,
    repo_path: str = "/tmp/fake-repo",
    issue_data: dict[str, Any] | None = None,
    config: EngineConfig | None = None,
    tool_executor: ToolExecutor | None = None,
    prior_results: list[PhaseResult] | None = None,
) -> ImplementPhase:
    llm = MockProvider(responses=responses or [_fix_response()])
    return ImplementPhase(
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
        prior_phase_results=(
            prior_results if prior_results is not None else [_triage_phase_result()]
        ),
    )


def _make_implement_with_repo(
    tmp_path: Path,
    responses: list[str] | None = None,
    config: EngineConfig | None = None,
) -> ImplementPhase:
    """Create an ImplementPhase with a real temp repo and ToolExecutor."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg" / "controller").mkdir(parents=True)
    (repo / "pkg" / "controller" / "reconciler.go").write_text(
        "package controller\n\nfunc Reconcile() error {\n\treturn nil\n}\n"
    )
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
    )

    return ImplementPhase(
        llm=MockProvider(responses=responses or [_fix_response()]),
        logger=logger,
        tracer=tracer,
        repo_path=str(repo),
        issue_data={
            "url": "https://github.com/test/repo/issues/42",
            "title": "nil pointer panic in reconciler",
            "body": "The reconciler crashes when reconciling a resource with no owner ref.",
        },
        config=config or EngineConfig(),
        tool_executor=tool_executor,
        prior_phase_results=[_triage_phase_result()],
    )


# ------------------------------------------------------------------
# parse_implement_response tests
# ------------------------------------------------------------------


class TestParseImplementResponse:
    def test_direct_json(self):
        raw = _fix_response()
        result = parse_implement_response(raw)
        assert result["root_cause"] == "Nil pointer dereference when owner ref is nil"
        assert result["confidence"] == 0.9

    def test_json_code_block(self):
        raw = "Here is the fix:\n```json\n" + _fix_response() + "\n```\nDone."
        result = parse_implement_response(raw)
        assert result["root_cause"] == "Nil pointer dereference when owner ref is nil"

    def test_generic_code_block(self):
        raw = "Fix:\n```\n" + _fix_response() + "\n```"
        result = parse_implement_response(raw)
        assert result["confidence"] == 0.9

    def test_malformed_returns_default(self):
        result = parse_implement_response("This is not JSON at all.")
        assert result["root_cause"] == "unknown"
        assert result["confidence"] == 0.0
        assert "Failed to parse" in result["fix_description"]

    def test_empty_string(self):
        result = parse_implement_response("")
        assert result["root_cause"] == "unknown"
        assert result["confidence"] == 0.0

    def test_partial_json(self):
        result = parse_implement_response('{"root_cause": "test"')
        assert result["root_cause"] == "unknown"

    def test_multiple_code_blocks_picks_valid(self):
        raw = "```\nnot json\n```\n\n```json\n" + _fix_response() + "\n```"
        result = parse_implement_response(raw)
        assert result["root_cause"] == "Nil pointer dereference when owner ref is nil"


# ------------------------------------------------------------------
# ImplementPhase.observe tests
# ------------------------------------------------------------------


class TestImplementObserve:
    @pytest.mark.asyncio
    async def test_observe_without_tools(self):
        phase = _make_implement(tool_executor=None)
        obs = await phase.observe()
        assert obs["issue"]["url"] == "https://github.com/test/repo/issues/42"
        assert obs["file_contents"] == {}
        assert obs["repo_structure"] == ""

    @pytest.mark.asyncio
    async def test_observe_extracts_triage_report(self):
        phase = _make_implement(prior_results=[_triage_phase_result()])
        obs = await phase.observe()
        assert obs["triage_report"]["classification"] == "bug"
        assert "pkg/controller/reconciler.go" in obs["affected_components"]

    @pytest.mark.asyncio
    async def test_observe_without_triage_result(self):
        phase = _make_implement(prior_results=[])
        obs = await phase.observe()
        assert obs["triage_report"] == {}
        assert obs["affected_components"] == []

    @pytest.mark.asyncio
    async def test_observe_reads_affected_files(self, tmp_path):
        phase = _make_implement_with_repo(tmp_path)
        obs = await phase.observe()
        assert "pkg/controller/reconciler.go" in obs["file_contents"]
        assert "package controller" in obs["file_contents"]["pkg/controller/reconciler.go"]

    @pytest.mark.asyncio
    async def test_observe_gets_repo_structure(self, tmp_path):
        phase = _make_implement_with_repo(tmp_path)
        obs = await phase.observe()
        assert "reconciler.go" in obs["repo_structure"]


# ------------------------------------------------------------------
# ImplementPhase.plan tests
# ------------------------------------------------------------------


class TestImplementPlan:
    @pytest.mark.asyncio
    async def test_plan_calls_llm(self):
        phase = _make_implement()
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["root_cause"] == "Nil pointer dereference when owner ref is nil"
        assert "raw_llm_response" in plan

    @pytest.mark.asyncio
    async def test_plan_records_llm_call(self):
        tracer = Tracer()
        phase = ImplementPhase(
            llm=MockProvider(responses=[_fix_response()]),
            logger=StructuredLogger(),
            tracer=tracer,
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        llm_actions = [a for a in tracer.get_actions() if a.action_type == "llm_query"]
        assert len(llm_actions) == 1
        assert llm_actions[0].llm_context["provider"] == "mock"

    @pytest.mark.asyncio
    async def test_plan_wraps_untrusted_content(self):
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "untrusted body"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        call = llm.call_log[0]
        msg = call["messages"][0]["content"]
        assert "UNTRUSTED CONTENT BELOW" in msg
        assert "END UNTRUSTED CONTENT" in msg
        assert "untrusted body" in msg

    @pytest.mark.asyncio
    async def test_plan_includes_triage_context(self):
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "Triage summary" in msg
        assert "bug" in msg
        assert "verify independently" in msg


# ------------------------------------------------------------------
# ImplementPhase.act tests
# ------------------------------------------------------------------


class TestImplementAct:
    @pytest.mark.asyncio
    async def test_act_writes_files(self, tmp_path):
        phase = _make_implement_with_repo(tmp_path)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert len(result["files_written"]) >= 1
        assert "pkg/controller/reconciler.go" in result["files_written"]

    @pytest.mark.asyncio
    async def test_act_without_tools(self):
        phase = _make_implement(tool_executor=None)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["files_written"] == []

    @pytest.mark.asyncio
    async def test_act_records_actions(self, tmp_path):
        phase = _make_implement_with_repo(tmp_path)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        write_actions = [a for a in result["actions"] if a["action"] == "file_write"]
        assert len(write_actions) >= 1

    @pytest.mark.asyncio
    async def test_act_test_skipped_when_disabled(self, tmp_path):
        cfg = EngineConfig()
        cfg.phases.implement.run_tests_after_each_edit = False
        phase = _make_implement_with_repo(tmp_path, config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["test_result"]["passed"] is True
        assert "skipped" in result["test_result"]["output"].lower()

    @pytest.mark.asyncio
    async def test_act_linter_skipped_when_disabled(self, tmp_path):
        cfg = EngineConfig()
        cfg.phases.implement.run_linters = False
        phase = _make_implement_with_repo(tmp_path, config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["lint_result"]["passed"] is True
        assert "skipped" in result["lint_result"]["output"].lower()

    @pytest.mark.asyncio
    async def test_act_no_file_changes_in_plan(self):
        phase = _make_implement(
            responses=[_fix_response_no_changes()],
            tool_executor=None,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["files_written"] == []


# ------------------------------------------------------------------
# ImplementPhase.validate tests
# ------------------------------------------------------------------


class TestImplementValidate:
    @pytest.mark.asyncio
    async def test_validate_all_passing(self):
        phase = _make_implement()
        validation = await phase.validate(
            {
                "test_result": {"passed": True, "output": "3 passed"},
                "lint_result": {"passed": True, "output": "OK"},
                "files_written": ["reconciler.go"],
                "diff": "--- a/reconciler.go\n+++ b/reconciler.go\n@@ ...",
                "impl_plan": json.loads(_fix_response()),
            }
        )
        assert validation["valid"] is True
        assert validation["issues"] == []

    @pytest.mark.asyncio
    async def test_validate_tests_failing(self):
        phase = _make_implement()
        validation = await phase.validate(
            {
                "test_result": {"passed": False, "output": "FAIL test_foo"},
                "lint_result": {"passed": True, "output": "OK"},
                "files_written": ["reconciler.go"],
                "diff": "some diff",
                "impl_plan": {},
            }
        )
        assert validation["valid"] is False
        assert any("Tests failing" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_linters_failing(self):
        phase = _make_implement()
        validation = await phase.validate(
            {
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": False, "output": "E501 line too long"},
                "files_written": ["reconciler.go"],
                "diff": "some diff",
                "impl_plan": {},
            }
        )
        assert validation["valid"] is False
        assert any("Linters failing" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_no_files_modified(self):
        phase = _make_implement()
        validation = await phase.validate(
            {
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "files_written": [],
                "diff": "",
                "impl_plan": {},
            }
        )
        assert validation["valid"] is False
        assert any("No files were modified" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_no_diff_despite_writes(self):
        phase = _make_implement()
        validation = await phase.validate(
            {
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "files_written": ["reconciler.go"],
                "diff": "",
                "impl_plan": {},
            }
        )
        assert validation["valid"] is False
        assert any("No git diff" in i for i in validation["issues"])


# ------------------------------------------------------------------
# ImplementPhase.reflect tests
# ------------------------------------------------------------------


class TestImplementReflect:
    @pytest.mark.asyncio
    async def test_valid_proceeds_to_review(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": True,
                "issues": [],
                "tests_passing": True,
                "linters_passing": True,
                "files_changed": ["reconciler.go"],
                "diff": "some diff",
                "inner_iterations_used": 0,
                "impl_plan": json.loads(_fix_response()),
            }
        )
        assert result.success is True
        assert result.next_phase == "review"
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_invalid_retries(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Tests failing"],
                "tests_passing": False,
                "linters_passing": False,
                "impl_plan": {},
            }
        )
        assert result.success is False
        assert result.should_continue is True
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_partial_failure_retries(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Linters failing"],
                "tests_passing": True,
                "linters_passing": False,
                "impl_plan": {},
            }
        )
        assert result.success is False
        assert result.should_continue is True

    @pytest.mark.asyncio
    async def test_success_artifacts_populated(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": True,
                "issues": [],
                "tests_passing": True,
                "linters_passing": True,
                "files_changed": ["a.go", "b.go"],
                "diff": "diff content",
                "inner_iterations_used": 2,
                "impl_plan": {"root_cause": "test"},
            }
        )
        assert result.artifacts["files_changed"] == ["a.go", "b.go"]
        assert result.artifacts["diff"] == "diff content"
        assert result.artifacts["inner_iterations_used"] == 2


# ------------------------------------------------------------------
# Full execute lifecycle tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_success_no_tools():
    """Execute() without tool executor — files_written empty, validation fails, retries."""
    phase = _make_implement(tool_executor=None)
    result = await phase.execute()
    assert result.success is False
    assert result.should_continue is True


@pytest.mark.asyncio
async def test_execute_with_real_repo(tmp_path):
    """Full execute() against a temp repo with real ToolExecutor."""
    phase = _make_implement_with_repo(tmp_path)
    result = await phase.execute()
    assert result.phase == "implement"
    assert isinstance(result.findings, dict)


@pytest.mark.asyncio
async def test_execute_no_triage_prior():
    """Execute() when there are no prior triage results — still runs."""
    phase = _make_implement(prior_results=[], tool_executor=None)
    result = await phase.execute()
    assert result.phase == "implement"


@pytest.mark.asyncio
async def test_execute_malformed_llm_response():
    """Execute() with unparseable LLM output — returns retry."""
    phase = _make_implement(
        responses=["This is not valid JSON, sorry!"],
        tool_executor=None,
    )
    result = await phase.execute()
    assert result.success is False
    assert result.should_continue is True


# ------------------------------------------------------------------
# Inner iteration tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inner_iteration_limit_respected():
    """The inner loop does not exceed max_inner_iterations."""
    cfg = EngineConfig()
    cfg.phases.implement.max_inner_iterations = 2
    cfg.phases.implement.run_tests_after_each_edit = False
    cfg.phases.implement.run_linters = False

    phase = _make_implement(config=cfg, tool_executor=None)
    obs = await phase.observe()
    plan = await phase.plan(obs)
    result = await phase.act(plan)
    assert result["inner_iterations"] <= cfg.phases.implement.max_inner_iterations


@pytest.mark.asyncio
async def test_inner_iteration_zero_when_passing():
    """No inner iterations needed when tests+linters pass on first try."""
    cfg = EngineConfig()
    cfg.phases.implement.run_tests_after_each_edit = False
    cfg.phases.implement.run_linters = False

    phase = _make_implement(config=cfg, tool_executor=None)
    obs = await phase.observe()
    plan = await phase.plan(obs)
    result = await phase.act(plan)
    assert result["inner_iterations"] == 0


# ------------------------------------------------------------------
# Triage extraction tests
# ------------------------------------------------------------------


class TestTriageExtraction:
    def test_extract_from_artifacts(self):
        phase = _make_implement(prior_results=[_triage_phase_result()])
        report = phase._extract_triage_report()
        assert report["classification"] == "bug"

    def test_extract_from_findings_fallback(self):
        result = PhaseResult(
            phase="triage",
            success=True,
            findings={"classification": "bug", "severity": "high"},
        )
        phase = _make_implement(prior_results=[result])
        report = phase._extract_triage_report()
        assert report["classification"] == "bug"

    def test_extract_empty_when_no_triage(self):
        phase = _make_implement(prior_results=[])
        report = phase._extract_triage_report()
        assert report == {}

    def test_extract_skips_failed_triage(self):
        failed = PhaseResult(phase="triage", success=False, findings={"bad": True})
        phase = _make_implement(prior_results=[failed])
        report = phase._extract_triage_report()
        assert report == {}


# ------------------------------------------------------------------
# Review feedback extraction tests
# ------------------------------------------------------------------


def _review_phase_result(
    verdict: str = "request_changes",
    findings: list[dict[str, Any]] | None = None,
    summary: str = "Fix has a correctness issue.",
) -> PhaseResult:
    """Create a mock review PhaseResult with request_changes verdict."""
    review_data = {
        "verdict": verdict,
        "findings": findings
        or [
            {
                "dimension": "correctness",
                "severity": "blocking",
                "file": "pkg/controller/reconciler.go",
                "line": 22,
                "description": "The nil check is in the wrong location",
                "suggestion": "Move the nil check before the dereference on line 22",
            }
        ],
        "scope_assessment": "bug_fix",
        "injection_detected": False,
        "confidence": 0.8,
        "summary": summary,
    }
    return PhaseResult(
        phase="review",
        success=False,
        should_continue=True,
        next_phase="implement",
        findings=review_data,
        artifacts={"review_report": review_data},
    )


class TestReviewFeedbackExtraction:
    def test_extract_from_artifacts(self):
        phase = _make_implement(prior_results=[_triage_phase_result(), _review_phase_result()])
        fb = phase._extract_review_feedback()
        assert fb["verdict"] == "request_changes"
        assert len(fb["findings"]) == 1
        assert fb["summary"] == "Fix has a correctness issue."

    def test_extract_from_findings_fallback(self):
        result = PhaseResult(
            phase="review",
            success=False,
            findings={
                "verdict": "request_changes",
                "findings": [{"dimension": "style", "severity": "nit"}],
                "summary": "Style issues",
            },
        )
        phase = _make_implement(prior_results=[_triage_phase_result(), result])
        fb = phase._extract_review_feedback()
        assert fb["verdict"] == "request_changes"
        assert len(fb["findings"]) == 1

    def test_extract_empty_when_no_review(self):
        phase = _make_implement(prior_results=[_triage_phase_result()])
        fb = phase._extract_review_feedback()
        assert fb == {}

    def test_extract_empty_when_no_prior_results(self):
        phase = _make_implement(prior_results=[])
        fb = phase._extract_review_feedback()
        assert fb == {}

    def test_extract_picks_latest_review(self):
        review1 = PhaseResult(
            phase="review",
            success=False,
            findings={"verdict": "request_changes", "summary": "old", "findings": []},
            artifacts={
                "review_report": {
                    "verdict": "request_changes",
                    "summary": "old",
                    "findings": [],
                }
            },
        )
        review2 = PhaseResult(
            phase="review",
            success=False,
            findings={
                "verdict": "request_changes",
                "summary": "latest",
                "findings": [],
            },
            artifacts={
                "review_report": {
                    "verdict": "request_changes",
                    "summary": "latest",
                    "findings": [],
                }
            },
        )
        phase = _make_implement(prior_results=[_triage_phase_result(), review1, review2])
        fb = phase._extract_review_feedback()
        assert fb["summary"] == "latest"

    def test_extract_skips_non_review_phases(self):
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            findings={"root_cause": "test"},
            artifacts={"diff": "x"},
        )
        phase = _make_implement(prior_results=[_triage_phase_result(), impl_result])
        fb = phase._extract_review_feedback()
        assert fb == {}

    def test_extract_includes_scope_assessment(self):
        phase = _make_implement(prior_results=[_triage_phase_result(), _review_phase_result()])
        fb = phase._extract_review_feedback()
        assert fb["scope_assessment"] == "bug_fix"

    def test_extract_skips_review_with_empty_findings_and_artifacts(self):
        empty_review = PhaseResult(phase="review", success=False)
        phase = _make_implement(prior_results=[_triage_phase_result(), empty_review])
        fb = phase._extract_review_feedback()
        assert fb == {}


# ------------------------------------------------------------------
# _format_review_feedback tests
# ------------------------------------------------------------------


class TestFormatReviewFeedback:
    def test_basic_format(self):
        fb = {
            "verdict": "request_changes",
            "summary": "Fix has issues.",
            "findings": [],
        }
        text = _format_review_feedback(fb)
        assert "PREVIOUS REVIEW FEEDBACK" in text
        assert "request_changes" in text
        assert "Fix has issues." in text

    def test_format_with_findings(self):
        fb = {
            "verdict": "request_changes",
            "summary": "Correctness issue.",
            "findings": [
                {
                    "dimension": "correctness",
                    "severity": "blocking",
                    "file": "reconciler.go",
                    "line": 22,
                    "description": "Nil check in wrong location",
                    "suggestion": "Move check before line 22",
                }
            ],
        }
        text = _format_review_feedback(fb)
        assert "Findings (1):" in text
        assert "[correctness/blocking]" in text
        assert "Nil check in wrong location" in text
        assert "Location: reconciler.go:22" in text
        assert "Suggestion: Move check before line 22" in text

    def test_format_without_location(self):
        fb = {
            "verdict": "request_changes",
            "summary": "Issue.",
            "findings": [
                {
                    "dimension": "style",
                    "severity": "nit",
                    "description": "Use consistent naming",
                    "suggestion": "Rename variable",
                }
            ],
        }
        text = _format_review_feedback(fb)
        assert "Location:" not in text
        assert "Suggestion: Rename variable" in text

    def test_format_multiple_findings(self):
        fb = {
            "verdict": "request_changes",
            "summary": "Multiple issues.",
            "findings": [
                {"dimension": "correctness", "severity": "blocking", "description": "Issue A"},
                {"dimension": "style", "severity": "nit", "description": "Issue B"},
                {"dimension": "security", "severity": "suggestion", "description": "Issue C"},
            ],
        }
        text = _format_review_feedback(fb)
        assert "Findings (3):" in text
        assert "1. [correctness/blocking] Issue A" in text
        assert "2. [style/nit] Issue B" in text
        assert "3. [security/suggestion] Issue C" in text

    def test_format_empty_findings(self):
        fb = {"verdict": "request_changes", "summary": "Minor.", "findings": []}
        text = _format_review_feedback(fb)
        assert "Findings" not in text
        assert "PREVIOUS REVIEW FEEDBACK" in text

    def test_format_caps_at_10_findings(self):
        fb = {
            "verdict": "request_changes",
            "summary": "Many.",
            "findings": [
                {"dimension": "style", "severity": "nit", "description": f"Issue {i}"}
                for i in range(15)
            ],
        }
        text = _format_review_feedback(fb)
        assert "10. " in text
        assert "11. " not in text


# ------------------------------------------------------------------
# Review feedback in observe/plan tests
# ------------------------------------------------------------------


class TestReviewFeedbackInPipeline:
    @pytest.mark.asyncio
    async def test_observe_includes_review_feedback(self):
        phase = _make_implement(
            prior_results=[_triage_phase_result(), _review_phase_result()],
            tool_executor=None,
        )
        obs = await phase.observe()
        assert "review_feedback" in obs
        assert obs["review_feedback"]["verdict"] == "request_changes"
        assert len(obs["review_feedback"]["findings"]) == 1

    @pytest.mark.asyncio
    async def test_observe_empty_review_feedback_when_no_review(self):
        phase = _make_implement(
            prior_results=[_triage_phase_result()],
            tool_executor=None,
        )
        obs = await phase.observe()
        assert obs["review_feedback"] == {}

    @pytest.mark.asyncio
    async def test_plan_includes_review_feedback_in_llm_context(self):
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), _review_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "PREVIOUS REVIEW FEEDBACK" in msg
        assert "request_changes" in msg
        assert "nil check is in the wrong location" in msg.lower()
        assert "Move the nil check before the dereference" in msg

    @pytest.mark.asyncio
    async def test_plan_omits_review_feedback_when_no_review(self):
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "PREVIOUS REVIEW FEEDBACK" not in msg

    @pytest.mark.asyncio
    async def test_plan_includes_review_suggestion_in_context(self):
        review = _review_phase_result(
            findings=[
                {
                    "dimension": "correctness",
                    "severity": "blocking",
                    "file": "pkg/controller/reconciler.go",
                    "line": 42,
                    "description": "Wrong approach — this downgrades the version",
                    "suggestion": "Keep the existing version and fix the nil check",
                }
            ],
            summary="Fix uses wrong approach.",
        )
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), review],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "Keep the existing version" in msg
        assert "Location: pkg/controller/reconciler.go:42" in msg
        assert "Fix uses wrong approach." in msg

    @pytest.mark.asyncio
    async def test_plan_review_feedback_is_in_trusted_context(self):
        """Review feedback must appear in trusted context, not inside untrusted delimiters."""
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), _review_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        untrusted_start = msg.index("UNTRUSTED CONTENT BELOW")
        feedback_pos = msg.index("PREVIOUS REVIEW FEEDBACK")
        assert feedback_pos < untrusted_start


# ------------------------------------------------------------------
# Integration with loop
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_implement_phase_in_loop(tmp_path):
    """ImplementPhase can be registered and executed in RalphLoop."""
    from engine.loop import RalphLoop
    from engine.phases.triage import TriagePhase

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg" / "controller").mkdir(parents=True)
    (repo / "pkg" / "controller" / "reconciler.go").write_text("package controller\n")
    out = tmp_path / "output"
    out.mkdir()

    triage_resp = json.dumps(
        {
            "classification": "bug",
            "confidence": 0.9,
            "severity": "high",
            "affected_components": ["pkg/controller/reconciler.go"],
            "reproduction": {
                "existing_tests": [],
                "can_reproduce": False,
                "reproduction_steps": "",
            },
            "injection_detected": False,
            "recommendation": "proceed",
            "reasoning": "Nil pointer bug",
        }
    )
    impl_resp = _fix_response()

    loop = RalphLoop(
        config=EngineConfig(),
        llm=MockProvider(responses=[triage_resp, impl_resp]),
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(repo),
        output_dir=str(out),
    )
    loop.register_phase("triage", TriagePhase)
    loop.register_phase("implement", ImplementPhase)

    execution = await loop.run()
    phases_run = [it["phase"] for it in execution.iterations]
    assert "triage" in phases_run
    assert "implement" in phases_run


# ------------------------------------------------------------------
# ClassVar and config tests
# ------------------------------------------------------------------


class TestImplementClassProperties:
    def test_name(self):
        assert ImplementPhase.name == "implement"

    def test_allowed_tools_falls_back_to_implement_set(self):
        tools = ImplementPhase.get_allowed_tools()
        assert "file_read" in tools
        assert "file_write" in tools
        assert "shell_run" in tools
        assert "git_diff" in tools
        assert "git_commit" in tools
        assert "github_api" not in tools

    def test_config_implement_flags(self):
        cfg = EngineConfig()
        assert cfg.phases.implement.max_inner_iterations == 5
        assert cfg.phases.implement.run_tests_after_each_edit is True
        assert cfg.phases.implement.run_linters is True


# ------------------------------------------------------------------
# Failed implement result helper
# ------------------------------------------------------------------


def _failed_implement_result(
    approach: str = "Added nil check",
    root_cause: str = "Nil pointer dereference",
    files_changed: list[str] | None = None,
    validation_issues: list[str] | None = None,
) -> PhaseResult:
    """Create a mock failed implement PhaseResult for retry testing."""
    return PhaseResult(
        phase="implement",
        success=False,
        should_continue=True,
        findings={
            "validation_issues": validation_issues or ["No files were modified"],
            "impl_plan": {
                "root_cause": root_cause,
                "fix_description": approach,
                "files_changed": files_changed or [],
                "file_changes": [],
            },
        },
        artifacts={
            "files_changed": files_changed or [],
        },
    )


# ------------------------------------------------------------------
# _extract_keywords tests
# ------------------------------------------------------------------


class TestExtractKeywords:
    def test_basic_title_keywords(self):
        kws = _extract_keywords("nil pointer panic in reconciler", "", min_len=4)
        assert "pointer" in kws
        assert "panic" in kws
        assert "reconciler" in kws

    def test_filters_short_words(self):
        kws = _extract_keywords("a bug in the controller", "", min_len=5)
        assert "controller" in kws
        assert "bug" not in kws
        assert "the" not in kws
        assert "in" not in kws

    def test_filters_stopwords(self):
        kws = _extract_keywords("error when running between phases", "", min_len=4)
        lower_kws = [k.lower() for k in kws]
        assert "when" not in lower_kws
        assert "between" not in lower_kws
        assert "running" in lower_kws
        assert "phases" in lower_kws

    def test_filters_na(self):
        kws = _extract_keywords("N/A", "N/A content here", min_len=3)
        lower_kws = [k.lower() for k in kws]
        assert "n/a" not in lower_kws

    def test_deduplicates(self):
        kws = _extract_keywords("reconciler reconciler Reconciler", "", min_len=4)
        assert len([k for k in kws if k.lower() == "reconciler"]) == 1

    def test_max_keywords_limit(self):
        title = " ".join(f"keyword{i}" for i in range(20))
        kws = _extract_keywords(title, "", min_len=4, max_keywords=5)
        assert len(kws) == 5

    def test_falls_back_to_body(self):
        kws = _extract_keywords("bug", "The fbc-fips-check-oci component crashes", min_len=4)
        lower_kws = [k.lower() for k in kws]
        found = (
            "fbc-fips-check-oci" in lower_kws or "component" in lower_kws or "crashes" in lower_kws
        )
        assert found

    def test_empty_inputs(self):
        kws = _extract_keywords("", "", min_len=4)
        assert kws == []

    def test_strips_punctuation(self):
        kws = _extract_keywords('"reconciler" (panics!)', "", min_len=4)
        assert "reconciler" in kws
        assert "panics" in kws


# ------------------------------------------------------------------
# _collect_previously_tried_files tests
# ------------------------------------------------------------------


class TestCollectPreviouslyTriedFiles:
    def test_empty_context(self):
        assert _collect_previously_tried_files([]) == set()

    def test_collects_files(self):
        ctx = [
            {"attempt": 1, "files_attempted": ["a.go", "b.go"]},
            {"attempt": 2, "files_attempted": ["c.go"]},
        ]
        result = _collect_previously_tried_files(ctx)
        assert result == {"a.go", "b.go", "c.go"}

    def test_deduplicates(self):
        ctx = [
            {"attempt": 1, "files_attempted": ["a.go"]},
            {"attempt": 2, "files_attempted": ["a.go", "b.go"]},
        ]
        result = _collect_previously_tried_files(ctx)
        assert result == {"a.go", "b.go"}

    def test_skips_empty_strings(self):
        ctx = [{"attempt": 1, "files_attempted": ["", "a.go", ""]}]
        result = _collect_previously_tried_files(ctx)
        assert result == {"a.go"}

    def test_handles_missing_key(self):
        ctx = [{"attempt": 1}]
        result = _collect_previously_tried_files(ctx)
        assert result == set()


# ------------------------------------------------------------------
# _format_retry_context tests
# ------------------------------------------------------------------


class TestFormatRetryContext:
    def test_empty_retries(self):
        assert _format_retry_context([]) == ""

    def test_single_retry(self):
        retries = [
            {
                "attempt": 1,
                "approach": "Added nil check",
                "root_cause_guess": "Nil pointer",
                "files_attempted": ["reconciler.go"],
                "validation_issues": ["Tests failing"],
            }
        ]
        text = _format_retry_context(retries)
        assert "PRIOR IMPLEMENTATION ATTEMPTS (1 failed" in text
        assert "Attempt 1:" in text
        assert "Added nil check" in text
        assert "Nil pointer" in text
        assert "reconciler.go" in text
        assert "Tests failing" in text
        assert "MUST try a different approach" in text

    def test_multiple_retries(self):
        retries = [
            {
                "attempt": 1,
                "approach": "Approach A",
                "root_cause_guess": "",
                "files_attempted": [],
                "validation_issues": ["No files were modified"],
            },
            {
                "attempt": 2,
                "approach": "Approach B",
                "root_cause_guess": "Wrong root cause",
                "files_attempted": ["a.go"],
                "validation_issues": ["Tests failing"],
            },
        ]
        text = _format_retry_context(retries)
        assert "2 failed" in text
        assert "Attempt 1:" in text
        assert "Attempt 2:" in text
        assert "Approach A" in text
        assert "Approach B" in text
        assert "NONE (no files were changed)" in text

    def test_truncates_long_approach(self):
        retries = [
            {
                "attempt": 1,
                "approach": "x" * 500,
                "root_cause_guess": "",
                "files_attempted": [],
                "validation_issues": [],
            }
        ]
        text = _format_retry_context(retries)
        assert len(text) < 600

    def test_no_approach_or_root_cause(self):
        retries = [
            {
                "attempt": 1,
                "approach": "",
                "root_cause_guess": "",
                "files_attempted": [],
                "validation_issues": [],
            }
        ]
        text = _format_retry_context(retries)
        assert "PRIOR IMPLEMENTATION ATTEMPTS" in text
        assert "Approach tried:" not in text
        assert "Root cause guess:" not in text

    def test_caps_validation_issues_at_5(self):
        retries = [
            {
                "attempt": 1,
                "approach": "test",
                "root_cause_guess": "",
                "files_attempted": [],
                "validation_issues": [f"Issue {i}" for i in range(10)],
            }
        ]
        text = _format_retry_context(retries)
        assert "Issue 4" in text
        assert "Issue 5" not in text


# ------------------------------------------------------------------
# _extract_retry_context tests
# ------------------------------------------------------------------


class TestExtractRetryContext:
    def test_no_prior_implement_results(self):
        phase = _make_implement(prior_results=[_triage_phase_result()])
        ctx = phase._extract_retry_context()
        assert ctx == []

    def test_skips_successful_implement(self):
        success = PhaseResult(
            phase="implement",
            success=True,
            findings={"impl_plan": {"fix_description": "good fix"}},
        )
        phase = _make_implement(prior_results=[_triage_phase_result(), success])
        ctx = phase._extract_retry_context()
        assert ctx == []

    def test_extracts_single_failure(self):
        failed = _failed_implement_result(
            approach="Wrong approach",
            files_changed=["a.go"],
            validation_issues=["Tests failing"],
        )
        phase = _make_implement(prior_results=[_triage_phase_result(), failed])
        ctx = phase._extract_retry_context()
        assert len(ctx) == 1
        assert ctx[0]["attempt"] == 1
        assert ctx[0]["approach"] == "Wrong approach"
        assert ctx[0]["files_attempted"] == ["a.go"]
        assert ctx[0]["validation_issues"] == ["Tests failing"]

    def test_extracts_multiple_failures(self):
        f1 = _failed_implement_result(approach="Approach A")
        f2 = _failed_implement_result(approach="Approach B", files_changed=["b.go"])
        phase = _make_implement(prior_results=[_triage_phase_result(), f1, f2])
        ctx = phase._extract_retry_context()
        assert len(ctx) == 2
        assert ctx[0]["attempt"] == 1
        assert ctx[1]["attempt"] == 2
        assert ctx[0]["approach"] == "Approach A"
        assert ctx[1]["approach"] == "Approach B"

    def test_skips_non_implement_phases(self):
        review = _review_phase_result()
        failed = _failed_implement_result(approach="Test")
        phase = _make_implement(prior_results=[_triage_phase_result(), review, failed])
        ctx = phase._extract_retry_context()
        assert len(ctx) == 1
        assert ctx[0]["approach"] == "Test"

    def test_files_from_artifacts(self):
        failed = PhaseResult(
            phase="implement",
            success=False,
            should_continue=True,
            findings={
                "validation_issues": ["Tests failing"],
                "impl_plan": {"fix_description": "fix", "root_cause": "bug"},
            },
            artifacts={"files_changed": ["x.py", "y.py"]},
        )
        phase = _make_implement(prior_results=[_triage_phase_result(), failed])
        ctx = phase._extract_retry_context()
        assert ctx[0]["files_attempted"] == ["x.py", "y.py"]

    def test_files_fallback_to_findings(self):
        failed = PhaseResult(
            phase="implement",
            success=False,
            should_continue=True,
            findings={
                "validation_issues": [],
                "impl_plan": {
                    "fix_description": "fix",
                    "root_cause": "bug",
                    "files_changed": ["z.go"],
                },
            },
        )
        phase = _make_implement(prior_results=[_triage_phase_result(), failed])
        ctx = phase._extract_retry_context()
        assert ctx[0]["files_attempted"] == ["z.go"]


# ------------------------------------------------------------------
# Retry context in observe/plan pipeline tests
# ------------------------------------------------------------------


class TestRetryContextInPipeline:
    @pytest.mark.asyncio
    async def test_observe_includes_retry_context(self):
        failed = _failed_implement_result(approach="Bad approach")
        phase = _make_implement(
            prior_results=[_triage_phase_result(), failed],
            tool_executor=None,
        )
        obs = await phase.observe()
        assert "retry_context" in obs
        assert len(obs["retry_context"]) == 1
        assert obs["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_observe_empty_retry_context_on_first_attempt(self):
        phase = _make_implement(
            prior_results=[_triage_phase_result()],
            tool_executor=None,
        )
        obs = await phase.observe()
        assert obs["retry_context"] == []
        assert obs["retry_count"] == 0

    @pytest.mark.asyncio
    async def test_plan_includes_retry_context_in_llm(self):
        failed = _failed_implement_result(
            approach="Wrong nil check location",
            validation_issues=["No files were modified"],
        )
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), failed],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "PRIOR IMPLEMENTATION ATTEMPTS" in msg
        assert "Wrong nil check location" in msg
        assert "No files were modified" in msg
        assert "MUST try a different approach" in msg

    @pytest.mark.asyncio
    async def test_plan_no_retry_context_on_first_attempt(self):
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "PRIOR IMPLEMENTATION ATTEMPTS" not in msg

    @pytest.mark.asyncio
    async def test_plan_retry_context_in_trusted_not_untrusted(self):
        failed = _failed_implement_result(approach="Bad fix")
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), failed],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        untrusted_start = msg.index("UNTRUSTED CONTENT BELOW")
        retry_pos = msg.index("PRIOR IMPLEMENTATION ATTEMPTS")
        assert retry_pos < untrusted_start

    @pytest.mark.asyncio
    async def test_plan_retry_context_with_review_feedback(self):
        """Both retry context and review feedback present in LLM prompt."""
        failed = _failed_implement_result(approach="First attempt")
        review = _review_phase_result(summary="Fix has scope issues")
        llm = MockProvider(responses=[_fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), failed, review],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "PRIOR IMPLEMENTATION ATTEMPTS" in msg
        assert "PREVIOUS REVIEW FEEDBACK" in msg
        assert "First attempt" in msg
        assert "scope issues" in msg


# ------------------------------------------------------------------
# Reflect includes retry metadata tests
# ------------------------------------------------------------------


class TestReflectRetryMetadata:
    @pytest.mark.asyncio
    async def test_failure_includes_files_changed_in_artifacts(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Tests failing"],
                "tests_passing": False,
                "linters_passing": True,
                "files_changed": ["a.go", "b.go"],
                "impl_plan": {"root_cause": "test", "fix_description": "test fix"},
            }
        )
        assert result.success is False
        assert result.artifacts.get("files_changed") == ["a.go", "b.go"]

    @pytest.mark.asyncio
    async def test_failure_includes_impl_plan_in_findings(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["No files were modified"],
                "tests_passing": True,
                "linters_passing": True,
                "files_changed": [],
                "impl_plan": {"root_cause": "nil ptr", "fix_description": "add check"},
            }
        )
        assert result.findings["impl_plan"]["root_cause"] == "nil ptr"
        assert result.findings["impl_plan"]["fix_description"] == "add check"
        assert result.findings["validation_issues"] == ["No files were modified"]

    @pytest.mark.asyncio
    async def test_failure_empty_files_changed(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["No files were modified"],
                "tests_passing": True,
                "linters_passing": True,
                "files_changed": [],
                "impl_plan": {},
            }
        )
        assert result.artifacts.get("files_changed") == []

    @pytest.mark.asyncio
    async def test_success_still_has_artifacts(self):
        phase = _make_implement()
        result = await phase.reflect(
            {
                "valid": True,
                "issues": [],
                "tests_passing": True,
                "linters_passing": True,
                "files_changed": ["x.go"],
                "diff": "some diff",
                "inner_iterations_used": 1,
                "impl_plan": {"root_cause": "test"},
            }
        )
        assert result.success is True
        assert result.artifacts["files_changed"] == ["x.go"]


# ------------------------------------------------------------------
# Adaptive search strategy tests
# ------------------------------------------------------------------


class TestAdaptiveSearchStrategy:
    @pytest.mark.asyncio
    async def test_broad_file_scan_with_repo(self, tmp_path):
        """_broad_file_scan reads source files from the repo."""
        phase = _make_implement_with_repo(tmp_path)
        result = await phase._broad_file_scan()
        assert len(result) >= 1
        paths = list(result.keys())
        assert any("main.go" in p or "reconciler.go" in p for p in paths)

    @pytest.mark.asyncio
    async def test_broad_file_scan_excludes_files(self, tmp_path):
        """_broad_file_scan respects the exclude set."""
        phase = _make_implement_with_repo(tmp_path)
        result = await phase._broad_file_scan(exclude={"./main.go"})
        paths = list(result.keys())
        assert "./main.go" not in paths

    @pytest.mark.asyncio
    async def test_search_escalates_to_broad_on_retry_2(self, tmp_path):
        """On retry_count >= 2, _search_relevant_files goes directly to broad scan."""
        phase = _make_implement_with_repo(tmp_path)
        result = await phase._search_relevant_files(retry_count=2)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_search_escalates_when_no_keywords(self):
        """When issue has no useful keywords, _extract_keywords returns empty."""
        kws = _extract_keywords("N/A", "N/A", min_len=4)
        assert kws == []
        kws2 = _extract_keywords("bug", "the issue is bad", min_len=5)
        assert kws2 == []


# ------------------------------------------------------------------
# is_parse_failure tests
# ------------------------------------------------------------------


class TestIsParseFailure:
    def test_true_for_default_dict(self):
        plan = parse_implement_response("not json at all")
        assert is_parse_failure(plan)

    def test_false_for_valid_response(self):
        plan = json.loads(_fix_response())
        assert not is_parse_failure(plan)

    def test_false_for_empty_file_changes(self):
        plan = json.loads(_fix_response_no_changes())
        assert not is_parse_failure(plan)

    def test_false_when_root_cause_set(self):
        plan = {
            "root_cause": "nil pointer",
            "confidence": 0.0,
            "fix_description": "Failed to parse LLM response. Raw: x",
        }
        assert not is_parse_failure(plan)

    def test_false_when_confidence_nonzero(self):
        plan = {
            "root_cause": "unknown",
            "confidence": 0.5,
            "fix_description": "Failed to parse LLM response. Raw: x",
        }
        assert not is_parse_failure(plan)


# ------------------------------------------------------------------
# validate_impl_plan tests
# ------------------------------------------------------------------


class TestValidateImplPlan:
    def test_valid_plan(self):
        plan = json.loads(_fix_response())
        issues = validate_impl_plan(plan)
        assert issues == []

    def test_parse_failure(self):
        plan = parse_implement_response("not json")
        issues = validate_impl_plan(plan)
        assert len(issues) == 1
        assert "parse failure" in issues[0].lower()

    def test_empty_file_changes(self):
        plan = json.loads(_fix_response_no_changes())
        issues = validate_impl_plan(plan)
        assert len(issues) == 1
        assert "empty or missing" in issues[0].lower()

    def test_missing_file_changes_key(self):
        plan = {"root_cause": "test", "confidence": 0.5}
        issues = validate_impl_plan(plan)
        assert len(issues) >= 1
        assert "empty or missing" in issues[0].lower()

    def test_file_changes_not_list(self):
        plan = {"root_cause": "test", "confidence": 0.5, "file_changes": "oops"}
        issues = validate_impl_plan(plan)
        assert len(issues) >= 1

    def test_entry_missing_path(self):
        plan = {
            "root_cause": "test",
            "confidence": 0.5,
            "file_changes": [{"content": "data"}],
        }
        issues = validate_impl_plan(plan)
        assert any("path" in i for i in issues)

    def test_entry_missing_content(self):
        plan = {
            "root_cause": "test",
            "confidence": 0.5,
            "file_changes": [{"path": "foo.go"}],
        }
        issues = validate_impl_plan(plan)
        assert any("content" in i for i in issues)

    def test_entry_empty_path(self):
        plan = {
            "root_cause": "test",
            "confidence": 0.5,
            "file_changes": [{"path": "", "content": "data"}],
        }
        issues = validate_impl_plan(plan)
        assert any("path" in i for i in issues)

    def test_entry_empty_content(self):
        plan = {
            "root_cause": "test",
            "confidence": 0.5,
            "file_changes": [{"path": "a.go", "content": ""}],
        }
        issues = validate_impl_plan(plan)
        assert any("content" in i for i in issues)

    def test_entry_not_dict(self):
        plan = {
            "root_cause": "test",
            "confidence": 0.5,
            "file_changes": ["not-a-dict"],
        }
        issues = validate_impl_plan(plan)
        assert any("not a dict" in i for i in issues)

    def test_multiple_entries_mixed(self):
        plan = {
            "root_cause": "test",
            "confidence": 0.5,
            "file_changes": [
                {"path": "good.go", "content": "package main"},
                {"path": "", "content": "data"},
                {"path": "x.go", "content": ""},
            ],
        }
        issues = validate_impl_plan(plan)
        assert len(issues) == 2

    def test_valid_multiple_entries(self):
        plan = {
            "root_cause": "test",
            "confidence": 0.8,
            "file_changes": [
                {"path": "a.go", "content": "package a"},
                {"path": "b.go", "content": "package b"},
            ],
        }
        issues = validate_impl_plan(plan)
        assert issues == []


# ------------------------------------------------------------------
# _parse_with_retry tests
# ------------------------------------------------------------------


class TestParseWithRetry:
    @pytest.mark.asyncio
    async def test_valid_plan_no_retry(self):
        """When initial plan is valid, no retry is attempted."""
        phase = _make_implement(responses=[_fix_response(), "SHOULD NOT BE CALLED"])
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["root_cause"] == "Nil pointer dereference when owner ref is nil"
        assert phase.llm._call_count == 1

    @pytest.mark.asyncio
    async def test_parse_failure_triggers_retry(self):
        """When LLM returns garbage, a retry is attempted with JSON-only instruction."""
        phase = _make_implement(
            responses=["not json at all", _fix_response()],
            tool_executor=None,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["root_cause"] == "Nil pointer dereference when owner ref is nil"
        assert phase.llm._call_count == 2

    @pytest.mark.asyncio
    async def test_empty_file_changes_triggers_retry(self):
        """When LLM returns valid JSON but empty file_changes, retry is triggered."""
        phase = _make_implement(
            responses=[_fix_response_no_changes(), _fix_response()],
            tool_executor=None,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert len(plan["impl_plan"]["file_changes"]) >= 1
        assert phase.llm._call_count == 2

    @pytest.mark.asyncio
    async def test_missing_content_triggers_retry(self):
        """When file_changes entries lack content, retry is triggered."""
        bad_response = json.dumps(
            {
                "root_cause": "test",
                "fix_description": "test",
                "file_changes": [{"path": "a.go"}],
                "confidence": 0.5,
            }
        )
        phase = _make_implement(
            responses=[bad_response, _fix_response()],
            tool_executor=None,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["file_changes"][0].get("content")
        assert phase.llm._call_count == 2

    @pytest.mark.asyncio
    async def test_both_attempts_fail_returns_best(self):
        """When all retry attempts fail, returns the better one (parsed > parse failure)."""
        partial_response = json.dumps(
            {
                "root_cause": "some cause",
                "fix_description": "partial fix",
                "file_changes": [],
                "confidence": 0.5,
            }
        )
        config = EngineConfig()
        config.phases.implement.max_parse_retries = 1
        phase = _make_implement(
            responses=["not json", partial_response],
            tool_executor=None,
            config=config,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["root_cause"] == "some cause"
        assert phase.llm._call_count == 2

    @pytest.mark.asyncio
    async def test_both_attempts_total_failure(self):
        """When all retry attempts produce garbage, returns the original parse failure."""
        config = EngineConfig()
        config.phases.implement.max_parse_retries = 1
        phase = _make_implement(
            responses=["not json 1", "not json 2"],
            tool_executor=None,
            config=config,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["root_cause"] == "unknown"
        assert phase.llm._call_count == 2

    @pytest.mark.asyncio
    async def test_retry_logs_raw_response(self):
        """When parse fails, the raw response is logged."""
        logger = StructuredLogger()
        phase = ImplementPhase(
            llm=MockProvider(responses=["garbage response", _fix_response()]),
            logger=logger,
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        warn_entries = [e for e in logger._entries if e.get("level") == "WARN"]
        raw_logged = any("garbage response" in e.get("message", "") for e in warn_entries)
        assert raw_logged

    @pytest.mark.asyncio
    async def test_retry_records_llm_call(self):
        """The retry LLM call is recorded in the tracer."""
        tracer = Tracer()
        phase = ImplementPhase(
            llm=MockProvider(responses=["not json", _fix_response()]),
            logger=StructuredLogger(),
            tracer=tracer,
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        llm_actions = [a for a in tracer.get_actions() if a.action_type == "llm_query"]
        assert len(llm_actions) == 2
        assert "parse retry" in llm_actions[1].input_description.lower()

    @pytest.mark.asyncio
    async def test_retry_message_contains_issues(self):
        """The retry prompt includes the validation issues from the first attempt."""
        llm = MockProvider(responses=["not json", _fix_response()])
        phase = ImplementPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        retry_msg = llm.call_log[1]["messages"][0]["content"]
        assert "IMPORTANT" in retry_msg
        assert "file_changes" in retry_msg
        assert "ONLY a valid JSON" in retry_msg

    @pytest.mark.asyncio
    async def test_max_parse_retries_configurable(self):
        """max_parse_retries=0 disables retry."""
        cfg = EngineConfig()
        cfg.phases.implement.max_parse_retries = 0
        phase = _make_implement(
            responses=["not json", _fix_response()],
            tool_executor=None,
            config=cfg,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["root_cause"] == "unknown"
        assert phase.llm._call_count == 1

    @pytest.mark.asyncio
    async def test_max_parse_retries_two(self):
        """max_parse_retries=2 allows two retries."""
        cfg = EngineConfig()
        cfg.phases.implement.max_parse_retries = 2
        phase = _make_implement(
            responses=["bad1", "bad2", _fix_response()],
            tool_executor=None,
            config=cfg,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["impl_plan"]["root_cause"] == "Nil pointer dereference when owner ref is nil"
        assert phase.llm._call_count == 3


# ------------------------------------------------------------------
# _parse_with_retry in _request_refinement tests
# ------------------------------------------------------------------


class TestRefinementParseRetry:
    @pytest.mark.asyncio
    async def test_refinement_retries_on_parse_failure(self):
        """_request_refinement also uses _parse_with_retry."""
        phase = _make_implement(
            responses=[
                _fix_response(),
                "not json refinement",
                _fix_response(),
            ],
            tool_executor=None,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        refinement = await phase._request_refinement(
            plan["impl_plan"],
            {"passed": False, "output": "test fail"},
            {"passed": True, "output": "OK"},
            plan,
            1,
        )
        assert refinement["root_cause"] == "Nil pointer dereference when owner ref is nil"
        assert phase.llm._call_count == 3

    @pytest.mark.asyncio
    async def test_refinement_no_retry_on_valid_response(self):
        """_request_refinement does not retry when response is valid."""
        phase = _make_implement(
            responses=[_fix_response(), _fix_response()],
            tool_executor=None,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        await phase._request_refinement(
            plan["impl_plan"],
            {"passed": False, "output": "test fail"},
            {"passed": True, "output": "OK"},
            plan,
            1,
        )
        assert phase.llm._call_count == 2


# ------------------------------------------------------------------
# Config max_parse_retries tests
# ------------------------------------------------------------------


class TestConfigMaxParseRetries:
    def test_default_value(self):
        cfg = EngineConfig()
        assert cfg.phases.implement.max_parse_retries == 3

    def test_yaml_override(self):
        from engine.config import load_config

        cfg = load_config(overrides={"phases": {"implement": {"max_parse_retries": 3}}})
        assert cfg.phases.implement.max_parse_retries == 3
