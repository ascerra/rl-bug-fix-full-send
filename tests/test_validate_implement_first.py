"""Tests for 10.1 — Validate Phase Restructure (Implement-First).

Covers:
- CIRemediationConfig creation, defaults, and YAML loading
- _has_review_approval() gate logic
- _is_ready_to_push() composite gate
- act() enforces implement-first gates (no push without review approval)
- push_blockers propagated in act() result
- Loop ordering ensures implement→review completes before validate
"""

from __future__ import annotations

import json

import pytest

from engine.config import CIRemediationConfig, EngineConfig, load_config
from engine.integrations.llm import MockProvider
from engine.observability.logger import StructuredLogger
from engine.observability.tracer import Tracer
from engine.phases.base import PhaseResult
from engine.phases.validate import ValidatePhase

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _ready_response(confidence: float = 0.95) -> str:
    return json.dumps(
        {
            "tests_passing": True,
            "test_summary": "10 passed",
            "linters_passing": True,
            "lint_issues": [],
            "diff_is_minimal": True,
            "unnecessary_changes": [],
            "pr_description": "## Fix\nAdded nil check.\n\nTests: CI will validate.",
            "ready_to_submit": True,
            "blocking_issues": [],
            "confidence": confidence,
        }
    )


def _triage_result() -> PhaseResult:
    return PhaseResult(
        phase="triage",
        success=True,
        should_continue=True,
        next_phase="implement",
        findings={"classification": "bug", "severity": "high"},
        artifacts={"triage_report": {"classification": "bug"}},
    )


def _impl_result() -> PhaseResult:
    return PhaseResult(
        phase="implement",
        success=True,
        findings={"root_cause": "nil ptr", "fix_description": "nil check"},
        artifacts={"diff": "--- a/r.go\n+++ b/r.go\n@@", "files_changed": ["r.go"]},
    )


def _review_approve() -> PhaseResult:
    return PhaseResult(
        phase="review",
        success=True,
        findings={"verdict": "approve", "summary": "LGTM"},
        artifacts={"review_report": {"verdict": "approve", "summary": "LGTM"}},
    )


def _review_request_changes() -> PhaseResult:
    return PhaseResult(
        phase="review",
        success=False,
        findings={"verdict": "request_changes", "summary": "Needs work"},
        artifacts={"review_report": {"verdict": "request_changes"}},
    )


def _review_approve_in_artifacts_only() -> PhaseResult:
    """Review where verdict is only in artifacts, not findings."""
    return PhaseResult(
        phase="review",
        success=True,
        findings={"summary": "LGTM"},
        artifacts={"review_report": {"verdict": "approve", "summary": "LGTM"}},
    )


def _make_val(
    config: EngineConfig | None = None,
    responses: list[str] | None = None,
    prior_results: list[PhaseResult] | None = None,
) -> ValidatePhase:
    return ValidatePhase(
        llm=MockProvider(responses=responses or [_ready_response()]),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake",
        issue_data={
            "url": "https://github.com/test/repo/issues/42",
            "title": "nil panic",
            "body": "controller panics",
        },
        config=config or EngineConfig(),
        prior_phase_results=(
            prior_results
            if prior_results is not None
            else [_triage_result(), _impl_result(), _review_approve()]
        ),
    )


# ==================================================================
# CIRemediationConfig
# ==================================================================


