"""Tests for D17 — cross-phase stack handoff.

Verifies:
- Triage serializes detected_stack into PhaseResult.artifacts
- Implement inherits triage stack via _extract_triage_stack()
- Validate inherits triage stack via _extract_triage_stack()
- Fallback to independent detection when no triage result is available
- Config overrides are applied on top of inherited stack
- Implement head limit increased to 200
- Validate head limit increased to 200
"""

from __future__ import annotations

import json

import pytest

from engine.config import EngineConfig
from engine.integrations.llm import MockProvider
from engine.observability.logger import StructuredLogger
from engine.observability.tracer import Tracer
from engine.phases.base import PhaseResult
from engine.phases.implement import ImplementPhase
from engine.phases.triage import TriagePhase
from engine.phases.validate import ValidatePhase
from engine.tools.test_runner import RepoStack

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _bug_response(components: list[str] | None = None) -> str:
    return json.dumps(
        {
            "classification": "bug",
            "confidence": 0.9,
            "severity": "high",
            "affected_components": components or ["pkg/controller/reconciler.go"],
            "reproduction": {
                "existing_tests": [],
                "can_reproduce": False,
                "reproduction_steps": "",
            },
            "injection_detected": False,
            "recommendation": "proceed",
            "reasoning": "The issue describes a nil pointer dereference — clearly a bug.",
        }
    )


def _fix_response() -> str:
    return json.dumps(
        {
            "root_cause": "Nil pointer dereference when owner ref is nil",
            "fix_description": "Added nil check before accessing owner reference",
            "files_changed": ["pkg/controller/reconciler.go"],
            "file_changes": [
                {
                    "path": "pkg/controller/reconciler.go",
                    "content": "package controller\n\nfunc Reconcile() error {\n"
                    "\tif owner == nil {\n\t\treturn nil\n\t}\n\treturn nil\n}\n",
                }
            ],
            "test_added": "",
            "tests_passing": True,
            "linters_passing": True,
            "confidence": 0.9,
            "diff_summary": "Added nil check",
        }
    )


def _ready_response() -> str:
    return json.dumps(
        {
            "tests_passing": True,
            "test_summary": "10 passed",
            "linters_passing": True,
            "lint_issues": [],
            "diff_is_minimal": True,
            "unnecessary_changes": [],
            "pr_description": "## Fix\nAdded nil check.",
            "ready_to_submit": True,
            "blocking_issues": [],
            "confidence": 0.95,
        }
    )


def _triage_result_with_stack(
    language: str = "go",
    detected_from: str = "go.mod",
    confidence: float = 0.95,
) -> PhaseResult:
    stack = RepoStack(
        language=language,
        test_command="go test ./... 2>&1",
        lint_command="golangci-lint run ./... 2>&1",
        detected_from=detected_from,
        confidence=confidence,
    )
    return PhaseResult(
        phase="triage",
        success=True,
        should_continue=True,
        next_phase="implement",
        findings={
            "classification": "bug",
            "severity": "high",
            "affected_components": ["pkg/controller/reconciler.go"],
            "reasoning": "Nil pointer dereference",
        },
        artifacts={
            "triage_report": {
                "classification": "bug",
                "severity": "high",
                "affected_components": ["pkg/controller/reconciler.go"],
            },
            "detected_stack": stack.to_dict(),
            "verified_components": [{"path": "pkg/controller/reconciler.go", "found": True}],
        },
    )


def _triage_result_without_stack() -> PhaseResult:
    return PhaseResult(
        phase="triage",
        success=True,
        should_continue=True,
        next_phase="implement",
        findings={
            "classification": "bug",
            "severity": "high",
            "affected_components": ["pkg/controller/reconciler.go"],
            "reasoning": "Nil pointer dereference",
        },
        artifacts={
            "triage_report": {
                "classification": "bug",
                "severity": "high",
                "affected_components": ["pkg/controller/reconciler.go"],
            },
            "verified_components": [{"path": "pkg/controller/reconciler.go", "found": True}],
        },
    )


def _make_implement(
    prior_results: list[PhaseResult] | None = None,
    config: EngineConfig | None = None,
) -> ImplementPhase:
    return ImplementPhase(
        llm=MockProvider(responses=[_fix_response()]),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake-repo",
        issue_data={
            "url": "https://github.com/test/repo/issues/42",
            "title": "nil pointer panic in reconciler",
            "body": "When reconciling a resource with no owner, the controller panics.",
        },
        config=config or EngineConfig(),
        prior_phase_results=prior_results,
    )


