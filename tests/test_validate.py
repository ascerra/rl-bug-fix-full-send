"""Tests for the Validation Phase — final checks, PR creation, minimal diff verification."""

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
from engine.phases.validate import ValidatePhase, parse_validate_response
from engine.tools.executor import ToolExecutor

# ------------------------------------------------------------------
# Helpers: canned LLM responses
# ------------------------------------------------------------------


def _ready_response(
    confidence: float = 0.95,
    diff_is_minimal: bool = True,
) -> str:
    """Return a JSON string representing a passing validation."""
    return json.dumps(
        {
            "tests_passing": True,
            "test_summary": "10 passed, 0 failed",
            "linters_passing": True,
            "lint_issues": [],
            "diff_is_minimal": diff_is_minimal,
            "unnecessary_changes": [],
            "pr_description": (
                "## Bug Fix: nil pointer panic in reconciler\n\n"
                "**Issue**: https://github.com/test/repo/issues/42\n\n"
                "**Root cause**: Nil pointer dereference when owner ref is nil.\n\n"
                "**Fix**: Added nil check before accessing owner reference.\n\n"
                "**Testing**: All existing tests pass. No new tests needed.\n\n"
                "**Risks**: None identified."
            ),
            "ready_to_submit": True,
            "blocking_issues": [],
            "confidence": confidence,
        }
    )


def _not_ready_response(
    blocking: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "tests_passing": False,
            "test_summary": "8 passed, 2 failed",
            "linters_passing": True,
            "lint_issues": [],
            "diff_is_minimal": True,
            "unnecessary_changes": [],
            "pr_description": "",
            "ready_to_submit": False,
            "blocking_issues": blocking or ["2 tests failing in pkg/controller"],
            "confidence": 0.4,
        }
    )