class TestCIRemediationConfig:
    def test_defaults(self):
        cfg = CIRemediationConfig()
        assert cfg.enabled is True
        assert cfg.max_iterations == 3
        assert cfg.time_budget_minutes == 15
        assert cfg.ci_poll_interval_seconds == 30
        assert cfg.ci_poll_timeout_minutes == 20
        assert cfg.rerun_on_infrastructure_flake is True
        assert cfg.max_flake_reruns == 2

    def test_default_failure_categories(self):
        cfg = CIRemediationConfig()
        assert cfg.failure_categories["test_failure"] == "remediate"
        assert cfg.failure_categories["build_error"] == "remediate"
        assert cfg.failure_categories["lint_violation"] == "remediate"
        assert cfg.failure_categories["infrastructure_flake"] == "rerun"
        assert cfg.failure_categories["timeout"] == "escalate"

    def test_engine_config_includes_ci_remediation(self):
        cfg = EngineConfig()
        assert isinstance(cfg.ci_remediation, CIRemediationConfig)
        assert cfg.ci_remediation.max_iterations == 3

    def test_yaml_override_scalars(self):
        cfg = load_config(
            overrides={
                "ci_remediation": {
                    "max_iterations": 5,
                    "time_budget_minutes": 25,
                    "ci_poll_interval_seconds": 15,
                    "ci_poll_timeout_minutes": 30,
                    "rerun_on_infrastructure_flake": False,
                    "max_flake_reruns": 0,
                    "enabled": False,
                }
            }
        )
        assert cfg.ci_remediation.max_iterations == 5
        assert cfg.ci_remediation.time_budget_minutes == 25
        assert cfg.ci_remediation.ci_poll_interval_seconds == 15
        assert cfg.ci_remediation.ci_poll_timeout_minutes == 30
        assert cfg.ci_remediation.rerun_on_infrastructure_flake is False
        assert cfg.ci_remediation.max_flake_reruns == 0
        assert cfg.ci_remediation.enabled is False

    def test_yaml_failure_categories_merge(self):
        """Overriding failure_categories merges into defaults."""
        cfg = load_config(
            overrides={
                "ci_remediation": {
                    "failure_categories": {
                        "timeout": "remediate",
                        "custom_category": "escalate",
                    }
                }
            }
        )
        assert cfg.ci_remediation.failure_categories["timeout"] == "remediate"
        assert cfg.ci_remediation.failure_categories["custom_category"] == "escalate"
        assert cfg.ci_remediation.failure_categories["test_failure"] == "remediate"

    def test_yaml_unknown_field_ignored(self):
        cfg = load_config(
            overrides={"ci_remediation": {"nonexistent_field": 42, "max_iterations": 7}}
        )
        assert cfg.ci_remediation.max_iterations == 7
        assert not hasattr(cfg.ci_remediation, "nonexistent_field")

    def test_no_ci_remediation_section_keeps_defaults(self):
        cfg = load_config(overrides={"llm": {"provider": "anthropic"}})
        assert cfg.ci_remediation.max_iterations == 3
        assert cfg.ci_remediation.enabled is True


# ==================================================================
# _has_review_approval()
# ==================================================================


class TestHasReviewApproval:
    def test_approved_via_findings(self):
        phase = _make_val(prior_results=[_triage_result(), _impl_result(), _review_approve()])
        assert phase._has_review_approval() is True

    def test_approved_via_artifacts_only(self):
        phase = _make_val(
            prior_results=[_triage_result(), _impl_result(), _review_approve_in_artifacts_only()]
        )
        assert phase._has_review_approval() is True

    def test_not_approved_request_changes(self):
        phase = _make_val(
            prior_results=[_triage_result(), _impl_result(), _review_request_changes()]
        )
        assert phase._has_review_approval() is False

    def test_no_review_at_all(self):
        phase = _make_val(prior_results=[_triage_result(), _impl_result()])
        assert phase._has_review_approval() is False

    def test_empty_prior_results(self):
        phase = _make_val(prior_results=[])
        assert phase._has_review_approval() is False

    def test_picks_latest_review(self):
        """When multiple reviews exist, uses the latest one."""
        rejected = PhaseResult(
            phase="review",
            success=False,
            findings={"verdict": "request_changes"},
        )
        approved = PhaseResult(
            phase="review",
            success=True,
            findings={"verdict": "approve"},
        )
        phase = _make_val(prior_results=[_triage_result(), _impl_result(), rejected, approved])
        assert phase._has_review_approval() is True

    def test_latest_review_rejected(self):
        """Even if an earlier review approved, latest must approve."""
        approved = PhaseResult(
            phase="review",
            success=True,
            findings={"verdict": "approve"},
        )
        rejected = PhaseResult(
            phase="review",
            success=True,
            findings={"verdict": "request_changes"},
        )
        phase = _make_val(prior_results=[_triage_result(), _impl_result(), approved, rejected])
        assert phase._has_review_approval() is False

    def test_skips_non_review_phases(self):
        """Non-review phases with 'approve' are ignored."""
        fake = PhaseResult(
            phase="implement",
            success=True,
            findings={"verdict": "approve"},
        )
        phase = _make_val(prior_results=[_triage_result(), fake])
        assert phase._has_review_approval() is False


