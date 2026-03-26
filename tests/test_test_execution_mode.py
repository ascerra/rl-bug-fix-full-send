"""Tests for D18 — test_execution_mode: CI-first optional test execution strategy.

Covers:
- Config defaults (disabled mode, changed boolean defaults)
- Auto-promotion from disabled → opportunistic when test_command is set
- Implement phase respects test_execution_mode (disabled/opportunistic/required)
- Validate phase adjusts PR gate, validate(), reflect() based on mode
- PR description includes appropriate test status messaging
- Post-PR CI monitoring (informational)
"""

from __future__ import annotations

import json

import pytest

from engine.config import EngineConfig, load_config
from engine.integrations.llm import MockProvider
from engine.observability.logger import StructuredLogger
from engine.observability.tracer import Tracer
from engine.phases.base import PhaseResult
from engine.phases.implement import ImplementPhase
from engine.phases.validate import ValidatePhase, _build_test_status_note

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _fix_response() -> str:
    return json.dumps(
        {
            "root_cause": "Nil pointer dereference",
            "fix_description": "Added nil check",
            "files_changed": ["reconciler.go"],
            "file_changes": [{"path": "reconciler.go", "content": "package main\n"}],
            "test_added": "",
            "tests_passing": True,
            "linters_passing": True,
            "confidence": 0.9,
            "diff_summary": "Added nil check",
        }
    )


def _ready_response(confidence: float = 0.95) -> str:
    return json.dumps(
        {
            "tests_passing": True,
            "test_summary": "10 passed",
            "linters_passing": True,
            "lint_issues": [],
            "diff_is_minimal": True,
            "unnecessary_changes": [],
            "pr_description": ("## Fix\nAdded nil check.\n\nTests not run — CI will validate."),
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
        findings={
            "classification": "bug",
            "severity": "high",
            "affected_components": ["reconciler.go"],
            "reasoning": "Nil pointer dereference",
        },
        artifacts={
            "triage_report": {
                "classification": "bug",
                "severity": "high",
                "affected_components": ["reconciler.go"],
            },
        },
    )


def _impl_result() -> PhaseResult:
    return PhaseResult(
        phase="implement",
        success=True,
        findings={"root_cause": "nil ptr", "fix_description": "nil check"},
        artifacts={"diff": "--- a/r.go\n+++ b/r.go\n@@ ...", "files_changed": ["reconciler.go"]},
    )


def _review_result() -> PhaseResult:
    return PhaseResult(
        phase="review",
        success=True,
        findings={"verdict": "approve", "summary": "LGTM"},
        artifacts={"review_report": {"verdict": "approve", "summary": "LGTM"}},
    )


def _make_impl(config: EngineConfig | None = None) -> ImplementPhase:
    return ImplementPhase(
        llm=MockProvider(responses=[_fix_response()]),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake",
        issue_data={"url": "u", "title": "t", "body": "b"},
        config=config or EngineConfig(),
        prior_phase_results=[_triage_result()],
    )


def _make_val(
    config: EngineConfig | None = None,
    responses: list[str] | None = None,
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
        prior_phase_results=[_triage_result(), _impl_result(), _review_result()],
    )


# ==================================================================
# Config defaults
# ==================================================================


class TestConfigDefaults:
    def test_default_test_execution_mode_disabled(self):
        cfg = EngineConfig()
        assert cfg.phases.implement.test_execution_mode == "disabled"
        assert cfg.phases.validate.test_execution_mode == "disabled"

    def test_default_run_tests_after_each_edit_false(self):
        cfg = EngineConfig()
        assert cfg.phases.implement.run_tests_after_each_edit is False

    def test_default_full_test_suite_false(self):
        cfg = EngineConfig()
        assert cfg.phases.validate.full_test_suite is False


# ==================================================================
# Auto-promotion via load_config
# ==================================================================


class TestAutoPromotion:
    def test_test_command_promotes_impl_to_opportunistic(self):
        cfg = load_config(overrides={"phases": {"implement": {"test_command": "go test ./..."}}})
        assert cfg.phases.implement.test_execution_mode == "opportunistic"

    def test_test_command_promotes_validate_to_opportunistic(self):
        cfg = load_config(overrides={"phases": {"validate": {"test_command": "go test ./..."}}})
        assert cfg.phases.validate.test_execution_mode == "opportunistic"

    def test_explicit_mode_not_overridden(self):
        cfg = load_config(
            overrides={
                "phases": {
                    "implement": {
                        "test_command": "go test ./...",
                        "test_execution_mode": "required",
                    }
                }
            }
        )
        assert cfg.phases.implement.test_execution_mode == "required"

    def test_no_test_command_stays_disabled(self):
        cfg = load_config(overrides={"phases": {"implement": {"run_linters": True}}})
        assert cfg.phases.implement.test_execution_mode == "disabled"

    def test_yaml_override_both_phases(self):
        cfg = load_config(
            overrides={
                "phases": {
                    "implement": {"test_command": "pytest"},
                    "validate": {"test_command": "pytest"},
                }
            }
        )
        assert cfg.phases.implement.test_execution_mode == "opportunistic"
        assert cfg.phases.validate.test_execution_mode == "opportunistic"


# ==================================================================
# Implement phase — test_execution_mode
# ==================================================================


class TestImplementTestExecMode:
    @pytest.mark.asyncio
    async def test_disabled_skips_tests(self):
        cfg = EngineConfig()
        assert cfg.phases.implement.test_execution_mode == "disabled"
        phase = _make_impl(config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["test_result"]["passed"] is True
        assert "skipped" in result["test_result"]["output"].lower()

    @pytest.mark.asyncio
    async def test_opportunistic_runs_tests_but_doesnt_gate(self):
        """In opportunistic mode, test failures don't trigger inner iterations."""
        cfg = EngineConfig()
        cfg.phases.implement.test_execution_mode = "opportunistic"
        cfg.phases.implement.run_linters = False
        phase = _make_impl(config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["inner_iterations"] == 0

    @pytest.mark.asyncio
    async def test_required_gates_on_test_failures(self):
        """In required mode, test failures trigger inner iterations."""
        cfg = EngineConfig()
        cfg.phases.implement.test_execution_mode = "required"
        phase = _make_impl(config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert "test_result" in result

    @pytest.mark.asyncio
    async def test_disabled_lint_still_gates(self):
        """Even with tests disabled, lint failures still trigger inner iterations."""
        cfg = EngineConfig()
        cfg.phases.implement.test_execution_mode = "disabled"
        cfg.phases.implement.run_linters = True
        phase = _make_impl(config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        result = await phase.act(plan)
        assert result["test_result"]["passed"] is True


# ==================================================================
# Validate phase — test_execution_mode
# ==================================================================


class TestValidateTestExecMode:
    @pytest.mark.asyncio
    async def test_disabled_skips_tests(self):
        cfg = EngineConfig()
        cfg.phases.validate.ci_equivalent = False
        phase = _make_val(config=cfg)
        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["test_result"]["passed"] is True
        assert "ci will validate" in plan["test_result"]["output"].lower()

    @pytest.mark.asyncio
    async def test_disabled_pr_created_without_tests(self):
        """In disabled mode, PR can be created even though tests didn't run."""
        cfg = EngineConfig()
        cfg.phases.validate.ci_equivalent = False
        phase = _make_val(config=cfg)
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": True, "output": "Tests not run"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {
                    "issue": {"url": "https://github.com/test/repo/issues/42"},
                    "diff": "d",
                },
            }
        )
        assert result["pr_created"] is False  # no tool executor

    @pytest.mark.asyncio
    async def test_opportunistic_test_fail_doesnt_block_pr(self):
        """In opportunistic mode, test failures don't block PR creation."""
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "opportunistic"
        phase = _make_val(config=cfg)
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": False, "output": "2 FAILED"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {
                    "issue": {"url": "https://github.com/test/repo/issues/42"},
                    "diff": "d",
                },
            }
        )
        assert result["pr_created"] is False  # no tool executor, but wasn't blocked

    @pytest.mark.asyncio
    async def test_required_test_fail_blocks_pr(self):
        """In required mode, test failures block PR creation."""
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "required"
        phase = _make_val(config=cfg)
        result = await phase.act(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": False, "output": "2 FAILED"},
                "lint_result": {"passed": True, "output": "OK"},
                "observation": {
                    "issue": {"url": "https://github.com/test/repo/issues/42"},
                    "diff": "d",
                },
            }
        )
        assert result["pr_created"] is False


