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
from engine.phases.implement import ImplementPhase, parse_implement_response
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