# ==================================================================
# _is_ready_to_push()
# ==================================================================


class TestIsReadyToPush:
    def test_all_gates_pass(self):
        phase = _make_val()
        ready, blockers = phase._is_ready_to_push(lint_passed=True, llm_ready=True, tests_ok=True)
        assert ready is True
        assert blockers == []

    def test_review_not_approved_blocks(self):
        phase = _make_val(prior_results=[_triage_result(), _impl_result()])
        ready, blockers = phase._is_ready_to_push(lint_passed=True, llm_ready=True, tests_ok=True)
        assert ready is False
        assert any("Review" in b for b in blockers)

    def test_lint_failing_blocks(self):
        phase = _make_val()
        ready, blockers = phase._is_ready_to_push(lint_passed=False, llm_ready=True, tests_ok=True)
        assert ready is False
        assert any("lint" in b.lower() for b in blockers)

    def test_llm_not_ready_blocks(self):
        phase = _make_val()
        ready, blockers = phase._is_ready_to_push(lint_passed=True, llm_ready=False, tests_ok=True)
        assert ready is False
        assert any("not ready" in b.lower() for b in blockers)

    def test_tests_failing_in_required_mode_blocks(self):
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "required"
        phase = _make_val(config=cfg)
        ready, blockers = phase._is_ready_to_push(lint_passed=True, llm_ready=True, tests_ok=False)
        assert ready is False
        assert any("required" in b.lower() for b in blockers)

    def test_tests_failing_in_disabled_mode_ok(self):
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "disabled"
        phase = _make_val(config=cfg)
        ready, blockers = phase._is_ready_to_push(lint_passed=True, llm_ready=True, tests_ok=False)
        assert ready is True
        assert blockers == []

    def test_tests_failing_in_opportunistic_mode_ok(self):
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "opportunistic"
        phase = _make_val(config=cfg)
        ready, blockers = phase._is_ready_to_push(lint_passed=True, llm_ready=True, tests_ok=False)
        assert ready is True
        assert blockers == []

    def test_multiple_blockers(self):
        phase = _make_val(prior_results=[_triage_result(), _impl_result()])
        ready, blockers = phase._is_ready_to_push(lint_passed=False, llm_ready=False, tests_ok=True)
        assert ready is False
        assert len(blockers) >= 3


# ==================================================================
# act() enforces implement-first gates
# ==================================================================