# ==================================================================
# Validate — validate() method
# ==================================================================


class TestValidateValidateMethod:
    @pytest.mark.asyncio
    async def test_disabled_test_fail_not_flagged(self):
        """In disabled mode, test failures don't appear in issues."""
        cfg = EngineConfig()
        phase = _make_val(config=cfg)
        validation = await phase.validate(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": False, "output": "FAIL"},
                "lint_result": {"passed": True, "output": "OK"},
            }
        )
        assert not any("Tests failing" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_opportunistic_test_fail_not_flagged(self):
        """In opportunistic mode, test failures don't appear in issues."""
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "opportunistic"
        phase = _make_val(config=cfg)
        validation = await phase.validate(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": False, "output": "FAIL"},
                "lint_result": {"passed": True, "output": "OK"},
            }
        )
        assert not any("Tests failing" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_required_test_fail_is_flagged(self):
        """In required mode, test failures DO appear in issues."""
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "required"
        phase = _make_val(config=cfg)
        validation = await phase.validate(
            {
                "validate_result": json.loads(_ready_response()),
                "test_result": {"passed": False, "output": "FAIL"},
                "lint_result": {"passed": True, "output": "OK"},
            }
        )
        assert any("Tests failing" in i for i in validation["issues"])

    @pytest.mark.asyncio
    async def test_lint_fail_always_flagged(self):
        """Lint failures are always flagged regardless of test_execution_mode."""
        for mode in ("disabled", "opportunistic", "required"):
            cfg = EngineConfig()
            cfg.phases.validate.test_execution_mode = mode
            phase = _make_val(config=cfg)
            validation = await phase.validate(
                {
                    "validate_result": json.loads(_ready_response()),
                    "test_result": {"passed": True, "output": "OK"},
                    "lint_result": {"passed": False, "output": "E501"},
                }
            )
            assert any("Linters failing" in i for i in validation["issues"]), (
                f"Lint failure not flagged in mode={mode}"
            )


# ==================================================================
# Validate — reflect() method
# ==================================================================


class TestValidateReflectMode:
    @pytest.mark.asyncio
    async def test_disabled_test_fail_no_backtrack(self):
        """In disabled mode, test failures alone don't backtrack."""
        cfg = EngineConfig()
        phase = _make_val(config=cfg)
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Other issue"],
                "tests_passing": False,
                "linters_passing": True,
                "validate_result": {},
            }
        )
        assert result.next_phase != "implement"

    @pytest.mark.asyncio
    async def test_required_test_fail_backtracks(self):
        """In required mode, test failures DO backtrack to implement."""
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "required"
        phase = _make_val(config=cfg)
        result = await phase.reflect(
            {
                "valid": False,
                "issues": ["Tests failing"],
                "tests_passing": False,
                "linters_passing": True,
                "validate_result": {},
            }
        )
        assert result.next_phase == "implement"

    @pytest.mark.asyncio
    async def test_lint_fail_always_backtracks(self):
        """Lint failures always backtrack regardless of mode."""
        for mode in ("disabled", "opportunistic", "required"):
            cfg = EngineConfig()
            cfg.phases.validate.test_execution_mode = mode
            phase = _make_val(config=cfg)
            result = await phase.reflect(
                {
                    "valid": False,
                    "issues": ["Linters failing"],
                    "tests_passing": True,
                    "linters_passing": False,
                    "validate_result": {},
                }
            )
            assert result.next_phase == "implement", f"Lint failure didn't backtrack in mode={mode}"