def _make_validate(
    prior_results: list[PhaseResult] | None = None,
    config: EngineConfig | None = None,
) -> ValidatePhase:
    return ValidatePhase(
        llm=MockProvider(responses=[_ready_response()]),
        logger=StructuredLogger(),
        tracer=Tracer(),
        repo_path="/tmp/fake-repo",
        issue_data={
            "url": "https://github.com/test/repo/issues/42",
            "title": "nil pointer panic in reconciler",
            "body": "When reconciling a resource with no owner, the controller panics.",
        },
        config=config or EngineConfig(),
        prior_phase_results=prior_results,
    )


# ------------------------------------------------------------------
# 1. Triage serializes detected_stack into artifacts
# ------------------------------------------------------------------


class TestTriageSerializesStack:
    """Verify triage puts detected_stack into PhaseResult.artifacts."""

    @pytest.mark.asyncio
    async def test_bug_reflect_includes_detected_stack(self):
        phase = TriagePhase(
            llm=MockProvider(responses=[_bug_response()]),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake-repo",
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic",
                "body": "Panic in reconciler.",
            },
            config=EngineConfig(),
        )
        phase._detected_stack = RepoStack(
            language="go",
            test_command="go test ./... 2>&1",
            lint_command="golangci-lint run ./... 2>&1",
            detected_from="go.mod",
            confidence=0.95,
        )
        validation = {
            "valid": True,
            "issues": [],
            "classification": "bug",
            "injection_detected": False,
            "triage_result": {
                "classification": "bug",
                "confidence": 0.9,
                "severity": "high",
                "reasoning": "Clearly a bug.",
                "affected_components": ["pkg/controller/reconciler.go"],
            },
            "verified_components": [{"path": "pkg/controller/reconciler.go", "found": True}],
            "reproduction": {"attempted": False},
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert "detected_stack" in result.artifacts
        stack = result.artifacts["detected_stack"]
        assert stack["language"] == "go"
        assert stack["detected_from"] == "go.mod"
        assert stack["confidence"] == 0.95
        assert "go test" in stack["test_command"]

    @pytest.mark.asyncio
    async def test_ambiguous_as_bug_includes_detected_stack(self):
        phase = TriagePhase(
            llm=MockProvider(responses=[]),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake-repo",
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "issue",
                "body": "body",
            },
            config=EngineConfig(),
        )
        phase._detected_stack = RepoStack(
            language="python",
            test_command="python -m pytest --tb=short -q 2>&1",
            lint_command="ruff check . 2>&1",
            detected_from="pyproject.toml",
            confidence=0.95,
        )
        validation = {
            "valid": True,
            "issues": [],
            "classification": "ambiguous",
            "injection_detected": False,
            "triage_result": {
                "classification": "ambiguous",
                "confidence": 0.5,
                "severity": "medium",
                "reasoning": "Ambiguous but likely a bug.",
            },
            "verified_components": [],
            "reproduction": {},
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert result.next_phase == "implement"
        assert "detected_stack" in result.artifacts
        stack = result.artifacts["detected_stack"]
        assert stack["language"] == "python"
        assert stack["detected_from"] == "pyproject.toml"

    @pytest.mark.asyncio
    async def test_no_stack_when_detection_not_run(self):
        phase = TriagePhase(
            llm=MockProvider(responses=[]),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake-repo",
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "issue",
                "body": "body",
            },
            config=EngineConfig(),
        )
        validation = {
            "valid": True,
            "issues": [],
            "classification": "bug",
            "injection_detected": False,
            "triage_result": {
                "classification": "bug",
                "confidence": 0.9,
                "severity": "high",
                "reasoning": "Bug.",
                "affected_components": [],
            },
            "verified_components": [],
            "reproduction": {},
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert "detected_stack" not in result.artifacts

    @pytest.mark.asyncio
    async def test_escalation_does_not_include_stack(self):
        phase = TriagePhase(
            llm=MockProvider(responses=[]),
            logger=StructuredLogger(),
            tracer=Tracer(),
            repo_path="/tmp/fake-repo",
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "feature",
                "body": "body",
            },
            config=EngineConfig(),
        )
        phase._detected_stack = RepoStack(
            language="go",
            test_command="go test ./...",
            lint_command="golangci-lint run ./...",
            detected_from="go.mod",
            confidence=0.95,
        )
        validation = {
            "valid": True,
            "issues": [],
            "classification": "feature",
            "injection_detected": False,
            "triage_result": {
                "classification": "feature",
                "confidence": 0.85,
                "severity": "medium",
                "reasoning": "Feature request.",
            },
            "verified_components": [],
            "reproduction": {},
        }
        result = await phase.reflect(validation)
        assert result.escalate is True
        assert "detected_stack" not in result.artifacts