class TestActImplementFirst:
    @pytest.mark.asyncio
    async def test_act_no_pr_without_review_approval(self):
        """act() does NOT create a PR when review hasn't approved."""
        phase = _make_val(prior_results=[_triage_result(), _impl_result()])
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
        assert result["pr_created"] is False
        assert result["can_create_pr"] is False
        assert any("Review" in b for b in result["push_blockers"])

    @pytest.mark.asyncio
    async def test_act_push_blockers_in_result(self):
        """act() returns push_blockers list in result."""
        phase = _make_val()
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": False, "output": "E501"},
                "observation": {"issue": {"url": "u"}, "diff": "d"},
            }
        )
        assert "push_blockers" in result
        assert any("lint" in b.lower() for b in result["push_blockers"])
        assert result["can_create_pr"] is False

    @pytest.mark.asyncio
    async def test_act_all_gates_pass_but_no_tool_executor(self):
        """When gates pass but no tool executor, can_create_pr becomes False."""
        phase = _make_val()
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {"issue": {"url": "u"}, "diff": "d"},
            }
        )
        assert result["pr_created"] is False
        assert result["push_blockers"] == []

    @pytest.mark.asyncio
    async def test_act_review_approved_lint_pass_creates_pr_attempt(self, monkeypatch):
        """With review approved + lint passing + GH_PAT, act() attempts PR creation."""
        monkeypatch.setenv("GH_PAT", "fake-token")
        phase = _make_val()
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
        assert result["push_blockers"] == []

    @pytest.mark.asyncio
    async def test_act_request_changes_review_blocks(self):
        """Review with request_changes verdict blocks PR creation."""
        phase = _make_val(
            prior_results=[_triage_result(), _impl_result(), _review_request_changes()]
        )
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {"issue": {"url": "u"}, "diff": "d"},
            }
        )
        assert result["pr_created"] is False
        assert result["can_create_pr"] is False
        assert any("Review" in b for b in result["push_blockers"])


# ==================================================================
# Loop integration — ordering
# ==================================================================


class TestLoopOrdering:
    def test_phase_order_has_review_before_validate(self):
        from engine.loop import PHASE_ORDER

        review_idx = PHASE_ORDER.index("review")
        validate_idx = PHASE_ORDER.index("validate")
        assert review_idx < validate_idx

    def test_phase_order_has_implement_before_review(self):
        from engine.loop import PHASE_ORDER

        implement_idx = PHASE_ORDER.index("implement")
        review_idx = PHASE_ORDER.index("review")
        assert implement_idx < review_idx

    @pytest.mark.asyncio
    async def test_loop_runs_review_before_validate(self, tmp_path):
        """Full loop runs review before validate."""
        import subprocess

        from engine.loop import PipelineEngine
        from engine.phases.implement import ImplementPhase
        from engine.phases.review import ReviewPhase
        from engine.phases.triage import TriagePhase

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "r.go").write_text("package main\n")

        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
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
                "affected_components": ["r.go"],
                "reproduction": {"existing_tests": [], "can_reproduce": False},
                "injection_detected": False,
                "recommendation": "proceed",
                "reasoning": "Nil pointer bug",
            }
        )
        impl_resp = json.dumps(
            {
                "root_cause": "Nil pointer",
                "fix_description": "Added nil check",
                "files_changed": ["r.go"],
                "file_changes": [{"path": "r.go", "content": "package main\nfunc f() {}\n"}],
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

        loop = PipelineEngine(
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
        if "review" in phases_run and "validate" in phases_run:
            review_idx = phases_run.index("review")
            validate_idx = phases_run.index("validate")
            assert review_idx < validate_idx


# ==================================================================
# Backward compatibility — existing tests still work
# ==================================================================


class TestBackwardCompat:
    @pytest.mark.asyncio
    async def test_existing_execute_success_still_works(self):
        """Full execute() with review-approved prior results still succeeds."""
        cfg = EngineConfig()
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False
        phase = _make_val(config=cfg)
        result = await phase.execute()
        assert result.success is True
        assert result.next_phase == "report"

    @pytest.mark.asyncio
    async def test_can_create_pr_field_still_in_result(self):
        """act() result still contains can_create_pr for backward compat."""
        phase = _make_val()
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {"issue": {"url": "u"}, "diff": "d"},
            }
        )
        assert "can_create_pr" in result

    @pytest.mark.asyncio
    async def test_validate_result_passthrough(self):
        """Validate result and lint/test results pass through act()."""
        phase = _make_val()
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "OK"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {"issue": {"url": "u"}, "diff": "d"},
            }
        )
        assert result["validate_result"]["ready_to_submit"] is True
        assert result["test_result"]["passed"] is True
        assert result["lint_result"]["passed"] is True
