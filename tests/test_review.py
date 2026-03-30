"""Tests for the Review Phase — independent code review, verdict logic, finding verification."""

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
from engine.phases.review import ReviewPhase, parse_review_response
from engine.tools.executor import ToolExecutor

# ------------------------------------------------------------------
# Helpers: canned LLM responses
# ------------------------------------------------------------------


def _approve_response(
    confidence: float = 0.9,
    scope: str = "bug_fix",
) -> str:
    """Return a JSON string representing a review approval."""
    return json.dumps(
        {
            "verdict": "approve",
            "findings": [
                {
                    "dimension": "correctness",
                    "severity": "nit",
                    "file": "pkg/controller/reconciler.go",
                    "line": 15,
                    "description": "Consider adding a comment explaining the nil check",
                    "suggestion": "Add inline comment",
                }
            ],
            "scope_assessment": scope,
            "injection_detected": False,
            "confidence": confidence,
            "summary": (
                "Fix correctly addresses the nil pointer dereference. Minimal change, tests pass."
            ),
        }
    )


def _request_changes_response(
    findings: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps(
        {
            "verdict": "request_changes",
            "findings": findings
            or [
                {
                    "dimension": "correctness",
                    "severity": "blocking",
                    "file": "pkg/controller/reconciler.go",
                    "line": 22,
                    "description": "The nil check is in the wrong location — should guard line 22",
                    "suggestion": "Move the nil check before the dereference on line 22",
                }
            ],
            "scope_assessment": "bug_fix",
            "injection_detected": False,
            "confidence": 0.8,
            "summary": "Fix is on the right track but has a correctness issue.",
        }
    )


def _block_response() -> str:
    return json.dumps(
        {
            "verdict": "block",
            "findings": [
                {
                    "dimension": "security",
                    "severity": "blocking",
                    "file": "pkg/controller/reconciler.go",
                    "line": 10,
                    "description": "Fix introduces an information leak via error message",
                    "suggestion": "Remove sensitive data from error output",
                },
                {
                    "dimension": "intent",
                    "severity": "blocking",
                    "file": "pkg/controller/reconciler.go",
                    "line": 30,
                    "description": "Fix adds new feature behavior beyond the bug scope",
                    "suggestion": "Remove the new feature and fix only the bug",
                },
            ],
            "scope_assessment": "mixed",
            "injection_detected": False,
            "confidence": 0.95,
            "summary": "Fix has security concerns and scope creep. Blocking for human review.",
        }
    )


def _injection_response() -> str:
    return json.dumps(
        {
            "verdict": "block",
            "findings": [],
            "scope_assessment": "bug_fix",
            "injection_detected": True,
            "confidence": 0.7,
            "summary": "Prompt injection detected in the code diff.",
        }
    )


# ------------------------------------------------------------------
# Helpers: prior phase results
# ------------------------------------------------------------------


_DEFAULT_DIFF = (
    "--- a/reconciler.go\n+++ b/reconciler.go\n"
    "@@ -10,6 +10,9 @@\n+\tif owner == nil {\n+\t\treturn nil\n+\t}"
)


def _impl_phase_result(
    files_changed: list[str] | None = None,
    diff: str = _DEFAULT_DIFF,
) -> PhaseResult:
    """Create a mock successful implementation PhaseResult."""
    return PhaseResult(
        phase="implement",
        success=True,
        should_continue=True,
        next_phase="review",
        findings={
            "root_cause": "Nil pointer dereference when owner ref is nil",
            "fix_description": "Added nil check before accessing owner reference",
            "confidence": 0.9,
        },
        artifacts={
            "files_changed": files_changed or ["pkg/controller/reconciler.go"],
            "diff": diff,
            "inner_iterations_used": 0,
        },
    )


def _triage_phase_result() -> PhaseResult:
    return PhaseResult(
        phase="triage",
        success=True,
        should_continue=True,
        next_phase="implement",
        findings={
            "classification": "bug",
            "severity": "high",
            "affected_components": ["pkg/controller/reconciler.go"],
            "reasoning": "Nil pointer dereference in reconciler",
        },
        artifacts={
            "triage_report": {
                "classification": "bug",
                "severity": "high",
            },
        },
    )


# ------------------------------------------------------------------
# Helpers: phase instantiation
# ------------------------------------------------------------------


def _make_review(
    responses: list[str] | None = None,
    repo_path: str = "/tmp/fake-repo",
    issue_data: dict[str, Any] | None = None,
    config: EngineConfig | None = None,
    tool_executor: ToolExecutor | None = None,
    prior_results: list[PhaseResult] | None = None,
) -> ReviewPhase:
    llm = MockProvider(responses=responses or [_approve_response()])
    return ReviewPhase(
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
            prior_results
            if prior_results is not None
            else [_triage_phase_result(), _impl_phase_result()]
        ),
    )