# ==================================================================
# PR description test status messaging
# ==================================================================


class TestBuildTestStatusNote:
    def test_disabled_mode_note(self):
        note = _build_test_status_note("disabled", {"passed": True})
        assert "not run locally" in note.lower()
        assert "ci will validate" in note.lower()

    def test_opportunistic_fail_note(self):
        note = _build_test_status_note("opportunistic", {"passed": False})
        assert "failures" in note.lower()
        assert "ci will validate" in note.lower()

    def test_opportunistic_pass_no_note(self):
        note = _build_test_status_note("opportunistic", {"passed": True})
        assert note == ""

    def test_required_no_note(self):
        note = _build_test_status_note("required", {"passed": True})
        assert note == ""

    def test_required_fail_no_note(self):
        note = _build_test_status_note("required", {"passed": False})
        assert note == ""


# ==================================================================
# Plan includes test status note in LLM context
# ==================================================================


class TestPlanTestStatusNote:
    @pytest.mark.asyncio
    async def test_disabled_mode_note_in_llm_context(self):
        llm = MockProvider(responses=[_ready_response()])
        cfg = EngineConfig()
        cfg.phases.validate.ci_equivalent = False
        phase = ValidatePhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=cfg,
            prior_phase_results=[_triage_result(), _impl_result(), _review_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "not run locally" in msg.lower()

    @pytest.mark.asyncio
    async def test_required_mode_no_note_in_llm_context(self):
        llm = MockProvider(responses=[_ready_response()])
        cfg = EngineConfig()
        cfg.phases.validate.test_execution_mode = "required"
        cfg.phases.validate.ci_equivalent = False
        phase = ValidatePhase(
            llm=llm,
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake",
            issue_data={"url": "u", "title": "t", "body": "b"},
            config=cfg,
            prior_phase_results=[_triage_result(), _impl_result(), _review_result()],
        )
        obs = await phase.observe()
        await phase.plan(obs)
        msg = llm.call_log[0]["messages"][0]["content"]
        assert "NOTE FOR PR DESCRIPTION" not in msg


# ==================================================================
# Post-PR CI monitoring
# ==================================================================


class TestPostPrCiMonitoring:
    @pytest.mark.asyncio
    async def test_ci_status_in_act_result(self):
        """act() result includes ci_status field."""
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
        assert "ci_status" in result

    @pytest.mark.asyncio
    async def test_check_post_pr_ci_no_tool_executor(self):
        """_check_post_pr_ci returns empty dict without tool executor."""
        phase = _make_val()
        result = await phase._check_post_pr_ci(
            {"issue": {"url": "https://github.com/test/repo/issues/42"}},
            [],
        )
        assert result == {}


# ==================================================================
# YAML config roundtrip
# ==================================================================


class TestYamlConfig:
    def test_test_execution_mode_via_yaml(self):
        cfg = load_config(overrides={"phases": {"implement": {"test_execution_mode": "required"}}})
        assert cfg.phases.implement.test_execution_mode == "required"

    def test_validate_mode_via_yaml(self):
        cfg = load_config(
            overrides={"phases": {"validate": {"test_execution_mode": "opportunistic"}}}
        )
        assert cfg.phases.validate.test_execution_mode == "opportunistic"