# ------------------------------------------------------------------
# 2. Implement inherits triage stack
# ------------------------------------------------------------------


class TestImplementInheritsStack:
    """Verify implement phase extracts and uses the triage stack."""

    def test_extract_triage_stack_present(self):
        phase = _make_implement(prior_results=[_triage_result_with_stack()])
        stack = phase._extract_triage_stack()
        assert stack is not None
        assert stack.language == "go"
        assert "go test" in stack.test_command
        assert "golangci-lint" in stack.lint_command
        assert "triage_handoff" in stack.detected_from
        assert stack.confidence == 0.95

    def test_extract_triage_stack_absent(self):
        phase = _make_implement(prior_results=[_triage_result_without_stack()])
        stack = phase._extract_triage_stack()
        assert stack is None

    def test_extract_triage_stack_no_prior_results(self):
        phase = _make_implement(prior_results=[])
        stack = phase._extract_triage_stack()
        assert stack is None

    def test_extract_triage_stack_skips_failed_triage(self):
        failed_triage = PhaseResult(
            phase="triage",
            success=False,
            should_continue=False,
            artifacts={
                "detected_stack": RepoStack("go", "go test", "lint", "go.mod", 0.95).to_dict(),
            },
        )
        phase = _make_implement(prior_results=[failed_triage])
        stack = phase._extract_triage_stack()
        assert stack is None

    def test_config_override_applied_on_top(self):
        from engine.config import load_config

        cfg = load_config(
            overrides={
                "phases": {
                    "implement": {
                        "test_command": "make test 2>&1",
                        "lint_command": "make lint 2>&1",
                    },
                },
            }
        )
        phase = _make_implement(prior_results=[_triage_result_with_stack()], config=cfg)
        stack = phase._extract_triage_stack()
        assert stack is not None
        assert stack.test_command == "make test 2>&1"
        assert stack.lint_command == "make lint 2>&1"
        assert stack.language == "go"

    def test_extract_triage_stack_picks_latest(self):
        old = _triage_result_with_stack(language="python")
        old.artifacts["detected_stack"] = RepoStack(
            "python", "pytest", "ruff check .", "pyproject.toml", 0.95
        ).to_dict()
        new = _triage_result_with_stack(language="go")
        phase = _make_implement(prior_results=[old, new])
        stack = phase._extract_triage_stack()
        assert stack is not None
        assert stack.language == "go"

    def test_extract_triage_stack_ignores_malformed_dict(self):
        bad_triage = PhaseResult(
            phase="triage",
            success=True,
            should_continue=True,
            next_phase="implement",
            artifacts={"detected_stack": {"not_language": "go"}},
        )
        phase = _make_implement(prior_results=[bad_triage])
        stack = phase._extract_triage_stack()
        assert stack is None

    def test_extract_triage_stack_ignores_non_dict(self):
        bad_triage = PhaseResult(
            phase="triage",
            success=True,
            should_continue=True,
            next_phase="implement",
            artifacts={"detected_stack": "not a dict"},
        )
        phase = _make_implement(prior_results=[bad_triage])
        stack = phase._extract_triage_stack()
        assert stack is None

    @pytest.mark.asyncio
    async def test_observe_uses_triage_stack(self):
        phase = _make_implement(prior_results=[_triage_result_with_stack()])
        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.language == "go"
        assert "triage_handoff" in phase._detected_stack.detected_from

    @pytest.mark.asyncio
    async def test_observe_falls_back_to_independent_detection(self):
        phase = _make_implement(prior_results=[_triage_result_without_stack()])
        await phase.observe()
        assert phase._detected_stack is not None
        assert "triage_handoff" not in phase._detected_stack.detected_from


# ------------------------------------------------------------------
# 3. Validate inherits triage stack
# ------------------------------------------------------------------