def _make_review_with_repo(
    tmp_path: Path,
    responses: list[str] | None = None,
    config: EngineConfig | None = None,
) -> ReviewPhase:
    """Create a ReviewPhase with a real temp repo and ToolExecutor."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg" / "controller").mkdir(parents=True)
    (repo / "pkg" / "controller" / "reconciler.go").write_text(
        "package controller\n\nfunc Reconcile() error {\n"
        "\tif owner == nil {\n\t\treturn nil\n\t}\n"
        "\treturn nil\n}\n"
    )
    (repo / "pkg" / "controller" / "reconciler_test.go").write_text("package controller\n")

    tracer = Tracer()
    logger = StructuredLogger()
    metrics = LoopMetrics()
    tool_executor = ToolExecutor(
        repo_path=str(repo),
        logger=logger,
        tracer=tracer,
        metrics=metrics,
        allowed_tools=["file_read", "file_search", "git_diff"],
    )

    return ReviewPhase(
        llm=MockProvider(responses=responses or [_approve_response()]),
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
        prior_phase_results=[_triage_phase_result(), _impl_phase_result()],
    )


# ------------------------------------------------------------------
# parse_review_response tests
# ------------------------------------------------------------------


class TestParseReview:
    def test_direct_json(self):
        raw = _approve_response()
        result = parse_review_response(raw)
        assert result["verdict"] == "approve"
        assert result["confidence"] == 0.9

    def test_json_code_block(self):
        raw = "Here is my review:\n```json\n" + _approve_response() + "\n```\nDone."
        result = parse_review_response(raw)
        assert result["verdict"] == "approve"

    def test_generic_code_block(self):
        raw = "Review:\n```\n" + _approve_response() + "\n```"
        result = parse_review_response(raw)
        assert result["verdict"] == "approve"

    def test_malformed_returns_block_default(self):
        result = parse_review_response("This is not JSON at all.")
        assert result["verdict"] == "block"
        assert result["confidence"] == 0.0
        assert "Failed to parse" in result["summary"]

    def test_empty_string(self):
        result = parse_review_response("")
        assert result["verdict"] == "block"

    def test_partial_json(self):
        result = parse_review_response('{"verdict": "approve"')
        assert result["verdict"] == "block"

    def test_multiple_code_blocks_picks_valid(self):
        raw = "```\nnot json\n```\n\n```json\n" + _approve_response() + "\n```"
        result = parse_review_response(raw)
        assert result["verdict"] == "approve"

    def test_request_changes_parse(self):
        result = parse_review_response(_request_changes_response())
        assert result["verdict"] == "request_changes"
        assert len(result["findings"]) == 1
        assert result["findings"][0]["severity"] == "blocking"

    def test_block_parse(self):
        result = parse_review_response(_block_response())
        assert result["verdict"] == "block"
        assert len(result["findings"]) == 2


# ------------------------------------------------------------------
# ReviewPhase.observe tests
# ------------------------------------------------------------------


class TestReviewObserve:
    @pytest.mark.asyncio
    async def test_observe_extracts_impl_diff(self):
        phase = _make_review(tool_executor=None)
        obs = await phase.observe()
        assert "owner == nil" in obs["diff"]
        assert obs["files_changed"] == ["pkg/controller/reconciler.go"]

    @pytest.mark.asyncio
    async def test_observe_without_impl_results(self):
        phase = _make_review(prior_results=[], tool_executor=None)
        obs = await phase.observe()
        assert obs["diff"] == ""
        assert obs["files_changed"] == []
        assert obs["file_contents"] == {}

    @pytest.mark.asyncio
    async def test_observe_reads_changed_files(self, tmp_path):
        phase = _make_review_with_repo(tmp_path)
        obs = await phase.observe()
        assert "pkg/controller/reconciler.go" in obs["file_contents"]
        assert "package controller" in obs["file_contents"]["pkg/controller/reconciler.go"]

    @pytest.mark.asyncio
    async def test_observe_includes_issue_data(self):
        phase = _make_review(tool_executor=None)
        obs = await phase.observe()
        assert obs["issue"]["url"] == "https://github.com/test/repo/issues/42"

    @pytest.mark.asyncio
    async def test_observe_includes_impl_findings(self):
        phase = _make_review(tool_executor=None)
        obs = await phase.observe()
        assert obs["impl_findings"]["root_cause"] == "Nil pointer dereference when owner ref is nil"


# ------------------------------------------------------------------
# ReviewPhase.plan tests
# ------------------------------------------------------------------


class TestReviewPlan:
    @pytest.mark.asyncio
    async def test_plan_calls_llm(self):
        phase = _make_review()
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["review_result"]["verdict"] == "approve"
        assert "raw_llm_response" in plan

    @pytest.mark.asyncio
    async def test_plan_records_llm_call(self):
        tracer = Tracer()
        phase = ReviewPhase(
            llm=MockProvider(responses=[_approve_response()]),
            logger=StructuredLogger(),
            tracer=tracer,
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), _impl_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        llm_actions = [a for a in tracer.get_actions() if a.action_type == "llm_query"]
        assert len(llm_actions) == 1
        assert llm_actions[0].llm_context["provider"] == "mock"

    @pytest.mark.asyncio
    async def test_plan_wraps_untrusted_content(self):
        llm = MockProvider(responses=[_approve_response()])
        phase = ReviewPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "untrusted review body"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), _impl_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        call = llm.call_log[0]
        msg = call["messages"][0]["content"]
        assert "UNTRUSTED CONTENT BELOW" in msg
        assert "END UNTRUSTED CONTENT" in msg
        assert "untrusted review body" in msg

    @pytest.mark.asyncio
    async def test_plan_includes_diff_as_untrusted(self):
        llm = MockProvider(responses=[_approve_response()])
        phase = ReviewPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), _impl_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        call = llm.call_log[0]
        msg = call["messages"][0]["content"]
        assert "owner == nil" in msg
        assert "treat as untrusted" in msg.lower()

    @pytest.mark.asyncio
    async def test_plan_includes_impl_summary_for_verification(self):
        llm = MockProvider(responses=[_approve_response()])
        phase = ReviewPhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=EngineConfig(),
            prior_phase_results=[_triage_phase_result(), _impl_phase_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        call = llm.call_log[0]
        msg = call["messages"][0]["content"]
        assert "verify independently" in msg.lower()


# ------------------------------------------------------------------
# ReviewPhase.act tests
# ------------------------------------------------------------------


class TestReviewAct:
    @pytest.mark.asyncio
    async def test_act_verifies_finding_files(self, tmp_path):
        phase = _make_review_with_repo(
            tmp_path,
            responses=[_request_changes_response()],
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        verified = result["verified_findings"]
        assert len(verified) >= 1
        found_any = any(v["found"] for v in verified)
        assert found_any

    @pytest.mark.asyncio
    async def test_act_without_tools(self):
        phase = _make_review(tool_executor=None)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["verified_findings"] == []

    @pytest.mark.asyncio
    async def test_act_records_actions(self, tmp_path):
        phase = _make_review_with_repo(
            tmp_path,
            responses=[_request_changes_response()],
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        verify_actions = [a for a in result["actions"] if a["action"] == "verify_finding_file"]
        assert len(verify_actions) >= 1

    @pytest.mark.asyncio
    async def test_act_deduplicates_file_checks(self, tmp_path):
        findings = [
            {
                "dimension": "correctness",
                "severity": "blocking",
                "file": "pkg/controller/reconciler.go",
                "line": 10,
                "description": "Issue 1",
                "suggestion": "Fix 1",
            },
            {
                "dimension": "style",
                "severity": "nit",
                "file": "pkg/controller/reconciler.go",
                "line": 20,
                "description": "Issue 2",
                "suggestion": "Fix 2",
            },
        ]
        phase = _make_review_with_repo(
            tmp_path,
            responses=[_request_changes_response(findings=findings)],
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert len(result["verified_findings"]) == 1

    @pytest.mark.asyncio
    async def test_act_no_findings(self):
        phase = _make_review(
            responses=[_approve_response()],
            tool_executor=None,
        )
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["verified_findings"] == []


# ------------------------------------------------------------------
# ReviewPhase.validate tests
# ------------------------------------------------------------------


class TestReviewValidate:
    @pytest.mark.asyncio
    async def test_validate_valid_approve(self):
        phase = _make_review()
        validation = await phase.validate(
            {
                "review_result": json.loads(_approve_response()),
                "verified_findings": [],
            }
        )
        assert validation["valid"] is True
        assert validation["verdict"] == "approve"
        assert validation["issues"] == []

    @pytest.mark.asyncio
    async def test_validate_valid_request_changes(self):
        phase = _make_review()
        validation = await phase.validate(
            {
                "review_result": json.loads(_request_changes_response()),
                "verified_findings": [],
            }
        )
        assert validation["valid"] is True
        assert validation["verdict"] == "request_changes"
        assert validation["blocking_count"] == 1

    @pytest.mark.asyncio
    async def test_validate_valid_block(self):
        phase = _make_review()
        validation = await phase.validate(
            {
                "review_result": json.loads(_block_response()),
                "verified_findings": [],
            }
        )
        assert validation["valid"] is True
        assert validation["verdict"] == "block"
        assert validation["blocking_count"] == 2

    @pytest.mark.asyncio
    async def test_validate_invalid_verdict(self):
        phase = _make_review()
        review = json.loads(_approve_response())
        review["verdict"] = "maybe"
        validation = await phase.validate({"review_result": review})
        assert validation["valid"] is False
        assert any("Invalid verdict" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_invalid_confidence(self):
        phase = _make_review()
        review = json.loads(_approve_response())
        review["confidence"] = 1.5
        validation = await phase.validate({"review_result": review})
        assert validation["valid"] is False
        assert any("Invalid confidence" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_negative_confidence(self):
        phase = _make_review()
        review = json.loads(_approve_response())
        review["confidence"] = -0.1
        validation = await phase.validate({"review_result": review})
        assert validation["valid"] is False

    @pytest.mark.asyncio
    async def test_validate_invalid_scope(self):
        phase = _make_review()
        review = json.loads(_approve_response())
        review["scope_assessment"] = "refactor"
        validation = await phase.validate({"review_result": review})
        assert validation["valid"] is False
        assert any("Invalid scope_assessment" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_missing_summary(self):
        phase = _make_review()
        review = json.loads(_approve_response())
        review["summary"] = ""
        validation = await phase.validate({"review_result": review})
        assert validation["valid"] is False
        assert any("Missing review summary" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_injection_flag(self):
        phase = _make_review()
        review = json.loads(_injection_response())
        validation = await phase.validate({"review_result": review})
        assert validation["injection_detected"] is True


# ------------------------------------------------------------------
# ReviewPhase.reflect tests
# ------------------------------------------------------------------


class TestReviewReflect:
    @pytest.mark.asyncio
    async def test_approve_advances_to_validate(self):
        phase = _make_review()
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "approve",
                "injection_detected": False,
                "review_result": json.loads(_approve_response()),
                "verified_findings": [],
            }
        )
        assert result.success is True
        assert result.next_phase == "validate"
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_request_changes_backtracks_to_implement(self):
        phase = _make_review()
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "request_changes",
                "injection_detected": False,
                "review_result": json.loads(_request_changes_response()),
                "verified_findings": [],
            }
        )
        assert result.success is False
        assert result.next_phase == "implement"
        assert result.should_continue is True
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_injection_detected_escalates(self):
        phase = _make_review()
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "block",
                "injection_detected": True,
                "review_result": json.loads(_injection_response()),
                "verified_findings": [],
            }
        )
        assert result.success is False
        assert result.escalate is True
        assert "injection" in result.escalation_reason.lower()

    @pytest.mark.asyncio
    async def test_validation_issues_trigger_retry(self):
        phase = _make_review()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Invalid verdict: 'maybe'"],
                "verdict": "maybe",
                "injection_detected": False,
                "review_result": {},
            }
        )
        assert result.success is False
        assert result.should_continue is True
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_block_downgraded_when_no_security_finding(self):
        """Block with non-security blocking findings → downgraded to request_changes."""
        phase = _make_review()
        non_security_block = {
            "verdict": "block",
            "findings": [
                {
                    "dimension": "intent",
                    "severity": "blocking",
                    "file": "reconciler.go",
                    "line": 30,
                    "description": "Fix introduces feature creep beyond the bug scope",
                    "suggestion": "Remove the new feature and fix only the bug",
                },
                {
                    "dimension": "correctness",
                    "severity": "blocking",
                    "file": "reconciler.go",
                    "line": 10,
                    "description": "Wrong approach — this downgrades the version",
                    "suggestion": "Keep the existing version and fix the nil check",
                },
            ],
            "scope_assessment": "mixed",
            "injection_detected": False,
            "confidence": 0.9,
            "summary": "Fix has quality issues but no security concerns.",
        }
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "block",
                "injection_detected": False,
                "review_result": non_security_block,
                "verified_findings": [],
            }
        )
        assert result.success is False
        assert result.escalate is False
        assert result.next_phase == "implement"
        assert result.should_continue is True
        assert result.findings["verdict"] == "request_changes"

    @pytest.mark.asyncio
    async def test_block_preserved_with_security_blocking_finding(self):
        """Block with a security+blocking finding → NOT downgraded, escalates."""
        phase = _make_review()
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "block",
                "injection_detected": False,
                "review_result": json.loads(_block_response()),
                "verified_findings": [],
            }
        )
        assert result.success is False
        assert result.escalate is True
        assert "blocked" in result.escalation_reason.lower()

    @pytest.mark.asyncio
    async def test_block_downgraded_with_empty_findings(self):
        """Block with no findings and no injection → downgraded."""
        phase = _make_review()
        empty_block = {
            "verdict": "block",
            "findings": [],
            "scope_assessment": "bug_fix",
            "injection_detected": False,
            "confidence": 0.0,
            "summary": "Failed to parse LLM review response.",
        }
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "block",
                "injection_detected": False,
                "review_result": empty_block,
                "verified_findings": [],
            }
        )
        assert result.success is False
        assert result.escalate is False
        assert result.next_phase == "implement"
        assert result.should_continue is True

    @pytest.mark.asyncio
    async def test_block_not_downgraded_when_injection_detected(self):
        """Block with injection_detected → escalates regardless of findings."""
        phase = _make_review()
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "block",
                "injection_detected": True,
                "review_result": json.loads(_injection_response()),
                "verified_findings": [],
            }
        )
        assert result.success is False
        assert result.escalate is True
        assert "injection" in result.escalation_reason.lower()

    @pytest.mark.asyncio
    async def test_block_downgrade_updates_review_verdict(self):
        """When block is downgraded, the review_result in artifacts reflects the change."""
        phase = _make_review()
        quality_block = {
            "verdict": "block",
            "findings": [
                {
                    "dimension": "style",
                    "severity": "suggestion",
                    "file": "main.go",
                    "line": 1,
                    "description": "Style issue",
                    "suggestion": "Fix style",
                }
            ],
            "scope_assessment": "bug_fix",
            "injection_detected": False,
            "confidence": 0.7,
            "summary": "Style issues found.",
        }
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "block",
                "injection_detected": False,
                "review_result": quality_block,
                "verified_findings": [],
            }
        )
        assert result.artifacts["review_report"]["verdict"] == "request_changes"

    @pytest.mark.asyncio
    async def test_approve_artifacts_populated(self):
        phase = _make_review()
        review_data = json.loads(_approve_response())
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "approve",
                "injection_detected": False,
                "review_result": review_data,
                "verified_findings": [{"path": "a.go", "found": True}],
            }
        )
        assert result.artifacts["review_report"] == review_data
        assert result.artifacts["verified_findings"] == [{"path": "a.go", "found": True}]

    @pytest.mark.asyncio
    async def test_request_changes_artifacts_populated(self):
        phase = _make_review()
        review_data = json.loads(_request_changes_response())
        result = await phase.reflect(
            {
                "valid": True,
                "verdict": "request_changes",
                "injection_detected": False,
                "review_result": review_data,
                "verified_findings": [],
            }
        )
        assert result.artifacts["review_report"]["verdict"] == "request_changes"


# ------------------------------------------------------------------
# Full execute lifecycle tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_approve_success():
    """Full execute() with an approve verdict → success, next_phase=validate."""
    phase = _make_review()
    result = await phase.execute()
    assert result.success is True
    assert result.next_phase == "validate"
    assert result.findings["verdict"] == "approve"


@pytest.mark.asyncio
async def test_execute_request_changes():
    """Full execute() with request_changes → backtrack to implement."""
    phase = _make_review(responses=[_request_changes_response()])
    result = await phase.execute()
    assert result.success is False
    assert result.next_phase == "implement"
    assert result.should_continue is True


@pytest.mark.asyncio
async def test_execute_block_escalates():
    """Full execute() with block verdict → escalate."""
    phase = _make_review(responses=[_block_response()])
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is True


@pytest.mark.asyncio
async def test_execute_injection_escalates():
    """Full execute() when injection is detected → escalate."""
    phase = _make_review(responses=[_injection_response()])
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is True
    assert "injection" in result.escalation_reason.lower()


@pytest.mark.asyncio
async def test_execute_malformed_llm_response():
    """Full execute() with unparseable LLM output → downgraded to request_changes.

    The parser defaults to ``block`` on malformed input, but since there is
    no injection detected and no security+blocking finding, the reflect()
    method downgrades the verdict to ``request_changes`` so the loop can
    retry via the implement phase instead of killing the loop.
    """
    phase = _make_review(responses=["This is not JSON, sorry!"])
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is False
    assert result.next_phase == "implement"
    assert result.should_continue is True


@pytest.mark.asyncio
async def test_execute_with_real_repo(tmp_path):
    """Full execute() against a temp repo with real ToolExecutor."""
    phase = _make_review_with_repo(tmp_path)
    result = await phase.execute()
    assert result.success is True
    assert result.next_phase == "validate"
    assert "review_report" in result.artifacts


@pytest.mark.asyncio
async def test_execute_no_impl_prior():
    """Execute() when there are no prior implementation results — still runs."""
    phase = _make_review(prior_results=[], tool_executor=None)
    result = await phase.execute()
    assert result.phase == "review"


@pytest.mark.asyncio
async def test_execute_artifacts_populated(tmp_path):
    """Approve result includes review_report and verified_findings."""
    phase = _make_review_with_repo(tmp_path)
    result = await phase.execute()
    assert "review_report" in result.artifacts
    assert "verified_findings" in result.artifacts


# ------------------------------------------------------------------
# _has_security_block tests
# ------------------------------------------------------------------


class TestHasSecurityBlock:
    def test_empty_findings(self):
        assert ReviewPhase._has_security_block({"findings": []}) is False

    def test_no_findings_key(self):
        assert ReviewPhase._has_security_block({}) is False

    def test_security_blocking(self):
        review = {
            "findings": [
                {"dimension": "security", "severity": "blocking"},
            ]
        }
        assert ReviewPhase._has_security_block(review) is True

    def test_security_suggestion_not_blocking(self):
        review = {
            "findings": [
                {"dimension": "security", "severity": "suggestion"},
            ]
        }
        assert ReviewPhase._has_security_block(review) is False

    def test_correctness_blocking_not_security(self):
        review = {
            "findings": [
                {"dimension": "correctness", "severity": "blocking"},
            ]
        }
        assert ReviewPhase._has_security_block(review) is False

    def test_intent_blocking_not_security(self):
        review = {
            "findings": [
                {"dimension": "intent", "severity": "blocking"},
            ]
        }
        assert ReviewPhase._has_security_block(review) is False

    def test_mixed_findings_with_security(self):
        review = {
            "findings": [
                {"dimension": "intent", "severity": "blocking"},
                {"dimension": "security", "severity": "blocking"},
                {"dimension": "style", "severity": "nit"},
            ]
        }
        assert ReviewPhase._has_security_block(review) is True

    def test_mixed_findings_without_security_blocking(self):
        review = {
            "findings": [
                {"dimension": "intent", "severity": "blocking"},
                {"dimension": "security", "severity": "nit"},
                {"dimension": "correctness", "severity": "blocking"},
            ]
        }
        assert ReviewPhase._has_security_block(review) is False


# ------------------------------------------------------------------
# Implementation artifact extraction tests
# ------------------------------------------------------------------


class TestImplExtraction:
    def test_extract_from_artifacts(self):
        phase = _make_review(
            prior_results=[_triage_phase_result(), _impl_phase_result()],
            tool_executor=None,
        )
        artifacts = phase._extract_impl_artifacts()
        assert "owner == nil" in artifacts["diff"]
        assert artifacts["files_changed"] == ["pkg/controller/reconciler.go"]

    def test_extract_findings(self):
        phase = _make_review(
            prior_results=[_triage_phase_result(), _impl_phase_result()],
            tool_executor=None,
        )
        artifacts = phase._extract_impl_artifacts()
        expected_cause = "Nil pointer dereference when owner ref is nil"
        assert artifacts["findings"]["root_cause"] == expected_cause

    def test_extract_empty_when_no_impl(self):
        phase = _make_review(prior_results=[], tool_executor=None)
        artifacts = phase._extract_impl_artifacts()
        assert artifacts == {}

    def test_extract_skips_failed_impl(self):
        failed = PhaseResult(phase="implement", success=False, findings={"bad": True})
        phase = _make_review(
            prior_results=[_triage_phase_result(), failed],
            tool_executor=None,
        )
        artifacts = phase._extract_impl_artifacts()
        assert artifacts == {}

    def test_extract_picks_latest_successful_impl(self):
        impl1 = PhaseResult(
            phase="implement",
            success=True,
            findings={"root_cause": "old"},
            artifacts={"diff": "old diff", "files_changed": ["old.go"]},
        )
        impl2 = PhaseResult(
            phase="implement",
            success=True,
            findings={"root_cause": "new"},
            artifacts={"diff": "new diff", "files_changed": ["new.go"]},
        )
        phase = _make_review(
            prior_results=[_triage_phase_result(), impl1, impl2],
            tool_executor=None,
        )
        artifacts = phase._extract_impl_artifacts()
        assert artifacts["diff"] == "new diff"
        assert artifacts["files_changed"] == ["new.go"]


# ------------------------------------------------------------------
# Integration with loop
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_phase_in_loop(tmp_path):
    """ReviewPhase can be registered and executed in PipelineEngine."""
    import subprocess

    from engine.loop import PipelineEngine
    from engine.phases.implement import ImplementPhase
    from engine.phases.triage import TriagePhase

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg" / "controller").mkdir(parents=True)
    (repo / "pkg" / "controller" / "reconciler.go").write_text("package controller\n")

    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--allow-empty"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env={
            **__import__("os").environ,
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
        },
    )

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
    impl_resp = json.dumps(
        {
            "root_cause": "Nil pointer",
            "fix_description": "Added nil check",
            "files_changed": ["pkg/controller/reconciler.go"],
            "file_changes": [
                {
                    "path": "pkg/controller/reconciler.go",
                    "content": (
                        "package controller\n\nfunc Reconcile()"
                        " error {\n\tif owner == nil {\n"
                        "\t\treturn nil\n\t}\n\treturn nil\n}\n"
                    ),
                }
            ],
            "test_added": "",
            "tests_passing": True,
            "linters_passing": True,
            "confidence": 0.9,
            "diff_summary": "Added nil check",
        }
    )
    review_resp = _approve_response()

    cfg = EngineConfig()
    cfg.phases.implement.run_tests_after_each_edit = False
    cfg.phases.implement.run_linters = False

    loop = PipelineEngine(
        config=cfg,
        llm=MockProvider(responses=[triage_resp, impl_resp, review_resp]),
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(repo),
        output_dir=str(out),
    )
    loop.register_phase("triage", TriagePhase)
    loop.register_phase("implement", ImplementPhase)
    loop.register_phase("review", ReviewPhase)

    execution = await loop.run()
    phases_run = [it["phase"] for it in execution.iterations]
    assert "triage" in phases_run
    assert "implement" in phases_run
    assert "review" in phases_run


# ------------------------------------------------------------------
# ClassVar and config tests
# ------------------------------------------------------------------


class TestReviewClassProperties:
    def test_name(self):
        assert ReviewPhase.name == "review"

    def test_allowed_tools_falls_back_to_review_set(self):
        tools = ReviewPhase.get_allowed_tools()
        assert "file_read" in tools
        assert "file_search" in tools
        assert "git_diff" in tools
        assert "file_write" not in tools
        assert "shell_run" not in tools
        assert "git_commit" not in tools
        assert "github_api" not in tools

    def test_config_review_flags(self):
        cfg = EngineConfig()
        assert cfg.phases.review.correctness is True
        assert cfg.phases.review.intent_alignment is True
        assert cfg.phases.review.security is True
        assert cfg.phases.review.style is True
        assert cfg.phases.review.scope_check is True