def _not_minimal_response() -> str:
    return json.dumps(
        {
            "tests_passing": True,
            "test_summary": "10 passed",
            "linters_passing": True,
            "lint_issues": [],
            "diff_is_minimal": False,
            "unnecessary_changes": ["Reformatted unrelated file main.go"],
            "pr_description": "Bug fix with unnecessary changes.",
            "ready_to_submit": False,
            "blocking_issues": ["Diff contains unnecessary changes"],
            "confidence": 0.6,
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


def _review_phase_result() -> PhaseResult:
    return PhaseResult(
        phase="review",
        success=True,
        should_continue=True,
        next_phase="validate",
        findings={
            "verdict": "approve",
            "confidence": 0.9,
            "summary": "Fix correctly addresses the nil pointer dereference.",
        },
        artifacts={
            "review_report": {
                "verdict": "approve",
                "confidence": 0.9,
                "summary": "Fix correctly addresses the nil pointer dereference.",
                "findings": [],
                "scope_assessment": "bug_fix",
                "injection_detected": False,
            },
            "verified_findings": [],
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


def _make_validate(
    responses: list[str] | None = None,
    repo_path: str = "/tmp/fake-repo",
    issue_data: dict[str, Any] | None = None,
    config: EngineConfig | None = None,
    tool_executor: ToolExecutor | None = None,
    prior_results: list[PhaseResult] | None = None,
) -> ValidatePhase:
    llm = MockProvider(responses=responses or [_ready_response()])
    return ValidatePhase(
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
            else [_triage_phase_result(), _impl_phase_result(), _review_phase_result()]
        ),
    )


def _make_validate_with_repo(
    tmp_path: Path,
    responses: list[str] | None = None,
    config: EngineConfig | None = None,
) -> ValidatePhase:
    """Create a ValidatePhase with a real temp repo and ToolExecutor."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg" / "controller").mkdir(parents=True)
    (repo / "pkg" / "controller" / "reconciler.go").write_text(
        "package controller\n\nfunc Reconcile() error {\n"
        "\tif owner == nil {\n\t\treturn nil\n\t}\n"
        "\treturn nil\n}\n"
    )

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    tracer = Tracer()
    logger = StructuredLogger()
    metrics = LoopMetrics()
    tool_executor = ToolExecutor(
        repo_path=str(repo),
        logger=logger,
        tracer=tracer,
        metrics=metrics,
        allowed_tools=["file_read", "file_search", "shell_run", "git_diff", "github_api"],
    )

    return ValidatePhase(
        llm=MockProvider(responses=responses or [_ready_response()]),
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
        prior_phase_results=[
            _triage_phase_result(),
            _impl_phase_result(),
            _review_phase_result(),
        ],
    )


# ------------------------------------------------------------------
# parse_validate_response tests
# ------------------------------------------------------------------


class TestParseValidate:
    def test_direct_json(self):
        raw = _ready_response()
        result = parse_validate_response(raw)
        assert result["ready_to_submit"] is True
        assert result["confidence"] == 0.95

    def test_json_code_block(self):
        raw = "Validation results:\n```json\n" + _ready_response() + "\n```\nDone."
        result = parse_validate_response(raw)
        assert result["ready_to_submit"] is True

    def test_generic_code_block(self):
        raw = "Results:\n```\n" + _ready_response() + "\n```"
        result = parse_validate_response(raw)
        assert result["ready_to_submit"] is True

    def test_malformed_returns_not_ready_default(self):
        result = parse_validate_response("This is not JSON at all.")
        assert result["ready_to_submit"] is False
        assert result["confidence"] == 0.0
        assert len(result["blocking_issues"]) >= 1
        assert "Parse failure" in result["blocking_issues"][0]

    def test_empty_string(self):
        result = parse_validate_response("")
        assert result["ready_to_submit"] is False

    def test_partial_json(self):
        result = parse_validate_response('{"ready_to_submit": true')
        assert result["ready_to_submit"] is False

    def test_multiple_code_blocks_picks_valid(self):
        raw = "```\nnot json\n```\n\n```json\n" + _ready_response() + "\n```"
        result = parse_validate_response(raw)
        assert result["ready_to_submit"] is True

    def test_not_ready_parse(self):
        result = parse_validate_response(_not_ready_response())
        assert result["ready_to_submit"] is False
        assert len(result["blocking_issues"]) >= 1

    def test_not_minimal_parse(self):
        result = parse_validate_response(_not_minimal_response())
        assert result["diff_is_minimal"] is False
        assert len(result["unnecessary_changes"]) >= 1


# ------------------------------------------------------------------
# ValidatePhase.observe tests
# ------------------------------------------------------------------


class TestValidateObserve:
    @pytest.mark.asyncio
    async def test_observe_extracts_review_report(self):
        phase = _make_validate(tool_executor=None)
        obs = await phase.observe()
        assert obs["review_report"]["verdict"] == "approve"

    @pytest.mark.asyncio
    async def test_observe_extracts_impl_diff(self):
        phase = _make_validate(tool_executor=None)
        obs = await phase.observe()
        assert "owner == nil" in obs["diff"]
        assert obs["files_changed"] == ["pkg/controller/reconciler.go"]

    @pytest.mark.asyncio
    async def test_observe_without_prior_results(self):
        phase = _make_validate(prior_results=[], tool_executor=None)
        obs = await phase.observe()
        assert obs["review_report"] == {}
        assert obs["diff"] == ""
        assert obs["files_changed"] == []

    @pytest.mark.asyncio
    async def test_observe_includes_issue_data(self):
        phase = _make_validate(tool_executor=None)
        obs = await phase.observe()
        assert obs["issue"]["url"] == "https://github.com/test/repo/issues/42"

    @pytest.mark.asyncio
    async def test_observe_reads_changed_files(self, tmp_path):
        phase = _make_validate_with_repo(tmp_path)
        obs = await phase.observe()
        assert "pkg/controller/reconciler.go" in obs["file_contents"]
        assert "package controller" in obs["file_contents"]["pkg/controller/reconciler.go"]


# ------------------------------------------------------------------
# ValidatePhase.plan tests
# ------------------------------------------------------------------


class TestValidatePlan:
    @pytest.mark.asyncio
    async def test_plan_calls_llm(self):
        cfg = EngineConfig()
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False
        phase = _make_validate(config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["validate_result"]["ready_to_submit"] is True
        assert "raw_llm_response" in plan

    @pytest.mark.asyncio
    async def test_plan_records_llm_call(self):
        tracer = Tracer()
        cfg = EngineConfig()
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False
        phase = ValidatePhase(
            llm=MockProvider(responses=[_ready_response()]),
            logger=StructuredLogger(),
            tracer=tracer,
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=cfg,
            prior_phase_results=[
                _triage_phase_result(),
                _impl_phase_result(),
                _review_phase_result(),
            ],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        llm_actions = [a for a in tracer.get_actions() if a.action_type == "llm_query"]
        assert len(llm_actions) == 1
        assert llm_actions[0].llm_context["provider"] == "mock"

    @pytest.mark.asyncio
    async def test_plan_wraps_untrusted_content(self):
        llm = MockProvider(responses=[_ready_response()])
        cfg = EngineConfig()
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False
        phase = ValidatePhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "untrusted validate body"},
            config=cfg,
            prior_phase_results=[
                _triage_phase_result(),
                _impl_phase_result(),
                _review_phase_result(),
            ],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        call = llm.call_log[0]
        msg = call["messages"][0]["content"]
        assert "UNTRUSTED CONTENT BELOW" in msg
        assert "END UNTRUSTED CONTENT" in msg
        assert "untrusted validate body" in msg

    @pytest.mark.asyncio
    async def test_plan_includes_review_verdict(self):
        llm = MockProvider(responses=[_ready_response()])
        cfg = EngineConfig()
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False
        phase = ValidatePhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=cfg,
            prior_phase_results=[
                _triage_phase_result(),
                _impl_phase_result(),
                _review_phase_result(),
            ],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "approve" in msg.lower()

    @pytest.mark.asyncio
    async def test_plan_tests_skipped_when_disabled(self):
        cfg = EngineConfig()
        cfg.phases.validate.ci_equivalent = False
        phase = _make_validate(config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["test_result"]["passed"] is True
        output = plan["test_result"]["output"].lower()
        assert "not run" in output or "skipped" in output
        assert plan["lint_result"]["passed"] is True
        assert "skipped" in plan["lint_result"]["output"].lower()


# ------------------------------------------------------------------
# ValidatePhase.act tests
# ------------------------------------------------------------------


class TestValidateAct:
    @pytest.mark.asyncio
    async def test_act_no_pr_without_tools(self):
        phase = _make_validate(tool_executor=None)
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {"issue": {"url": "u"}, "diff": "d"},
            }
        )
        assert result["pr_created"] is False
        assert result["pr_url"] == ""

    @pytest.mark.asyncio
    async def test_act_no_pr_when_not_ready(self, tmp_path):
        phase = _make_validate_with_repo(tmp_path, responses=[_not_ready_response()])
        result = await phase.act(
            {
                "validate_result": json.loads(_not_ready_response()),
                "test_result": {"passed": False, "output": "FAIL"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {"issue": {"url": "u"}, "diff": "d"},
            }
        )
        assert result["pr_created"] is False

    @pytest.mark.asyncio
    async def test_act_records_actions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GH_PAT", "fake-token-for-test")
        phase = _make_validate_with_repo(tmp_path)
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {
                    "issue": {"url": "https://github.com/test/repo/issues/42"},
                    "diff": "d",
                },
            }
        )
        action_types = [a["action"] for a in result["actions"]]
        assert "git_checkout_branch" in action_types
        assert "git_push" in action_types
        assert "create_pr" in action_types

    @pytest.mark.asyncio
    async def test_act_cross_fork_pr_head(self, tmp_path, monkeypatch):
        """When RL_FORK_REPO is set, PR head uses 'fork_owner:branch' format."""
        monkeypatch.setenv("RL_FORK_REPO", "myuser/build-definitions")
        monkeypatch.setenv("GH_PAT", "fake-token-for-test")
        phase = _make_validate_with_repo(tmp_path)
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {
                    "issue": {"url": "https://github.com/upstream/repo/issues/1"},
                    "diff": "d",
                },
            }
        )
        action_types = [a["action"] for a in result["actions"]]
        assert "git_checkout_branch" in action_types
        assert "git_push" in action_types
        assert "create_pr" in action_types


# ------------------------------------------------------------------
# ValidatePhase.validate tests
# ------------------------------------------------------------------


class TestValidateValidate:
    @pytest.mark.asyncio
    async def test_validate_all_passing(self):
        phase = _make_validate()
        validation = await phase.validate(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "10 passed"},
                "lint_result": {"passed": True, "output": "OK"},
                "pr_created": True,
                "pr_url": "https://github.com/test/repo/pull/1",
            }
        )
        assert validation["valid"] is True
        assert validation["issues"] == []
        assert validation["tests_passing"] is True
        assert validation["linters_passing"] is True

    @pytest.mark.asyncio
    async def test_validate_tests_failing(self):
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "required"
        phase = _make_validate(config=cfg)
        validation = await phase.validate(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": False, "output": "2 FAILED"},
                "lint_result": {"passed": True, "output": "OK"},
                "pr_created": False,
                "pr_url": "",
            }
        )
        assert validation["valid"] is False
        assert any("Tests failing" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_linters_failing(self):
        phase = _make_validate()
        validation = await phase.validate(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": False, "output": "E501 line too long"},
                "pr_created": False,
                "pr_url": "",
            }
        )
        assert validation["valid"] is False
        assert any("Linters failing" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_invalid_confidence(self):
        phase = _make_validate()
        vr = json.loads(_ready_response())
        vr["confidence"] = 1.5
        validation = await phase.validate(
            {
                "validate_result": vr,
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
            }
        )
        assert validation["valid"] is False
        assert any("Invalid confidence" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_negative_confidence(self):
        phase = _make_validate()
        vr = json.loads(_ready_response())
        vr["confidence"] = -0.1
        validation = await phase.validate(
            {
                "validate_result": vr,
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
            }
        )
        assert validation["valid"] is False

    @pytest.mark.asyncio
    async def test_validate_missing_pr_description(self):
        phase = _make_validate()
        vr = json.loads(_ready_response())
        vr["pr_description"] = ""
        validation = await phase.validate(
            {
                "validate_result": vr,
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
            }
        )
        assert validation["valid"] is False
        assert any("Missing PR description" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_blocking_issues(self):
        phase = _make_validate()
        vr = json.loads(_not_ready_response())
        validation = await phase.validate(
            {
                "validate_result": vr,
                "test_result": {"passed": False, "output": "FAIL"},
                "lint_result": {"passed": True, "output": "OK"},
            }
        )
        assert validation["valid"] is False
        assert any("Blocking" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_validate_pr_url_passthrough(self):
        phase = _make_validate()
        validation = await phase.validate(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "pr_created": True,
                "pr_url": "https://github.com/test/repo/pull/99",
            }
        )
        assert validation["pr_url"] == "https://github.com/test/repo/pull/99"
        assert validation["pr_created"] is True


# ------------------------------------------------------------------
# ValidatePhase.reflect tests
# ------------------------------------------------------------------


class TestValidateReflect:
    @pytest.mark.asyncio
    async def test_valid_advances_to_report(self):
        phase = _make_validate()
        result = await phase.reflect(
            {
                "valid": True,
                "issues": [],
                "tests_passing": True,
                "linters_passing": True,
                "diff_is_minimal": True,
                "ready_to_submit": True,
                "pr_created": True,
                "pr_url": "https://github.com/test/repo/pull/1",
                "validate_result": json.loads(_ready_response()),
            }
        )
        assert result.success is True
        assert result.next_phase == "report"
        assert result.escalate is False
        assert result.artifacts["pr_url"] == "https://github.com/test/repo/pull/1"

    @pytest.mark.asyncio
    async def test_tests_failing_backtracks_to_implement(self):
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "required"
        phase = _make_validate(config=cfg)
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Tests failing"],
                "tests_passing": False,
                "linters_passing": True,
                "validate_result": json.loads(_not_ready_response()),
            }
        )
        assert result.success is False
        assert result.next_phase == "implement"
        assert result.should_continue is True
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_linters_failing_backtracks_to_implement(self):
        phase = _make_validate()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Linters failing"],
                "tests_passing": True,
                "linters_passing": False,
                "validate_result": {},
            }
        )
        assert result.success is False
        assert result.next_phase == "implement"
        assert result.should_continue is True

    @pytest.mark.asyncio
    async def test_other_issues_retry(self):
        phase = _make_validate()
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Missing PR description"],
                "tests_passing": True,
                "linters_passing": True,
                "validate_result": {},
            }
        )
        assert result.success is False
        assert result.should_continue is True
        assert result.next_phase == ""
        assert result.escalate is False

    @pytest.mark.asyncio
    async def test_success_artifacts_populated(self):
        phase = _make_validate()
        vr = json.loads(_ready_response())
        result = await phase.reflect(
            {
                "valid": True,
                "issues": [],
                "tests_passing": True,
                "linters_passing": True,
                "diff_is_minimal": True,
                "ready_to_submit": True,
                "pr_created": True,
                "pr_url": "https://github.com/test/repo/pull/5",
                "validate_result": vr,
            }
        )
        assert result.artifacts["pr_url"] == "https://github.com/test/repo/pull/5"
        assert result.artifacts["pr_created"] is True
        assert result.artifacts["tests_passing"] is True
        assert result.artifacts["linters_passing"] is True
        assert result.artifacts["diff_is_minimal"] is True
        assert "Bug Fix" in result.artifacts["pr_description"]


# ------------------------------------------------------------------
# Full execute lifecycle tests
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_ready_success():
    """Full execute() with passing validation → success, next_phase=report."""
    cfg = EngineConfig()
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    phase = _make_validate(config=cfg)
    result = await phase.execute()
    assert result.success is True
    assert result.next_phase == "report"
    assert result.findings["ready_to_submit"] is True


@pytest.mark.asyncio
async def test_execute_not_ready():
    """Full execute() with failing validation → backtrack to implement."""
    cfg = EngineConfig()
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    phase = _make_validate(responses=[_not_ready_response()], config=cfg)
    result = await phase.execute()
    assert result.success is False
    assert result.should_continue is True


@pytest.mark.asyncio
async def test_execute_malformed_llm_response():
    """Full execute() with unparseable LLM output → retry."""
    cfg = EngineConfig()
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    phase = _make_validate(responses=["Not JSON at all!"], config=cfg)
    result = await phase.execute()
    assert result.success is False
    assert result.should_continue is True


@pytest.mark.asyncio
async def test_execute_with_real_repo(tmp_path):
    """Full execute() against a temp repo with real ToolExecutor."""
    cfg = EngineConfig()
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    phase = _make_validate_with_repo(tmp_path, config=cfg)
    result = await phase.execute()
    assert result.phase == "validate"
    assert isinstance(result.findings, dict)


@pytest.mark.asyncio
async def test_execute_no_prior_results():
    """Execute() when there are no prior phase results — still runs."""
    cfg = EngineConfig()
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    phase = _make_validate(prior_results=[], config=cfg)
    result = await phase.execute()
    assert result.phase == "validate"


@pytest.mark.asyncio
async def test_execute_artifacts_populated():
    """Success result includes PR info and test/lint status."""
    cfg = EngineConfig()
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    phase = _make_validate(config=cfg)
    result = await phase.execute()
    assert result.success is True
    assert "pr_created" in result.artifacts
    assert "tests_passing" in result.artifacts
    assert "linters_passing" in result.artifacts
    assert "pr_description" in result.artifacts


# ------------------------------------------------------------------
# Review/impl artifact extraction tests
# ------------------------------------------------------------------


class TestArtifactExtraction:
    def test_extract_review_from_artifacts(self):
        phase = _make_validate(
            prior_results=[
                _triage_phase_result(),
                _impl_phase_result(),
                _review_phase_result(),
            ],
            tool_executor=None,
        )
        report = phase._extract_review_report()
        assert report["verdict"] == "approve"

    def test_extract_review_from_findings_fallback(self):
        review = PhaseResult(
            phase="review",
            success=True,
            findings={"verdict": "approve", "summary": "LGTM"},
        )
        phase = _make_validate(
            prior_results=[_triage_phase_result(), _impl_phase_result(), review],
            tool_executor=None,
        )
        report = phase._extract_review_report()
        assert report["verdict"] == "approve"

    def test_extract_review_empty_when_no_review(self):
        phase = _make_validate(
            prior_results=[_triage_phase_result(), _impl_phase_result()],
            tool_executor=None,
        )
        report = phase._extract_review_report()
        assert report == {}

    def test_extract_review_skips_failed(self):
        failed_review = PhaseResult(
            phase="review",
            success=False,
            findings={"verdict": "block"},
        )
        phase = _make_validate(
            prior_results=[_triage_phase_result(), _impl_phase_result(), failed_review],
            tool_executor=None,
        )
        report = phase._extract_review_report()
        assert report == {}

    def test_extract_impl_diff(self):
        phase = _make_validate(
            prior_results=[
                _triage_phase_result(),
                _impl_phase_result(),
                _review_phase_result(),
            ],
            tool_executor=None,
        )
        artifacts = phase._extract_impl_artifacts()
        assert "owner == nil" in artifacts["diff"]
        assert artifacts["files_changed"] == ["pkg/controller/reconciler.go"]

    def test_extract_impl_empty_when_no_impl(self):
        phase = _make_validate(
            prior_results=[_triage_phase_result()],
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
        phase = _make_validate(
            prior_results=[_triage_phase_result(), impl1, impl2, _review_phase_result()],
            tool_executor=None,
        )
        artifacts = phase._extract_impl_artifacts()
        assert artifacts["diff"] == "new diff"

    def test_repo_endpoint_extraction(self):
        assert (
            ValidatePhase._extract_repo_endpoint("https://github.com/test/repo/issues/42")
            == "test/repo"
        )

    def test_repo_endpoint_extraction_no_github(self):
        assert ValidatePhase._extract_repo_endpoint("https://gitlab.com/foo/bar") == ""

    def test_repo_endpoint_extraction_empty(self):
        assert ValidatePhase._extract_repo_endpoint("") == ""


# ------------------------------------------------------------------
# Integration with loop
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_phase_in_loop(tmp_path):
    """ValidatePhase can be registered and executed in RalphLoop."""
    import subprocess

    from engine.loop import RalphLoop
    from engine.phases.implement import ImplementPhase
    from engine.phases.review import ReviewPhase
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
    review_resp = json.dumps(
        {
            "verdict": "approve",
            "findings": [],
            "scope_assessment": "bug_fix",
            "injection_detected": False,
            "confidence": 0.9,
            "summary": "Fix is correct.",
        }
    )
    validate_resp = _ready_response()

    cfg = EngineConfig()
    cfg.phases.implement.run_tests_after_each_edit = False
    cfg.phases.implement.run_linters = False
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False

    loop = RalphLoop(
        config=cfg,
        llm=MockProvider(responses=[triage_resp, impl_resp, review_resp, validate_resp]),
        issue_url="https://github.com/test/repo/issues/1",
        repo_path=str(repo),
        output_dir=str(out),
    )
    loop.register_phase("triage", TriagePhase)
    loop.register_phase("implement", ImplementPhase)
    loop.register_phase("review", ReviewPhase)
    loop.register_phase("validate", ValidatePhase)

    execution = await loop.run()
    phases_run = [it["phase"] for it in execution.iterations]
    assert "triage" in phases_run
    assert "implement" in phases_run
    assert "review" in phases_run
    assert "validate" in phases_run


# ------------------------------------------------------------------
# ClassVar and config tests
# ------------------------------------------------------------------


class TestValidateClassProperties:
    def test_name(self):
        assert ValidatePhase.name == "validate"

    def test_allowed_tools_falls_back_to_validate_set(self):
        tools = ValidatePhase.get_allowed_tools()
        assert "file_read" in tools
        assert "file_search" in tools
        assert "shell_run" in tools
        assert "git_diff" in tools
        assert "github_api" in tools
        assert "file_write" not in tools
        assert "git_commit" not in tools

    def test_config_validate_flags(self):
        cfg = EngineConfig()
        assert cfg.phases.validate.full_test_suite is False
        assert cfg.phases.validate.ci_equivalent is False
        assert cfg.phases.validate.minimal_diff_check is True
        assert cfg.phases.validate.test_execution_mode == "disabled"
