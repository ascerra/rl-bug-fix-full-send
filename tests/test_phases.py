"""Tests for the Phase Framework — prompt loading, tool sets, config wiring, base class."""

from __future__ import annotations

from typing import Any, ClassVar

import jinja2
import pytest

from engine.config import (
    EngineConfig,
    PhasesConfig,
    TriagePhaseConfig,
    load_config,
)
from engine.integrations.llm import MockProvider
from engine.observability.logger import StructuredLogger
from engine.observability.tracer import Tracer
from engine.phases.base import (
    IMPLEMENT_TOOLS,
    PHASE_TOOL_SETS,
    REPORT_TOOLS,
    REVIEW_TOOLS,
    TRIAGE_TOOLS,
    VALIDATE_TOOLS,
    Phase,
    PhaseResult,
)
from engine.phases.prompt_loader import available_prompts, load_prompt

# ------------------------------------------------------------------
# Prompt loading tests
# ------------------------------------------------------------------


class TestPromptLoader:
    def test_load_triage_prompt(self):
        prompt = load_prompt("triage")
        assert "bug triage agent" in prompt.lower()
        assert "UNTRUSTED" in prompt

    def test_load_implement_prompt(self):
        prompt = load_prompt("implement")
        assert "bug fix implementation" in prompt.lower() or "implementation" in prompt.lower()
        assert "UNTRUSTED" in prompt

    def test_load_review_prompt(self):
        prompt = load_prompt("review")
        assert "review" in prompt.lower()
        assert "UNTRUSTED" in prompt

    def test_load_validate_prompt(self):
        prompt = load_prompt("validate")
        assert "validation" in prompt.lower()
        assert "UNTRUSTED" in prompt

    def test_load_report_prompt(self):
        prompt = load_prompt("report")
        assert "report" in prompt.lower()

    def test_load_nonexistent_prompt_raises(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            load_prompt("nonexistent_phase_xyz")

    def test_load_prompt_with_variables(self, tmp_path):
        tmpl = tmp_path / "test_phase.md"
        tmpl.write_text("Hello {{ name }}, phase={{ phase_name }}!")
        result = load_prompt(
            "test_phase",
            variables={"name": "Ralph", "phase_name": "triage"},
            templates_dir=tmp_path,
        )
        assert result == "Hello Ralph, phase=triage!"

    def test_load_prompt_missing_variable_raises(self, tmp_path):
        tmpl = tmp_path / "needs_var.md"
        tmpl.write_text("Hello {{ required_var }}!")
        with pytest.raises(jinja2.UndefinedError):
            load_prompt("needs_var", variables={}, templates_dir=tmp_path)

    def test_load_prompt_no_variables_returns_raw(self, tmp_path):
        tmpl = tmp_path / "raw.md"
        tmpl.write_text("No variables here, just raw text.")
        result = load_prompt("raw", templates_dir=tmp_path)
        assert result == "No variables here, just raw text."

    def test_available_prompts_lists_all(self):
        prompts = available_prompts()
        assert "triage" in prompts
        assert "implement" in prompts
        assert "review" in prompts
        assert "validate" in prompts
        assert "report" in prompts

    def test_available_prompts_custom_dir(self, tmp_path):
        (tmp_path / "alpha.md").write_text("a")
        (tmp_path / "beta.md").write_text("b")
        (tmp_path / "not_md.txt").write_text("c")
        result = available_prompts(templates_dir=tmp_path)
        assert result == ["alpha", "beta"]

    def test_available_prompts_empty_dir(self, tmp_path):
        result = available_prompts(templates_dir=tmp_path)
        assert result == []

    def test_available_prompts_nonexistent_dir(self, tmp_path):
        result = available_prompts(templates_dir=tmp_path / "nope")
        assert result == []


# ------------------------------------------------------------------
# Tool set definition tests
# ------------------------------------------------------------------


class TestToolSets:
    def test_triage_tools_read_only(self):
        assert "file_write" not in TRIAGE_TOOLS
        assert "git_commit" not in TRIAGE_TOOLS
        assert "github_api" not in TRIAGE_TOOLS
        assert "file_read" in TRIAGE_TOOLS
        assert "file_search" in TRIAGE_TOOLS
        assert "shell_run" in TRIAGE_TOOLS

    def test_implement_tools_include_write(self):
        assert "file_read" in IMPLEMENT_TOOLS
        assert "file_write" in IMPLEMENT_TOOLS
        assert "shell_run" in IMPLEMENT_TOOLS
        assert "git_diff" in IMPLEMENT_TOOLS
        assert "git_commit" in IMPLEMENT_TOOLS
        assert "github_api" not in IMPLEMENT_TOOLS

    def test_review_tools_read_only(self):
        assert "file_read" in REVIEW_TOOLS
        assert "file_search" in REVIEW_TOOLS
        assert "git_diff" in REVIEW_TOOLS
        assert "file_write" not in REVIEW_TOOLS
        assert "shell_run" not in REVIEW_TOOLS
        assert "git_commit" not in REVIEW_TOOLS

    def test_validate_tools_include_github_api(self):
        assert "file_read" in VALIDATE_TOOLS
        assert "shell_run" in VALIDATE_TOOLS
        assert "github_api" in VALIDATE_TOOLS
        assert "git_diff" in VALIDATE_TOOLS
        assert "file_write" not in VALIDATE_TOOLS
        assert "git_commit" not in VALIDATE_TOOLS

    def test_report_tools_minimal(self):
        assert "file_read" in REPORT_TOOLS
        assert "file_search" in REPORT_TOOLS
        assert len(REPORT_TOOLS) == 2

    def test_phase_tool_sets_has_all_phases(self):
        expected = {"triage", "implement", "review", "validate", "report", "ci_remediate"}
        assert set(PHASE_TOOL_SETS.keys()) == expected

    def test_no_tool_set_has_unknown_tools(self):
        known_tools = {
            "file_read",
            "file_write",
            "file_search",
            "shell_run",
            "git_diff",
            "git_commit",
            "github_api",
        }
        for phase_name, tools in PHASE_TOOL_SETS.items():
            for tool in tools:
                assert tool in known_tools, f"Unknown tool '{tool}' in phase '{phase_name}'"


# ------------------------------------------------------------------
# Phase base class tests
# ------------------------------------------------------------------


def _make_phase(
    name: str = "triage",
    allowed_tools: list[str] | None = None,
    config: EngineConfig | None = None,
) -> type[Phase]:
    """Create a concrete Phase subclass for testing."""
    tools = allowed_tools if allowed_tools is not None else []

    class _TestPhase(Phase):
        async def observe(self) -> dict[str, Any]:
            return {}

        async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
            return PhaseResult(phase=name, success=True)

    _TestPhase.name = name
    _TestPhase.allowed_tools = tools
    return _TestPhase


def _instantiate_phase(
    phase_cls: type[Phase],
    config: EngineConfig | None = None,
) -> Phase:
    return phase_cls(
        llm=MockProvider(),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake-repo",
        issue_data={"url": "https://github.com/test/repo/issues/1"},
        config=config,
    )


class TestPhaseBase:
    def test_get_allowed_tools_from_classvar(self):
        cls = _make_phase("triage", allowed_tools=["file_read", "shell_run"])
        assert cls.get_allowed_tools() == ["file_read", "shell_run"]

    def test_get_allowed_tools_falls_back_to_phase_tool_sets(self):
        cls = _make_phase("triage", allowed_tools=[])
        assert cls.get_allowed_tools() == TRIAGE_TOOLS

    def test_get_allowed_tools_unknown_phase_returns_empty(self):
        cls = _make_phase("unknown_phase_xyz", allowed_tools=[])
        assert cls.get_allowed_tools() == []

    def test_get_allowed_tools_implement_fallback(self):
        cls = _make_phase("implement", allowed_tools=[])
        assert cls.get_allowed_tools() == IMPLEMENT_TOOLS

    def test_get_allowed_tools_review_fallback(self):
        cls = _make_phase("review", allowed_tools=[])
        assert cls.get_allowed_tools() == REVIEW_TOOLS

    def test_config_defaults_when_none(self):
        cls = _make_phase("triage")
        phase = _instantiate_phase(cls, config=None)
        assert phase.config is not None
        assert phase.config.phases.triage.enabled is True

    def test_config_passed_through(self):
        cfg = EngineConfig(phases=PhasesConfig(triage=TriagePhaseConfig(enabled=False)))
        cls = _make_phase("triage")
        phase = _instantiate_phase(cls, config=cfg)
        assert phase.config.phases.triage.enabled is False

    def test_load_system_prompt(self):
        cls = _make_phase("triage")
        phase = _instantiate_phase(cls)
        prompt = phase.load_system_prompt()
        assert "triage" in prompt.lower()

    def test_load_system_prompt_with_variables(self, tmp_path):
        tmpl = tmp_path / "test_phase.md"
        tmpl.write_text("Phase {{ phase_name }} for {{ repo }}.")
        cls = _make_phase("test_phase")
        phase = _instantiate_phase(cls)
        prompt = phase.load_system_prompt(
            variables={"phase_name": "test_phase", "repo": "my-repo"},
            templates_dir=tmp_path,
        )
        assert prompt == "Phase test_phase for my-repo."

    def test_wrap_untrusted_content_default_delimiter(self):
        cls = _make_phase("triage")
        phase = _instantiate_phase(cls)
        wrapped = phase._wrap_untrusted_content("some user input")
        assert "--- UNTRUSTED CONTENT BELOW ---" in wrapped
        assert "some user input" in wrapped
        assert "--- END UNTRUSTED CONTENT ---" in wrapped

    def test_wrap_untrusted_content_custom_delimiter(self):
        cfg = EngineConfig()
        cfg.security.untrusted_content_delimiter = "=== DANGER ZONE ==="
        cls = _make_phase("triage")
        phase = _instantiate_phase(cls, config=cfg)
        wrapped = phase._wrap_untrusted_content("malicious input")
        assert "=== DANGER ZONE ===" in wrapped
        assert "malicious input" in wrapped

    def test_build_system_prompt_with_context(self):
        cls = _make_phase("triage")
        phase = _instantiate_phase(cls)
        result = phase._build_system_prompt("You are an agent.", "Repo: my-repo")
        assert result == "You are an agent.\n\nRepo: my-repo"

    def test_build_system_prompt_without_context(self):
        cls = _make_phase("triage")
        phase = _instantiate_phase(cls)
        result = phase._build_system_prompt("You are an agent.")
        assert result == "You are an agent."

    def test_prior_results_stored(self):
        prior = [PhaseResult(phase="triage", success=True)]
        cls = _make_phase("implement")
        phase = cls(
            llm=MockProvider(),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake-repo",
            issue_data={"url": "https://example.com"},
            prior_phase_results=prior,
        )
        assert len(phase.prior_results) == 1
        assert phase.prior_results[0].phase == "triage"


# ------------------------------------------------------------------
# Phase config tests
# ------------------------------------------------------------------


class TestPhasesConfig:
    def test_default_phases_config(self):
        cfg = EngineConfig()
        assert cfg.phases.triage.enabled is True
        assert cfg.phases.implement.max_inner_iterations == 5
        assert cfg.phases.review.correctness is True
        assert cfg.phases.validate.full_test_suite is False
        assert cfg.phases.report.enabled is True

    def test_phases_config_from_yaml(self, tmp_path):
        yaml_content = """
phases:
  triage:
    enabled: false
    classify_bug_vs_feature: false
  implement:
    max_inner_iterations: 3
    run_linters: false
  review:
    security: false
  validate:
    minimal_diff_check: false
"""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(yaml_content)
        cfg = load_config(config_path=str(config_file))
        assert cfg.phases.triage.enabled is False
        assert cfg.phases.triage.classify_bug_vs_feature is False
        assert cfg.phases.triage.attempt_reproduction is True  # unchanged
        assert cfg.phases.implement.max_inner_iterations == 3
        assert cfg.phases.implement.run_linters is False
        assert cfg.phases.review.security is False
        assert cfg.phases.review.correctness is True  # unchanged
        assert cfg.phases.validate.minimal_diff_check is False

    def test_phases_config_override(self):
        cfg = load_config(
            overrides={
                "phases": {
                    "implement": {"max_inner_iterations": 10},
                },
            }
        )
        assert cfg.phases.implement.max_inner_iterations == 10
        assert cfg.phases.triage.enabled is True  # unchanged

    def test_unknown_phase_key_ignored(self):
        cfg = load_config(
            overrides={
                "phases": {
                    "nonexistent": {"foo": "bar"},
                },
            }
        )
        assert cfg.phases.triage.enabled is True

    def test_unknown_field_in_phase_ignored(self):
        cfg = load_config(
            overrides={
                "phases": {
                    "triage": {"nonexistent_field": True},
                },
            }
        )
        assert cfg.phases.triage.enabled is True
        assert not hasattr(cfg.phases.triage, "nonexistent_field")


# ------------------------------------------------------------------
# Phase execute lifecycle tests
# ------------------------------------------------------------------


class _FullLifecyclePhase(Phase):
    """Concrete phase that tracks lifecycle calls."""

    name = "triage"
    allowed_tools: ClassVar[list[str]] = []
    call_order: ClassVar[list[str]] = []

    async def observe(self) -> dict[str, Any]:
        _FullLifecyclePhase.call_order.append("observe")
        return {"observed": True}

    async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
        _FullLifecyclePhase.call_order.append("plan")
        return {"planned": True, "observation": observation}

    async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
        _FullLifecyclePhase.call_order.append("act")
        return {"acted": True, "plan": plan}

    async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
        _FullLifecyclePhase.call_order.append("validate")
        return {"valid": True}

    async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
        _FullLifecyclePhase.call_order.append("reflect")
        return PhaseResult(phase="triage", success=True, should_continue=True)


@pytest.mark.asyncio
async def test_phase_execute_calls_lifecycle_in_order():
    _FullLifecyclePhase.call_order = []
    phase = _FullLifecyclePhase(
        llm=MockProvider(),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake",
        issue_data={"url": "https://example.com"},
    )
    result = await phase.execute()
    assert result.success is True
    assert _FullLifecyclePhase.call_order == ["observe", "plan", "act", "validate", "reflect"]


@pytest.mark.asyncio
async def test_phase_execute_handles_exception():
    class _Boom(Phase):
        name = "triage"
        allowed_tools: ClassVar[list[str]] = []

        async def observe(self) -> dict[str, Any]:
            raise ValueError("Kaboom")

        async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
            return PhaseResult(phase="triage", success=True)

    phase = _Boom(
        llm=MockProvider(),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake",
        issue_data={"url": "https://example.com"},
    )
    result = await phase.execute()
    assert result.success is False
    assert result.escalate is True
    assert "Kaboom" in result.escalation_reason
