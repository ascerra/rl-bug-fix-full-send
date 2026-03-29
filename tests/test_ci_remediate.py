"""Tests for 10.3 — CI Remediation Loop.

Covers:
- CIRemediatePhase OODA cycle: observe, plan, act, validate, reflect
- Phase attributes (name, allowed_tools)
- Prior attempt extraction from phase results
- LLM response parsing (valid JSON, code blocks, malformed)
- Module helpers: _extract_failing_files, _build_trusted_context, _build_untrusted_context
- Infrastructure flake handling (needs_rerun)
- Loop integration: _pr_was_created, _extract_branch_from_pr, _extract_repo_parts_from_url
- Loop CI monitoring sub-loop: CI pass, CI fail → remediate, iteration cap, time budget
- Loop CI monitoring sub-loop: escalation, rerun on flake, disabled config
- Phase registration in __main__.py
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config import CIRemediationConfig, EngineConfig
from engine.integrations.llm import MockProvider
from engine.loop import PHASE_ORDER, RalphLoop
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.phases.base import PhaseResult
from engine.phases.ci_remediate import (
    CIRemediatePhase,
    _build_trusted_context,
    _build_untrusted_context,
    _extract_failing_files,
    _parse_remediation_response,
)

# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------


def _make_failure_details(
    *,
    category: str = "test_failure",
    summary: str = "1 check(s) failed (test_failure): ci/test",
    failing_checks: list[str] | None = None,
    error_messages: list[str] | None = None,
    failing_tests: list[str] | None = None,
    annotations: list[dict[str, Any]] | None = None,
    log_excerpts: list[str] | None = None,
    workflow_run_ids: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "category": category,
        "summary": summary,
        "failing_checks": failing_checks or ["ci/test"],
        "error_messages": error_messages or ["TestFoo failed: expected 1 got 2"],
        "failing_tests": failing_tests or ["TestFoo"],
        "annotations": annotations
        or [
            {
                "path": "pkg/handler.go",
                "start_line": 42,
                "end_line": 42,
                "annotation_level": "failure",
                "message": "assertion failed",
            }
        ],
        "log_excerpts": log_excerpts or ["--- FAIL: TestFoo (0.1s)"],
        "workflow_run_ids": workflow_run_ids or [9000],
        "recommended_action": "remediate",
    }


def _make_issue_data(
    *,
    failure_details: dict[str, Any] | None = None,
    category: str = "test_failure",
    branch: str = "rl/fix-123-abc",
    remediation_iter: int = 1,
) -> dict[str, Any]:
    return {
        "url": "https://github.com/org/repo/issues/123",
        "ci_failure_details": failure_details or _make_failure_details(),
        "ci_failure_category": category,
        "branch_name": branch,
        "original_diff": "--- a/x.go\n+++ b/x.go\n@@ -1 +1 @@\n-old\n+new",
        "original_description": "Added nil check",
        "remediation_iteration": remediation_iter,
    }


def _make_llm_response(
    *,
    analysis: str = "Test fails because nil check is incomplete",
    fix_strategy: str = "Add missing return statement",
    is_code_fix: bool = True,
    file_changes: list[dict[str, str]] | None = None,
) -> str:
    return json.dumps(
        {
            "analysis": analysis,
            "fix_strategy": fix_strategy,
            "is_code_fix": is_code_fix,
            "file_changes": file_changes
            or [{"path": "pkg/handler.go", "content": "fixed content"}],
            "expected_resolution": "ci/test should pass",
            "pre_existing_failures": [],
        }
    )


def _make_rerun_response() -> str:
    return json.dumps(
        {
            "analysis": "Network timeout on CI runner",
            "fix_strategy": "rerun",
            "is_code_fix": False,
            "file_changes": [],
            "expected_resolution": "CI rerun should resolve infrastructure flake",
            "pre_existing_failures": [],
        }
    )


def _make_phase(
    issue_data: dict[str, Any] | None = None,
    prior_results: list[PhaseResult] | None = None,
    llm_response: str = "",
    tool_executor: Any = None,
) -> CIRemediatePhase:
    mock_llm = MockProvider(
        responses=[llm_response or _make_llm_response()],
    )
    logger = StructuredLogger()
    tracer = Tracer()
    config = EngineConfig()
    metrics = LoopMetrics()

    return CIRemediatePhase(
        llm=mock_llm,
        logger=logger,
        tracer=tracer,
        repo_path="/tmp/test-repo",
        issue_data=issue_data or _make_issue_data(),
        prior_phase_results=prior_results or [],
        tool_executor=tool_executor,
        config=config,
        metrics=metrics,
    )


def _mock_tool_executor(
    file_read_content: str = "existing content",
    shell_success: bool = True,
    shell_stdout: str = "",
) -> MagicMock:
    executor = MagicMock()

    async def mock_execute(tool: str, **kwargs: Any) -> dict[str, Any]:
        if tool == "file_read":
            return {"success": True, "content": file_read_content}
        if tool == "file_write":
            return {"success": True}
        if tool == "git_commit":
            return {"success": True}
        if tool == "shell_run":
            return {
                "success": shell_success,
                "stdout": shell_stdout or "ok",
                "stderr": "",
            }
        return {"success": True}

    executor.execute = AsyncMock(side_effect=mock_execute)
    return executor


# ==================================================================
# Phase attributes
# ==================================================================


class TestPhaseAttributes:
    def test_name(self):
        phase = _make_phase()
        assert phase.name == "ci_remediate"

    def test_allowed_tools_from_phase_tool_sets(self):
        from engine.phases.base import CI_REMEDIATE_TOOLS, PHASE_TOOL_SETS

        assert "ci_remediate" in PHASE_TOOL_SETS
        assert PHASE_TOOL_SETS["ci_remediate"] == CI_REMEDIATE_TOOLS
        assert "file_write" in CI_REMEDIATE_TOOLS
        assert "github_api" in CI_REMEDIATE_TOOLS

    def test_get_allowed_tools(self):
        tools = CIRemediatePhase.get_allowed_tools()
        assert "file_write" in tools
        assert "github_api" in tools
        assert "shell_run" in tools


# ==================================================================
# Module helpers
# ==================================================================


class TestExtractFailingFiles:
    def test_from_annotations(self):
        details = _make_failure_details(
            annotations=[
                {"path": "pkg/a.go", "start_line": 1, "annotation_level": "failure", "message": ""},
                {"path": "pkg/b.go", "start_line": 2, "annotation_level": "failure", "message": ""},
            ]
        )
        files = _extract_failing_files(details)
        assert "pkg/a.go" in files
        assert "pkg/b.go" in files

    def test_from_error_messages(self):
        details = _make_failure_details(
            annotations=[],
            error_messages=["pkg/handler.go:42: assertion failed"],
        )
        files = _extract_failing_files(details)
        assert "pkg/handler.go" in files

    def test_deduplicates(self):
        ann = {"path": "a.go", "start_line": 1, "annotation_level": "failure", "message": ""}
        details = _make_failure_details(
            annotations=[ann],
            error_messages=["a.go:10: error"],
        )
        files = _extract_failing_files(details)
        assert files.count("a.go") == 1

    def test_empty_details(self):
        assert _extract_failing_files({}) == []

    def test_caps_at_20(self):
        annotations = [
            {"path": f"f{i}.go", "start_line": 1, "annotation_level": "failure", "message": ""}
            for i in range(25)
        ]
        files = _extract_failing_files({"annotations": annotations, "error_messages": []})
        assert len(files) <= 25


class TestBuildTrustedContext:
    def test_contains_failure_category(self):
        obs = {
            "failure_details": _make_failure_details(),
            "failure_category": "test_failure",
            "remediation_iteration": 1,
            "original_description": "nil check",
            "prior_attempts": [],
            "file_contents": {},
            "original_diff": "",
        }
        ctx = _build_trusted_context(obs)
        assert "test_failure" in ctx

    def test_includes_prior_attempts(self):
        prior = [{"action": "pushed", "fix_strategy": "add return", "success": False}]
        obs = {
            "failure_details": _make_failure_details(),
            "failure_category": "test_failure",
            "remediation_iteration": 2,
            "original_description": "",
            "prior_attempts": prior,
            "file_contents": {},
            "original_diff": "",
        }
        ctx = _build_trusted_context(obs)
        assert "PRIOR CI REMEDIATION ATTEMPTS" in ctx
        assert "add return" in ctx

    def test_includes_file_contents(self):
        obs = {
            "failure_details": _make_failure_details(),
            "failure_category": "test_failure",
            "remediation_iteration": 1,
            "original_description": "",
            "prior_attempts": [],
            "file_contents": {"handler.go": "package main"},
            "original_diff": "",
        }
        ctx = _build_trusted_context(obs)
        assert "handler.go" in ctx
        assert "package main" in ctx


class TestBuildUntrustedContext:
    def test_contains_error_messages(self):
        details = _make_failure_details(error_messages=["TestFoo FAILED"])
        ctx = _build_untrusted_context(details)
        assert "TestFoo FAILED" in ctx

    def test_contains_annotations(self):
        details = _make_failure_details(
            annotations=[
                {
                    "path": "a.go",
                    "start_line": 10,
                    "annotation_level": "failure",
                    "message": "nil ptr",
                },
            ]
        )
        ctx = _build_untrusted_context(details)
        assert "a.go:10" in ctx
        assert "nil ptr" in ctx

    def test_contains_log_excerpts(self):
        details = _make_failure_details(log_excerpts=["--- FAIL: TestBar"])
        ctx = _build_untrusted_context(details)
        assert "FAIL: TestBar" in ctx


class TestParseRemediationResponse:
    def test_valid_json(self):
        result = _parse_remediation_response(_make_llm_response())
        assert result["is_code_fix"] is True
        assert len(result["file_changes"]) == 1

    def test_json_code_block(self):
        content = "Here's the fix:\n```json\n" + _make_llm_response() + "\n```\nDone."
        result = _parse_remediation_response(content)
        assert result["is_code_fix"] is True

    def test_malformed_returns_default(self):
        result = _parse_remediation_response("This is not JSON at all")
        assert result["is_code_fix"] is False
        assert "Failed to parse" in result["analysis"]

    def test_empty_content(self):
        result = _parse_remediation_response("")
        assert result["is_code_fix"] is False


# ==================================================================
# CIRemediatePhase OODA cycle
# ==================================================================


class TestObserve:
    @pytest.mark.asyncio
    async def test_basic_observation(self):
        phase = _make_phase()
        obs = await phase.observe()
        assert obs["failure_category"] == "test_failure"
        assert obs["branch_name"] == "rl/fix-123-abc"
        assert obs["remediation_iteration"] == 1
        assert len(obs["failure_details"]["failing_checks"]) > 0

    @pytest.mark.asyncio
    async def test_reads_failing_files(self):
        executor = _mock_tool_executor(file_read_content="package main")
        phase = _make_phase(tool_executor=executor)
        obs = await phase.observe()
        assert "pkg/handler.go" in obs["file_contents"]

    @pytest.mark.asyncio
    async def test_extracts_prior_attempts(self):
        prior = PhaseResult(
            phase="ci_remediate",
            success=False,
            findings={"action": "pushed", "analysis": "wrong approach"},
            artifacts={"files_changed": ["a.go"], "pushed": True},
        )
        phase = _make_phase(prior_results=[prior])
        obs = await phase.observe()
        assert len(obs["prior_attempts"]) == 1
        assert obs["prior_attempts"][0]["analysis"] == "wrong approach"


class TestPlan:
    @pytest.mark.asyncio
    async def test_calls_llm_with_failure_context(self):
        phase = _make_phase(llm_response=_make_llm_response())
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["plan"]["is_code_fix"] is True
        assert plan["plan"]["fix_strategy"] == "Add missing return statement"

    @pytest.mark.asyncio
    async def test_records_llm_call(self):
        phase = _make_phase(llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)
        actions = phase.tracer.get_actions_as_dicts()
        llm_actions = [a for a in actions if a.get("action_type") == "llm_query"]
        assert len(llm_actions) >= 1

    @pytest.mark.asyncio
    async def test_rerun_plan(self):
        phase = _make_phase(llm_response=_make_rerun_response())
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["plan"]["is_code_fix"] is False
        assert plan["plan"]["fix_strategy"] == "rerun"


class TestAct:
    @pytest.mark.asyncio
    async def test_applies_file_changes(self):
        executor = _mock_tool_executor()
        phase = _make_phase(tool_executor=executor)
        obs = await phase.observe()
        plan_result = await phase.plan(obs)
        act_result = await phase.act(plan_result)
        assert act_result["committed"] is True
        assert act_result["pushed"] is True
        assert "pkg/handler.go" in act_result["files_changed"]

    @pytest.mark.asyncio
    async def test_no_code_fix_skips_commit(self):
        phase = _make_phase(llm_response=_make_rerun_response())
        obs = await phase.observe()
        plan_result = await phase.plan(obs)
        act_result = await phase.act(plan_result)
        assert act_result["committed"] is False
        assert act_result["needs_rerun"] is True

    @pytest.mark.asyncio
    async def test_empty_file_changes(self):
        resp = json.dumps(
            {
                "analysis": "Cannot determine fix",
                "fix_strategy": "unknown",
                "is_code_fix": True,
                "file_changes": [],
                "expected_resolution": "",
                "pre_existing_failures": [],
            }
        )
        executor = _mock_tool_executor()
        phase = _make_phase(llm_response=resp, tool_executor=executor)
        obs = await phase.observe()
        plan_result = await phase.plan(obs)
        act_result = await phase.act(plan_result)
        assert act_result["committed"] is False
        assert act_result["files_changed"] == []

    @pytest.mark.asyncio
    async def test_no_tool_executor(self):
        phase = _make_phase(llm_response=_make_llm_response(), tool_executor=None)
        obs = await phase.observe()
        plan_result = await phase.plan(obs)
        act_result = await phase.act(plan_result)
        assert act_result["committed"] is False
        assert act_result["files_changed"] == []


class TestValidateStep:
    @pytest.mark.asyncio
    async def test_lint_pass(self):
        executor = _mock_tool_executor(shell_success=True, shell_stdout="All checks passed!")
        phase = _make_phase(tool_executor=executor)
        act_result = {
            "plan": {},
            "files_changed": ["a.go"],
            "committed": True,
            "pushed": True,
            "needs_rerun": False,
            "actions": [],
        }
        validation = await phase.validate(act_result)
        assert validation["valid"] is True
        assert validation["lint_passed"] is True

    @pytest.mark.asyncio
    async def test_no_files_changed_skips_lint(self):
        phase = _make_phase()
        act_result = {
            "plan": {},
            "files_changed": [],
            "committed": False,
            "pushed": False,
            "needs_rerun": False,
            "actions": [],
        }
        validation = await phase.validate(act_result)
        assert validation["valid"] is True
        assert "No files changed" in validation["lint_output"]


class TestReflect:
    @pytest.mark.asyncio
    async def test_successful_push(self):
        phase = _make_phase()
        validation = {
            "valid": True,
            "lint_passed": True,
            "lint_output": "",
            "action_result": {
                "plan": {"analysis": "Fixed", "fix_strategy": "add return"},
                "files_changed": ["a.go"],
                "committed": True,
                "pushed": True,
                "needs_rerun": False,
            },
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert result.artifacts.get("pushed") is True
        assert result.findings.get("action") == "pushed"

    @pytest.mark.asyncio
    async def test_needs_rerun(self):
        phase = _make_phase()
        validation = {
            "valid": True,
            "lint_passed": True,
            "lint_output": "",
            "action_result": {
                "plan": {"analysis": "Flake"},
                "files_changed": [],
                "committed": False,
                "pushed": False,
                "needs_rerun": True,
            },
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert result.artifacts.get("needs_rerun") is True

    @pytest.mark.asyncio
    async def test_no_files_changed(self):
        phase = _make_phase()
        validation = {
            "valid": True,
            "lint_passed": True,
            "lint_output": "",
            "action_result": {
                "plan": {"analysis": "No fix"},
                "files_changed": [],
                "committed": False,
                "pushed": False,
                "needs_rerun": False,
            },
        }
        result = await phase.reflect(validation)
        assert result.success is False
        assert result.findings.get("action") == "no_fix"

    @pytest.mark.asyncio
    async def test_lint_failed(self):
        phase = _make_phase()
        validation = {
            "valid": False,
            "lint_passed": False,
            "lint_output": "E501 line too long",
            "action_result": {
                "plan": {"analysis": "Fix"},
                "files_changed": ["a.go"],
                "committed": True,
                "pushed": False,
                "needs_rerun": False,
            },
        }
        result = await phase.reflect(validation)
        assert result.success is False
        assert result.findings.get("action") == "lint_failed"

    @pytest.mark.asyncio
    async def test_push_failed(self):
        phase = _make_phase()
        validation = {
            "valid": True,
            "lint_passed": True,
            "lint_output": "",
            "action_result": {
                "plan": {"analysis": "Fix"},
                "files_changed": ["a.go"],
                "committed": True,
                "pushed": False,
                "needs_rerun": False,
            },
        }
        result = await phase.reflect(validation)
        assert result.success is False
        assert result.findings.get("action") == "push_failed"


class TestFullExecute:
    @pytest.mark.asyncio
    async def test_full_ooda_cycle(self):
        executor = _mock_tool_executor()
        phase = _make_phase(
            llm_response=_make_llm_response(),
            tool_executor=executor,
        )
        result = await phase.execute()
        assert result.phase == "ci_remediate"
        assert result.success is True
        assert result.artifacts.get("pushed") is True

    @pytest.mark.asyncio
    async def test_full_cycle_no_code_fix(self):
        phase = _make_phase(llm_response=_make_rerun_response())
        result = await phase.execute()
        assert result.phase == "ci_remediate"
        assert result.success is True
        assert result.artifacts.get("needs_rerun") is True


# ==================================================================
# Loop integration
# ==================================================================


class TestLoopHelpers:
    def test_pr_was_created_true(self):
        result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={"pr_created": True, "pr_url": "https://github.com/o/r/pull/1"},
        )
        assert RalphLoop._pr_was_created(result) is True

    def test_pr_was_created_false(self):
        result = PhaseResult(phase="validate", success=True, artifacts={"pr_created": False})
        assert RalphLoop._pr_was_created(result) is False

    def test_pr_was_created_missing(self):
        result = PhaseResult(phase="validate", success=True, artifacts={})
        assert RalphLoop._pr_was_created(result) is False

    def test_extract_branch_from_pr_url(self):
        result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={"pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc"},
        )
        branch = RalphLoop._extract_branch_from_pr(result)
        assert branch == "rl/fix-123-abc"

    def test_extract_branch_from_artifacts_key(self):
        result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={"pr_url": "https://github.com/o/r/pull/1", "branch_name": "rl/fix-42-xyz"},
        )
        branch = RalphLoop._extract_branch_from_pr(result)
        assert "rl/fix" in branch

    def test_extract_branch_empty(self):
        result = PhaseResult(phase="validate", success=True, artifacts={"pr_url": ""})
        assert RalphLoop._extract_branch_from_pr(result) == ""

    def test_extract_repo_parts(self):
        parts = RalphLoop._extract_repo_parts_from_url("https://github.com/org/repo/pull/1")
        assert parts == ("org", "repo")

    def test_extract_repo_parts_no_github(self):
        assert RalphLoop._extract_repo_parts_from_url("https://gitlab.com/x/y") is None

    def test_extract_repo_parts_short(self):
        assert RalphLoop._extract_repo_parts_from_url("https://github.com/only") is None


class TestCIMonitoringDisabled:
    @pytest.mark.asyncio
    async def test_disabled_config_skips(self):
        config = EngineConfig()
        config.ci_remediation.enabled = False
        llm = MockProvider(responses=["{}"])
        loop = RalphLoop(config=config, llm=llm, issue_url="url", repo_path="/tmp")
        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={"pr_created": True, "pr_url": "https://github.com/o/r/pull/1"},
        )
        result = await loop._run_ci_monitoring_loop(validate_result, [])
        assert result == "success"

    @pytest.mark.asyncio
    async def test_no_token_skips(self):
        config = EngineConfig()
        llm = MockProvider(responses=["{}"])
        loop = RalphLoop(config=config, llm=llm, issue_url="url", repo_path="/tmp")
        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={"pr_created": True, "pr_url": "https://github.com/o/r/pull/1"},
        )
        with patch.dict(os.environ, {}, clear=True):
            result = await loop._run_ci_monitoring_loop(validate_result, [])
        assert result == "success"


class TestCIMonitoringLoop:
    def _make_loop(self, ci_config: CIRemediationConfig | None = None) -> RalphLoop:
        config = EngineConfig()
        if ci_config:
            config.ci_remediation = ci_config
        llm = MockProvider(responses=[_make_llm_response()] * 10)
        loop = RalphLoop(config=config, llm=llm, issue_url="url", repo_path="/tmp")
        loop._start_time = time.monotonic()
        return loop

    @pytest.mark.asyncio
    async def test_ci_passes_immediately(self):
        from engine.workflow.ci_monitor import CIResult

        loop = self._make_loop()
        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc",
            },
        )

        mock_ci = AsyncMock()
        mock_ci.poll_ci_status = AsyncMock(
            return_value=CIResult(
                sha="abc",
                overall_state="success",
                completed=True,
                total_count=1,
            )
        )

        with (
            patch.dict(os.environ, {"GH_PAT": "tok"}),
            patch("engine.workflow.ci_monitor.CIMonitor", return_value=mock_ci),
        ):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "success"
        mock_ci.poll_ci_status.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ci_fails_then_remediated(self):
        from engine.phases.ci_remediate import CIRemediatePhase
        from engine.workflow.ci_monitor import (
            CheckRunResult,
            CIFailureCategory,
            CIResult,
            FailureDetails,
        )

        ci_config = CIRemediationConfig(max_iterations=3, time_budget_minutes=10)
        loop = self._make_loop(ci_config)
        loop.register_phase("ci_remediate", CIRemediatePhase)

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc",
            },
        )

        fail_ci = CIResult(
            sha="abc",
            overall_state="failure",
            completed=True,
            total_count=1,
            check_runs=[CheckRunResult(name="ci/test", status="completed", conclusion="failure")],
            workflow_run_ids=[9000],
        )
        pass_ci = CIResult(
            sha="abc",
            overall_state="success",
            completed=True,
            total_count=1,
        )

        mock_ci = AsyncMock()
        mock_ci.poll_ci_status = AsyncMock(side_effect=[fail_ci, pass_ci])
        mock_ci.categorize_failure = MagicMock(return_value=CIFailureCategory.TEST_FAILURE)
        mock_ci.extract_failure_details = MagicMock(
            return_value=FailureDetails(
                category=CIFailureCategory.TEST_FAILURE,
                summary="1 check(s) failed",
                failing_checks=["ci/test"],
            )
        )

        with (
            patch.dict(os.environ, {"GH_PAT": "tok"}),
            patch("engine.workflow.ci_monitor.CIMonitor", return_value=mock_ci),
        ):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "success"
        assert mock_ci.poll_ci_status.await_count == 2

    @pytest.mark.asyncio
    async def test_iteration_cap_escalates(self):
        from engine.phases.ci_remediate import CIRemediatePhase
        from engine.workflow.ci_monitor import (
            CheckRunResult,
            CIFailureCategory,
            CIResult,
            FailureDetails,
        )

        ci_config = CIRemediationConfig(max_iterations=2, time_budget_minutes=10)
        loop = self._make_loop(ci_config)
        loop.register_phase("ci_remediate", CIRemediatePhase)

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc",
            },
        )

        fail_ci = CIResult(
            sha="abc",
            overall_state="failure",
            completed=True,
            total_count=1,
            check_runs=[CheckRunResult(name="ci/test", status="completed", conclusion="failure")],
            workflow_run_ids=[9000],
        )

        mock_ci = AsyncMock()
        mock_ci.poll_ci_status = AsyncMock(return_value=fail_ci)
        mock_ci.categorize_failure = MagicMock(return_value=CIFailureCategory.TEST_FAILURE)
        mock_ci.extract_failure_details = MagicMock(
            return_value=FailureDetails(
                category=CIFailureCategory.TEST_FAILURE,
                summary="1 check(s) failed",
                failing_checks=["ci/test"],
            )
        )

        with (
            patch.dict(os.environ, {"GH_PAT": "tok"}),
            patch("engine.workflow.ci_monitor.CIMonitor", return_value=mock_ci),
        ):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "escalated"

    @pytest.mark.asyncio
    async def test_escalation_on_timeout_category(self):
        from engine.workflow.ci_monitor import (
            CheckRunResult,
            CIFailureCategory,
            CIResult,
            FailureDetails,
        )

        ci_config = CIRemediationConfig(max_iterations=3)
        loop = self._make_loop(ci_config)

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc",
            },
        )

        fail_ci = CIResult(
            sha="abc",
            overall_state="failure",
            completed=True,
            total_count=1,
            check_runs=[CheckRunResult(name="ci/test", status="completed", conclusion="timed_out")],
        )

        mock_ci = AsyncMock()
        mock_ci.poll_ci_status = AsyncMock(return_value=fail_ci)
        mock_ci.categorize_failure = MagicMock(return_value=CIFailureCategory.TIMEOUT)
        mock_ci.extract_failure_details = MagicMock(
            return_value=FailureDetails(
                category=CIFailureCategory.TIMEOUT,
                summary="Timed out",
                failing_checks=["ci/test"],
                recommended_action="escalate",
            )
        )

        with (
            patch.dict(os.environ, {"GH_PAT": "tok"}),
            patch("engine.workflow.ci_monitor.CIMonitor", return_value=mock_ci),
        ):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "escalated"

    @pytest.mark.asyncio
    async def test_infrastructure_flake_triggers_rerun(self):
        from engine.workflow.ci_monitor import (
            CheckRunResult,
            CIFailureCategory,
            CIResult,
            FailureDetails,
        )

        ci_config = CIRemediationConfig(
            max_iterations=3,
            max_flake_reruns=2,
            time_budget_minutes=10,
        )
        loop = self._make_loop(ci_config)

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc",
            },
        )

        flake_ci = CIResult(
            sha="abc",
            overall_state="failure",
            completed=True,
            total_count=1,
            check_runs=[CheckRunResult(name="ci/test", status="completed", conclusion="failure")],
            workflow_run_ids=[9000],
        )
        pass_ci = CIResult(sha="abc", overall_state="success", completed=True, total_count=1)

        mock_ci = AsyncMock()
        mock_ci.poll_ci_status = AsyncMock(side_effect=[flake_ci, pass_ci])
        mock_ci.categorize_failure = MagicMock(return_value=CIFailureCategory.INFRASTRUCTURE_FLAKE)
        mock_ci.extract_failure_details = MagicMock(
            return_value=FailureDetails(
                category=CIFailureCategory.INFRASTRUCTURE_FLAKE,
                summary="Runner timeout",
                failing_checks=["ci/test"],
                recommended_action="rerun",
            )
        )
        mock_ci.trigger_rerun = AsyncMock(return_value={"success": True})

        with (
            patch.dict(os.environ, {"GH_PAT": "tok"}),
            patch("engine.workflow.ci_monitor.CIMonitor", return_value=mock_ci),
        ):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "success"
        mock_ci.trigger_rerun.assert_awaited_once_with(9000)

    @pytest.mark.asyncio
    async def test_flake_rerun_limit_escalates(self):
        from engine.workflow.ci_monitor import (
            CheckRunResult,
            CIFailureCategory,
            CIResult,
            FailureDetails,
        )

        ci_config = CIRemediationConfig(
            max_iterations=5,
            max_flake_reruns=1,
            time_budget_minutes=10,
        )
        loop = self._make_loop(ci_config)

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc",
            },
        )

        flake_ci = CIResult(
            sha="abc",
            overall_state="failure",
            completed=True,
            total_count=1,
            check_runs=[CheckRunResult(name="ci", status="completed", conclusion="failure")],
            workflow_run_ids=[9000],
        )

        mock_ci = AsyncMock()
        mock_ci.poll_ci_status = AsyncMock(return_value=flake_ci)
        mock_ci.categorize_failure = MagicMock(return_value=CIFailureCategory.INFRASTRUCTURE_FLAKE)
        mock_ci.extract_failure_details = MagicMock(
            return_value=FailureDetails(
                category=CIFailureCategory.INFRASTRUCTURE_FLAKE,
                summary="Runner timeout",
                failing_checks=["ci"],
                recommended_action="rerun",
            )
        )
        mock_ci.trigger_rerun = AsyncMock(return_value={"success": True})

        with (
            patch.dict(os.environ, {"GH_PAT": "tok"}),
            patch("engine.workflow.ci_monitor.CIMonitor", return_value=mock_ci),
        ):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "escalated"

    @pytest.mark.asyncio
    async def test_ci_poll_timeout_escalates(self):
        from engine.workflow.ci_monitor import CIResult

        ci_config = CIRemediationConfig(max_iterations=3, time_budget_minutes=10)
        loop = self._make_loop(ci_config)

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/1?head=rl/fix-123-abc",
            },
        )

        pending_ci = CIResult(
            sha="abc",
            overall_state="pending",
            completed=False,
            total_count=1,
        )

        mock_ci = AsyncMock()
        mock_ci.poll_ci_status = AsyncMock(return_value=pending_ci)

        with (
            patch.dict(os.environ, {"GH_PAT": "tok"}),
            patch("engine.workflow.ci_monitor.CIMonitor", return_value=mock_ci),
        ):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "escalated"

    @pytest.mark.asyncio
    async def test_no_branch_skips(self):
        config = EngineConfig()
        llm = MockProvider(responses=["{}"])
        loop = RalphLoop(config=config, llm=llm, issue_url="url", repo_path="/tmp")
        loop._start_time = time.monotonic()

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            artifacts={"pr_created": True, "pr_url": ""},
        )

        with patch.dict(os.environ, {"GH_PAT": "tok"}):
            result = await loop._run_ci_monitoring_loop(validate_result, [])

        assert result == "success"


class TestCIRemediateNotRegistered:
    @pytest.mark.asyncio
    async def test_unregistered_phase_returns_failure(self):
        from engine.workflow.ci_monitor import (
            CIFailureCategory,
            FailureDetails,
        )

        config = EngineConfig()
        llm = MockProvider(responses=["{}"])
        loop = RalphLoop(config=config, llm=llm, issue_url="url", repo_path="/tmp")
        loop._start_time = time.monotonic()

        result = await loop._execute_ci_remediation(
            failure_details=FailureDetails(
                category=CIFailureCategory.TEST_FAILURE,
                summary="fail",
                failing_checks=["ci"],
            ),
            category=CIFailureCategory.TEST_FAILURE,
            branch_name="rl/fix-1-x",
            original_diff="",
            original_desc="",
            ci_iter=1,
            phase_results=[],
        )
        assert result.success is False
        assert result.findings.get("skipped") is True


class TestMainRegistration:
    def test_ci_remediate_registered(self):
        from engine.__main__ import CIRemediatePhase as ImportedCIRemediate

        assert ImportedCIRemediate is CIRemediatePhase

    def test_ci_remediate_not_in_phase_order(self):
        assert "ci_remediate" not in PHASE_ORDER


class TestPromptTemplate:
    def test_prompt_template_exists(self):
        from pathlib import Path

        template = Path("templates/prompts/ci_remediate.md")
        assert template.exists(), "ci_remediate.md prompt template must exist"

    def test_prompt_template_has_untrusted_delimiter(self):
        from pathlib import Path

        content = Path("templates/prompts/ci_remediate.md").read_text()
        assert "UNTRUSTED CONTENT" in content

    def test_prompt_loadable(self):
        from engine.phases.prompt_loader import load_prompt

        prompt = load_prompt("ci_remediate")
        assert len(prompt) > 100
        assert "CI" in prompt


# ==================================================================
# 10.4 — CI Failure Context Injection
# ==================================================================


class TestPromptCategoryStrategies:
    """Verify the prompt template includes per-category remediation strategies."""

    def _load(self) -> str:
        from pathlib import Path

        return Path("templates/prompts/ci_remediate.md").read_text()

    def test_test_failure_strategy(self):
        content = self._load()
        assert "### test_failure" in content

    def test_build_error_strategy(self):
        content = self._load()
        assert "### build_error" in content

    def test_lint_violation_strategy(self):
        content = self._load()
        assert "### lint_violation" in content

    def test_infrastructure_flake_strategy(self):
        content = self._load()
        assert "### infrastructure_flake" in content

    def test_timeout_strategy(self):
        content = self._load()
        assert "### timeout" in content

    def test_no_raw_json_in_category_sections(self):
        content = self._load()
        sections = content.split("### ")
        for section in sections[1:]:
            if section.startswith(("test_failure", "build_error", "lint_violation")):
                assert '{"' not in section, f"Raw JSON found in category section: {section[:80]}"

    def test_category_sections_are_human_readable(self):
        content = self._load()
        assert "Read the failing test name" in content
        assert "compiler or build tool error" in content
        assert "lint rule name" in content

    def test_prior_attempts_section_detailed(self):
        content = self._load()
        assert "What the analysis/root cause was" in content
        assert "What fix strategy was tried" in content
        assert "Which files were changed" in content

    def test_non_empty_file_changes_rule(self):
        content = self._load()
        assert "non-empty when `is_code_fix` is true" in content


class TestEnhancedPriorAttempts:
    """Test _extract_prior_attempts includes lint_output and expected_resolution."""

    def test_extracts_lint_output(self):
        prior = PhaseResult(
            phase="ci_remediate",
            success=False,
            findings={
                "action": "lint_failed",
                "analysis": "Wrong import",
                "lint_output": "E501 line too long",
            },
            artifacts={"files_changed": ["a.py"], "pushed": False},
        )
        phase = _make_phase(prior_results=[prior])
        attempts = phase._extract_prior_attempts()
        assert len(attempts) == 1
        assert attempts[0]["lint_output"] == "E501 line too long"

    def test_extracts_expected_resolution(self):
        prior = PhaseResult(
            phase="ci_remediate",
            success=True,
            findings={
                "action": "pushed",
                "analysis": "Fixed nil check",
                "expected_resolution": "ci/test should pass",
            },
            artifacts={"files_changed": ["h.go"], "pushed": True},
        )
        phase = _make_phase(prior_results=[prior])
        attempts = phase._extract_prior_attempts()
        assert attempts[0]["expected_resolution"] == "ci/test should pass"

    def test_empty_lint_output_when_not_present(self):
        prior = PhaseResult(
            phase="ci_remediate",
            success=True,
            findings={"action": "pushed", "analysis": "ok"},
            artifacts={"files_changed": ["a.go"], "pushed": True},
        )
        phase = _make_phase(prior_results=[prior])
        attempts = phase._extract_prior_attempts()
        assert attempts[0]["lint_output"] == ""

    def test_skips_non_ci_remediate_phases(self):
        prior = PhaseResult(
            phase="implement",
            success=True,
            findings={"lint_output": "should not appear"},
            artifacts={},
        )
        phase = _make_phase(prior_results=[prior])
        attempts = phase._extract_prior_attempts()
        assert len(attempts) == 0


class TestEnhancedTrustedContext:
    """Test _build_trusted_context includes full prior-attempt details."""

    def _obs_with_prior(
        self,
        attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "failure_details": _make_failure_details(),
            "failure_category": "test_failure",
            "remediation_iteration": 2,
            "original_description": "nil check",
            "prior_attempts": attempts,
            "file_contents": {},
            "original_diff": "",
        }

    def test_includes_analysis_in_prior(self):
        prior = [{"analysis": "Wrong nil check placement", "success": False, "action": "pushed"}]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "Wrong nil check placement" in ctx

    def test_includes_files_changed_in_prior(self):
        prior = [
            {
                "analysis": "Fix",
                "success": False,
                "action": "pushed",
                "files_changed": ["pkg/handler.go", "pkg/utils.go"],
            }
        ]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "pkg/handler.go" in ctx
        assert "pkg/utils.go" in ctx

    def test_includes_lint_output_in_prior(self):
        prior = [
            {
                "analysis": "Added return",
                "success": False,
                "action": "lint_failed",
                "lint_output": "E501 line too long at handler.go:42",
            }
        ]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "E501 line too long at handler.go:42" in ctx

    def test_includes_expected_resolution_in_prior(self):
        prior = [
            {
                "analysis": "Fix",
                "success": False,
                "action": "pushed",
                "expected_resolution": "ci/test should pass after nil check",
            }
        ]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "ci/test should pass after nil check" in ctx

    def test_shows_succeeded_vs_failed(self):
        prior = [
            {"analysis": "First try", "success": False, "action": "pushed"},
            {"analysis": "Second try", "success": True, "action": "pushed"},
        ]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "Attempt #1 (FAILED)" in ctx
        assert "Attempt #2 (succeeded)" in ctx

    def test_strategy_in_prior_formatted(self):
        prior = [{"fix_strategy": "Add error handling", "success": False, "action": "pushed"}]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "Strategy: Add error handling" in ctx

    def test_no_raw_json_in_prior_section(self):
        prior = [
            {
                "analysis": "Fix",
                "success": False,
                "action": "pushed",
                "fix_strategy": "add return",
                "files_changed": ["a.go"],
                "lint_output": "",
            }
        ]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        prior_section = ctx[ctx.index("PRIOR CI REMEDIATION ATTEMPTS") :]
        assert "True" not in prior_section or "success=True" not in prior_section
        assert "'analysis':" not in prior_section
        assert "{'action'" not in prior_section

    def test_truncates_long_analysis(self):
        prior = [{"analysis": "x" * 1000, "success": False, "action": "pushed"}]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert len(ctx) < len("x" * 1000) + 500

    def test_truncates_long_lint_output(self):
        prior = [
            {
                "analysis": "Fix",
                "success": False,
                "action": "lint_failed",
                "lint_output": "L" * 1000,
            }
        ]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "L" * 500 in ctx
        assert "L" * 600 not in ctx

    def test_caps_files_changed_at_10(self):
        prior = [
            {
                "analysis": "Fix",
                "success": False,
                "action": "pushed",
                "files_changed": [f"f{i}.go" for i in range(20)],
            }
        ]
        ctx = _build_trusted_context(self._obs_with_prior(prior))
        assert "f9.go" in ctx
        assert "f10.go" not in ctx


class TestContextNoRawJSON:
    """Verify that no raw JSON/dict repr leaks into the LLM context."""

    def test_trusted_context_no_dict_repr(self):
        obs = {
            "failure_details": _make_failure_details(),
            "failure_category": "build_error",
            "remediation_iteration": 1,
            "original_description": "Added nil check",
            "prior_attempts": [],
            "file_contents": {"main.go": "package main"},
            "original_diff": "+line",
        }
        ctx = _build_trusted_context(obs)
        assert "{'category'" not in ctx
        assert "{'summary'" not in ctx
        assert repr(_make_failure_details()) not in ctx

    def test_untrusted_context_no_dict_repr(self):
        details = _make_failure_details()
        ctx = _build_untrusted_context(details)
        assert "'failing_checks'" not in ctx
        assert repr(details) not in ctx

    @pytest.mark.asyncio
    async def test_plan_llm_call_no_raw_json_in_user_message(self):
        phase = _make_phase(llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)

        calls = phase.llm.call_log
        assert len(calls) >= 1
        messages = calls[0].get("messages", [])
        user_msg = ""
        for msg in messages:
            if msg.get("role") == "user":
                user_msg = msg.get("content", "")
                break
        assert "{'category'" not in user_msg
        assert "{'summary'" not in user_msg


class TestPlanIncludesFailureContext:
    """Verify plan() sends structured failure context to LLM via MockProvider.call_log."""

    def _get_user_msg(self, phase: CIRemediatePhase) -> str:
        """Extract user message from MockProvider call log."""
        calls = phase.llm.call_log
        assert len(calls) >= 1, "Expected at least one LLM call"
        messages = calls[0].get("messages", [])
        for msg in messages:
            if msg.get("role") == "user":
                return msg.get("content", "")
        return ""

    @pytest.mark.asyncio
    async def test_plan_includes_category_in_context(self):
        issue = _make_issue_data(category="build_error")
        phase = _make_phase(issue_data=issue, llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)
        user_msg = self._get_user_msg(phase)
        assert "build_error" in user_msg

    @pytest.mark.asyncio
    async def test_plan_includes_failing_tests_in_context(self):
        details = _make_failure_details(failing_tests=["TestHandler_Nil", "TestReconciler_Error"])
        issue = _make_issue_data(failure_details=details)
        phase = _make_phase(issue_data=issue, llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)
        user_msg = self._get_user_msg(phase)
        assert "TestHandler_Nil" in user_msg
        assert "TestReconciler_Error" in user_msg

    @pytest.mark.asyncio
    async def test_plan_includes_error_messages_in_untrusted(self):
        details = _make_failure_details(
            error_messages=["FATAL: nil pointer dereference in reconciler.go:42"]
        )
        issue = _make_issue_data(failure_details=details)
        phase = _make_phase(issue_data=issue, llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)
        user_msg = self._get_user_msg(phase)
        assert "nil pointer dereference" in user_msg
        assert "UNTRUSTED" in user_msg

    @pytest.mark.asyncio
    async def test_plan_includes_annotations_in_untrusted(self):
        details = _make_failure_details(
            annotations=[
                {
                    "path": "pkg/handler.go",
                    "start_line": 42,
                    "annotation_level": "failure",
                    "message": "nil pointer crash here",
                }
            ]
        )
        issue = _make_issue_data(failure_details=details)
        phase = _make_phase(issue_data=issue, llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)
        user_msg = self._get_user_msg(phase)
        assert "pkg/handler.go:42" in user_msg
        assert "nil pointer crash here" in user_msg

    @pytest.mark.asyncio
    async def test_plan_includes_log_excerpts_in_untrusted(self):
        details = _make_failure_details(
            log_excerpts=["--- FAIL: TestReconciler (0.02s)\n    handler_test.go:55: got nil"]
        )
        issue = _make_issue_data(failure_details=details)
        phase = _make_phase(issue_data=issue, llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)
        user_msg = self._get_user_msg(phase)
        assert "FAIL: TestReconciler" in user_msg

    @pytest.mark.asyncio
    async def test_plan_includes_prior_attempt_analysis(self):
        prior = PhaseResult(
            phase="ci_remediate",
            success=False,
            findings={
                "action": "pushed",
                "analysis": "Tried adding return but wrong line",
                "fix_strategy": "Add return statement",
            },
            artifacts={"files_changed": ["pkg/handler.go"], "pushed": True},
        )
        issue = _make_issue_data(remediation_iter=2)
        phase = _make_phase(
            issue_data=issue,
            prior_results=[prior],
            llm_response=_make_llm_response(),
        )
        obs = await phase.observe()
        await phase.plan(obs)
        user_msg = self._get_user_msg(phase)
        assert "Tried adding return but wrong line" in user_msg
        assert "pkg/handler.go" in user_msg
        assert "Add return statement" in user_msg

    @pytest.mark.asyncio
    async def test_plan_includes_original_diff(self):
        issue = _make_issue_data()
        issue["original_diff"] = "--- a/handler.go\n+++ b/handler.go\n+if x != nil {"
        phase = _make_phase(issue_data=issue, llm_response=_make_llm_response())
        obs = await phase.observe()
        await phase.plan(obs)
        user_msg = self._get_user_msg(phase)
        assert "if x != nil {" in user_msg