class TestValidateInheritsStack:
    """Verify validate phase extracts and uses the triage stack."""

    def test_extract_triage_stack_present(self):
        prior = [
            _triage_result_with_stack(),
            PhaseResult(
                phase="implement",
                success=True,
                should_continue=True,
                next_phase="review",
                findings={"root_cause": "nil check"},
                artifacts={"files_changed": ["reconciler.go"], "diff": "diff"},
            ),
            PhaseResult(
                phase="review",
                success=True,
                should_continue=True,
                next_phase="validate",
                findings={"verdict": "approve", "summary": "ok"},
            ),
        ]
        phase = _make_validate(prior_results=prior)
        stack = phase._extract_triage_stack()
        assert stack is not None
        assert stack.language == "go"
        assert "triage_handoff" in stack.detected_from

    def test_extract_triage_stack_absent(self):
        prior = [
            _triage_result_without_stack(),
            PhaseResult(
                phase="implement",
                success=True,
                should_continue=True,
                next_phase="review",
                findings={},
                artifacts={"files_changed": [], "diff": ""},
            ),
        ]
        phase = _make_validate(prior_results=prior)
        stack = phase._extract_triage_stack()
        assert stack is None

    def test_extract_triage_stack_no_prior(self):
        phase = _make_validate(prior_results=[])
        stack = phase._extract_triage_stack()
        assert stack is None

    def test_config_override_applied_on_top(self):
        from engine.config import load_config

        cfg = load_config(
            overrides={
                "phases": {
                    "validate": {
                        "test_command": "make test 2>&1",
                        "lint_command": "make lint 2>&1",
                    },
                },
            }
        )
        prior = [_triage_result_with_stack()]
        phase = _make_validate(prior_results=prior, config=cfg)
        stack = phase._extract_triage_stack()
        assert stack is not None
        assert stack.test_command == "make test 2>&1"
        assert stack.lint_command == "make lint 2>&1"
        assert stack.language == "go"

    @pytest.mark.asyncio
    async def test_observe_uses_triage_stack(self):
        prior = [
            _triage_result_with_stack(),
            PhaseResult(
                phase="implement",
                success=True,
                should_continue=True,
                next_phase="review",
                findings={"root_cause": "nil"},
                artifacts={"files_changed": [], "diff": ""},
            ),
            PhaseResult(
                phase="review",
                success=True,
                should_continue=True,
                next_phase="validate",
                findings={"verdict": "approve", "summary": "ok"},
            ),
        ]
        phase = _make_validate(prior_results=prior)
        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.language == "go"
        assert "triage_handoff" in phase._detected_stack.detected_from

    @pytest.mark.asyncio
    async def test_observe_falls_back_to_independent_detection(self):
        prior = [
            _triage_result_without_stack(),
            PhaseResult(
                phase="implement",
                success=True,
                should_continue=True,
                next_phase="review",
                findings={},
                artifacts={"files_changed": [], "diff": ""},
            ),
        ]
        phase = _make_validate(prior_results=prior)
        await phase.observe()
        assert phase._detected_stack is not None
        assert "triage_handoff" not in phase._detected_stack.detected_from


# ------------------------------------------------------------------
# 4. Head limit verification
# ------------------------------------------------------------------


class TestHeadLimitIncreased:
    """Verify file listing head limits are 200 (not 100) for implement and validate."""

    def test_implement_head_200(self):
        import inspect

        src = inspect.getsource(ImplementPhase.observe)
        assert "head -200" in src
        assert "head -100" not in src

    def test_validate_head_200(self):
        import inspect

        src = inspect.getsource(ValidatePhase.observe)
        assert "head -200" in src
        assert "head -100" not in src


# ------------------------------------------------------------------
# 5. RepoStack round-trip serialization
# ------------------------------------------------------------------


class TestRepoStackSerialization:
    """Verify to_dict() produces values that _extract_triage_stack can consume."""

    def test_round_trip(self):
        original = RepoStack(
            language="rust",
            test_command="cargo test 2>&1",
            lint_command="cargo clippy -- -D warnings 2>&1",
            detected_from="Cargo.toml",
            confidence=0.95,
        )
        serialized = original.to_dict()
        triage_result = PhaseResult(
            phase="triage",
            success=True,
            should_continue=True,
            next_phase="implement",
            artifacts={"detected_stack": serialized},
        )
        phase = _make_implement(prior_results=[triage_result])
        reconstructed = phase._extract_triage_stack()
        assert reconstructed is not None
        assert reconstructed.language == original.language
        assert reconstructed.test_command == original.test_command
        assert reconstructed.lint_command == original.lint_command
        assert reconstructed.confidence == original.confidence
        assert "triage_handoff" in reconstructed.detected_from

    def test_round_trip_with_node_stack(self):
        original = RepoStack(
            language="node",
            test_command="npm test 2>&1",
            lint_command="npx eslint . 2>&1",
            detected_from="package.json",
            confidence=0.95,
        )
        triage_result = PhaseResult(
            phase="triage",
            success=True,
            should_continue=True,
            next_phase="implement",
            artifacts={"detected_stack": original.to_dict()},
        )
        phase = _make_validate(prior_results=[triage_result])
        reconstructed = phase._extract_triage_stack()
        assert reconstructed is not None
        assert reconstructed.language == "node"
        assert "npm test" in reconstructed.test_command
