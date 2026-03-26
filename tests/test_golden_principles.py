"""Tests for Golden Principles enforcement (Phase 6.1).

Validates the AST-based checker catches violations and passes clean code.
Tests cover:
- Violation and CheckResult dataclasses
- AST helper functions
- Each golden principle check with synthetic source code
- CLI entry point
- Integration with the real engine codebase
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from engine.golden_principles import (
    CheckResult,
    GoldenPrinciplesChecker,
    Violation,
    _class_contains_attr_call,
    _class_contains_call,
    _class_references_attr,
    _contains_attr_call,
    _count_attr_calls,
    _find_method,
    _inherits_from,
    _is_dotted_access,
    main,
)

# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestViolation:
    def test_str_format(self):
        v = Violation(
            file="engine/phases/foo.py",
            line=42,
            principle="P1: Every action is logged",
            code="GP001",
            message="Missing logger call",
        )
        s = str(v)
        assert "engine/phases/foo.py:42" in s
        assert "[GP001]" in s
        assert "P1" in s
        assert "Missing logger call" in s

    def test_fields(self):
        v = Violation(file="a.py", line=1, principle="P1", code="GP001", message="msg")
        assert v.file == "a.py"
        assert v.line == 1


class TestCheckResult:
    def test_empty_result_passes(self):
        r = CheckResult()
        assert r.passed is True

    def test_result_with_violations_fails(self):
        r = CheckResult(
            violations=[Violation(file="a.py", line=1, principle="P1", code="GP001", message="bad")]
        )
        assert r.passed is False

    def test_summary_pass(self):
        r = CheckResult(checks_run=5, files_scanned=3)
        assert "PASS" in r.summary()
        assert "5 checks" in r.summary()
        assert "3 files" in r.summary()
        assert "0 violation" in r.summary()

    def test_summary_fail(self):
        r = CheckResult(
            checks_run=5,
            files_scanned=3,
            violations=[Violation(file="x.py", line=1, principle="P1", code="GP001", message="m")],
        )
        assert "FAIL" in r.summary()
        assert "1 violation" in r.summary()


# ---------------------------------------------------------------------------
# AST helper tests
# ---------------------------------------------------------------------------


def _parse(source: str) -> ast.Module:
    return ast.parse(textwrap.dedent(source))


def _first_class(source: str) -> ast.ClassDef:
    tree = _parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            return node
    raise ValueError("No class found")


class TestInheritsFrom:
    def test_direct_name(self):
        cls = _first_class("class Foo(Phase): pass")
        assert _inherits_from(cls, "Phase") is True

    def test_no_match(self):
        cls = _first_class("class Foo(Bar): pass")
        assert _inherits_from(cls, "Phase") is False

    def test_no_bases(self):
        cls = _first_class("class Foo: pass")
        assert _inherits_from(cls, "Phase") is False

    def test_attribute_base(self):
        cls = _first_class("class Foo(module.Phase): pass")
        assert _inherits_from(cls, "Phase") is True


class TestContainsAttrCall:
    def test_finds_self_logger_info(self):
        tree = _parse(
            """
            class X:
                def f(self):
                    self.logger.info("hi")
            """
        )
        method = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "f":
                method = node
                break
        assert method is not None
        assert _contains_attr_call(method, "self", "logger") is True

    def test_missing_call(self):
        tree = _parse(
            """
            class X:
                def f(self):
                    pass
            """
        )
        method = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "f":
                method = node
                break
        assert method is not None
        assert _contains_attr_call(method, "self", "logger") is False

    def test_direct_attr_call(self):
        tree = _parse(
            """
            class X:
                def f(self):
                    self.logger("hi")
            """
        )
        method = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "f":
                method = node
                break
        assert _contains_attr_call(method, "self", "logger") is True


class TestClassContainsAttrCall:
    def test_with_method_name(self):
        cls = _first_class(
            """
            class Foo(Phase):
                async def plan(self):
                    await self.llm.complete(system_prompt="x", messages=[])
            """
        )
        assert _class_contains_attr_call(cls, "self", "llm", "complete") is True

    def test_without_method_name(self):
        cls = _first_class(
            """
            class Foo(Phase):
                async def plan(self):
                    self.tracer.record_llm_call(description="x")
            """
        )
        assert _class_contains_attr_call(cls, "self", "tracer") is True

    def test_missing(self):
        cls = _first_class(
            """
            class Foo(Phase):
                async def plan(self):
                    pass
            """
        )
        assert _class_contains_attr_call(cls, "self", "llm", "complete") is False


class TestClassContainsCall:
    def test_finds_method_call(self):
        cls = _first_class(
            """
            class Foo(Phase):
                async def plan(self):
                    self._wrap_untrusted_content("x")
            """
        )
        assert _class_contains_call(cls, "_wrap_untrusted_content") is True

    def test_missing(self):
        cls = _first_class(
            """
            class Foo(Phase):
                async def plan(self):
                    pass
            """
        )
        assert _class_contains_call(cls, "_wrap_untrusted_content") is False


class TestIsDottedAccess:
    def test_matches(self):
        tree = _parse("self.config")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "config":
                assert _is_dotted_access(node, "self", "config") is True

    def test_no_match(self):
        tree = _parse("other.config")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "config":
                assert _is_dotted_access(node, "self", "config") is False


class TestClassReferencesAttr:
    def test_direct_access(self):
        cls = _first_class(
            """
            class Foo(Phase):
                def f(self):
                    x = self.config.llm.temperature
            """
        )
        assert _class_references_attr(cls, "self", "config") is True

    def test_missing(self):
        cls = _first_class(
            """
            class Foo(Phase):
                def f(self):
                    x = 42
            """
        )
        assert _class_references_attr(cls, "self", "config") is False


class TestCountAttrCalls:
    def test_counts_multiple(self):
        cls = _first_class(
            """
            class Foo(Phase):
                async def plan(self):
                    await self.llm.complete(system_prompt="a", messages=[])
                async def _refine(self):
                    await self.llm.complete(system_prompt="b", messages=[])
            """
        )
        assert _count_attr_calls(cls, "self", "llm", "complete") == 2

    def test_zero(self):
        cls = _first_class(
            """
            class Foo(Phase):
                async def plan(self):
                    pass
            """
        )
        assert _count_attr_calls(cls, "self", "llm", "complete") == 0


class TestFindMethod:
    def test_finds_async_method(self):
        cls = _first_class(
            """
            class Foo:
                async def run(self):
                    pass
            """
        )
        m = _find_method(cls, "run")
        assert m is not None
        assert m.name == "run"

    def test_finds_sync_method(self):
        cls = _first_class(
            """
            class Foo:
                def process(self):
                    pass
            """
        )
        m = _find_method(cls, "process")
        assert m is not None

    def test_not_found(self):
        cls = _first_class(
            """
            class Foo:
                def other(self):
                    pass
            """
        )
        assert _find_method(cls, "run") is None


# ---------------------------------------------------------------------------
# Golden Principle Check tests (synthetic source code)
# ---------------------------------------------------------------------------


def _write_phase_file(tmp_path: Path, name: str, source: str) -> None:
    """Write a synthetic phase file under tmp_path/engine/phases/."""
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir(parents=True, exist_ok=True)
    (phases_dir / f"{name}.py").write_text(textwrap.dedent(source))


def _write_engine_file(tmp_path: Path, rel_path: str, source: str) -> None:
    """Write a synthetic engine file at tmp_path/rel_path."""
    full_path = tmp_path / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(textwrap.dedent(source))


class TestCheckPhaseLogging:
    """GP001: Every phase method must call self.logger."""

    def test_compliant_phase(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "good",
            """
            from engine.phases.base import Phase
            class GoodPhase(Phase):
                name = "good"
                async def observe(self):
                    self.logger.info("observing")
                    return {}
                async def plan(self, obs):
                    self.logger.info("planning")
                    return {}
                async def act(self, plan):
                    self.logger.info("acting")
                    return {}
                async def validate(self, result):
                    self.logger.info("validating")
                    return {}
                async def reflect(self, validation):
                    self.logger.info("reflecting")
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_phase_logging()
        assert len(checker._result.violations) == 0

    def test_missing_logger_in_act(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "bad",
            """
            from engine.phases.base import Phase
            class BadPhase(Phase):
                name = "bad"
                async def observe(self):
                    self.logger.info("ok")
                    return {}
                async def plan(self, obs):
                    self.logger.info("ok")
                    return {}
                async def act(self, plan):
                    return {}
                async def validate(self, result):
                    self.logger.info("ok")
                    return {}
                async def reflect(self, validation):
                    self.logger.info("ok")
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_phase_logging()
        assert len(checker._result.violations) == 1
        assert checker._result.violations[0].code == "GP001"
        assert "act" in checker._result.violations[0].message

    def test_skips_base_and_init(self, tmp_path):
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir(parents=True, exist_ok=True)
        (phases_dir / "__init__.py").write_text("# init")
        (phases_dir / "base.py").write_text("class Phase: pass")
        (phases_dir / "prompt_loader.py").write_text("def load(): pass")
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_phase_logging()
        assert len(checker._result.violations) == 0


class TestCheckUntrustedSeparation:
    """GP003: Phases calling self.llm.complete() must use _wrap_untrusted_content."""

    def test_compliant_phase(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "good",
            """
            from engine.phases.base import Phase
            class GoodPhase(Phase):
                name = "good"
                async def plan(self, obs):
                    untrusted = self._wrap_untrusted_content("body")
                    await self.llm.complete(system_prompt="x", messages=[])
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_untrusted_separation()
        assert len(checker._result.violations) == 0

    def test_missing_wrap(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "bad",
            """
            from engine.phases.base import Phase
            class BadPhase(Phase):
                name = "bad"
                async def plan(self, obs):
                    await self.llm.complete(system_prompt="x", messages=[])
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_untrusted_separation()
        assert len(checker._result.violations) == 1
        assert checker._result.violations[0].code == "GP003"

    def test_no_llm_call_is_fine(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "nollm",
            """
            from engine.phases.base import Phase
            class NoLLMPhase(Phase):
                name = "nollm"
                async def plan(self, obs):
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_untrusted_separation()
        assert len(checker._result.violations) == 0


class TestCheckLLMProvenance:
    """GP008: Every llm.complete() must have a matching tracer.record_llm_call()."""

    def test_matched_calls(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "good",
            """
            from engine.phases.base import Phase
            class GoodPhase(Phase):
                name = "good"
                async def plan(self, obs):
                    resp = await self.llm.complete(system_prompt="x", messages=[])
                    self.tracer.record_llm_call(description="x", model="m",
                        provider="p", tokens_in=1, tokens_out=1, latency_ms=1.0)
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_llm_provenance()
        assert len(checker._result.violations) == 0

    def test_missing_trace(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "bad",
            """
            from engine.phases.base import Phase
            class BadPhase(Phase):
                name = "bad"
                async def plan(self, obs):
                    resp = await self.llm.complete(system_prompt="x", messages=[])
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_llm_provenance()
        assert len(checker._result.violations) == 1
        assert checker._result.violations[0].code == "GP008"

    def test_mismatched_counts(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "unbalanced",
            """
            from engine.phases.base import Phase
            class Unbalanced(Phase):
                name = "unbalanced"
                async def plan(self, obs):
                    await self.llm.complete(system_prompt="a", messages=[])
                    self.tracer.record_llm_call(description="a", model="m",
                        provider="p", tokens_in=1, tokens_out=1, latency_ms=1.0)
                    return {}
                async def _refine(self):
                    await self.llm.complete(system_prompt="b", messages=[])
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_llm_provenance()
        assert len(checker._result.violations) == 1
        assert "2 llm.complete()" in checker._result.violations[0].message
        assert "1 record_llm_call()" in checker._result.violations[0].message


class TestCheckToolExecutionTracing:
    """GP001: ToolExecutor.execute() must call self.tracer.record_action()."""

    def test_compliant_executor(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "tools/executor.py",
            """
            class ToolExecutor:
                async def execute(self, tool_name, **kwargs):
                    self.tracer.record_action(
                        action_type=tool_name, description="x"
                    )
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_tool_execution_tracing()
        assert len(checker._result.violations) == 0

    def test_missing_tracer(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "tools/executor.py",
            """
            class ToolExecutor:
                async def execute(self, tool_name, **kwargs):
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_tool_execution_tracing()
        assert len(checker._result.violations) == 1
        assert checker._result.violations[0].code == "GP001"
        assert "ToolExecutor.execute()" in checker._result.violations[0].message


class TestCheckIterationBounds:
    """GP005: RalphLoop.run() must check max_iterations and time_budget."""

    def test_compliant_loop(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "loop.py",
            """
            class RalphLoop:
                async def run(self):
                    if self._total_iterations >= self.config.loop.max_iterations:
                        pass
                    if self._check_time_budget():
                        pass
                    return self.execution
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_iteration_bounds()
        assert len(checker._result.violations) == 0

    def test_missing_max_iterations(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "loop.py",
            """
            class RalphLoop:
                async def run(self):
                    if self._check_time_budget():
                        pass
                    return self.execution
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_iteration_bounds()
        violations = [v for v in checker._result.violations if "max_iterations" in v.message]
        assert len(violations) == 1

    def test_missing_time_budget(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "loop.py",
            """
            class RalphLoop:
                async def run(self):
                    if self._total_iterations >= self.config.loop.max_iterations:
                        pass
                    return self.execution
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_iteration_bounds()
        violations = [v for v in checker._result.violations if "time_budget" in v.message]
        assert len(violations) == 1


class TestCheckReportPublishing:
    """GP009: RalphLoop._write_outputs() must trigger report generation."""

    def test_compliant_loop(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "loop.py",
            """
            class RalphLoop:
                def _write_outputs(self, status):
                    self._publish_reports()
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_report_publishing()
        assert len(checker._result.violations) == 0

    def test_missing_report_call(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "loop.py",
            """
            class RalphLoop:
                def _write_outputs(self, status):
                    with open("status.txt") as f:
                        f.write(status)
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_report_publishing()
        assert len(checker._result.violations) == 1
        assert checker._result.violations[0].code == "GP009"

    def test_missing_write_outputs(self, tmp_path):
        _write_engine_file(
            tmp_path,
            "loop.py",
            """
            class RalphLoop:
                async def run(self):
                    pass
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_report_publishing()
        assert len(checker._result.violations) == 1
        assert "no _write_outputs()" in checker._result.violations[0].message


class TestCheckConfigurationUsage:
    """GP010: Phase implementations must reference self.config."""

    def test_compliant_phase(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "good",
            """
            from engine.phases.base import Phase
            class GoodPhase(Phase):
                name = "good"
                async def plan(self, obs):
                    temp = self.config.llm.temperature
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_configuration_usage()
        assert len(checker._result.violations) == 0

    def test_missing_config(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "bad",
            """
            from engine.phases.base import Phase
            class BadPhase(Phase):
                name = "bad"
                async def plan(self, obs):
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_configuration_usage()
        assert len(checker._result.violations) == 1
        assert checker._result.violations[0].code == "GP010"


# ---------------------------------------------------------------------------
# check_all() integration
# ---------------------------------------------------------------------------


class TestCheckAll:
    def test_fully_compliant_engine(self, tmp_path):
        """A synthetic engine that passes all checks."""
        _write_phase_file(
            tmp_path,
            "myphase",
            """
            from engine.phases.base import Phase
            class MyPhase(Phase):
                name = "myp"
                async def observe(self):
                    self.logger.info("observe")
                    return {}
                async def plan(self, obs):
                    self.logger.info("plan")
                    temp = self.config.llm.temperature
                    untrusted = self._wrap_untrusted_content("body")
                    resp = await self.llm.complete(system_prompt="x", messages=[])
                    self.tracer.record_llm_call(description="x", model="m",
                        provider="p", tokens_in=1, tokens_out=1, latency_ms=1.0)
                    return {}
                async def act(self, plan):
                    self.logger.info("act")
                    return {}
                async def validate(self, result):
                    self.logger.info("validate")
                    return {}
                async def reflect(self, validation):
                    self.logger.info("reflect")
                    return {}
            """,
        )
        _write_engine_file(
            tmp_path,
            "tools/executor.py",
            """
            class ToolExecutor:
                async def execute(self, tool_name, **kwargs):
                    self.tracer.record_action(action_type=tool_name, description="x")
                    return {}
            """,
        )
        _write_engine_file(
            tmp_path,
            "loop.py",
            """
            class RalphLoop:
                async def run(self):
                    if self._total_iterations >= self.config.loop.max_iterations:
                        pass
                    if self._check_time_budget():
                        pass
                    return self.execution
                def _write_outputs(self, status):
                    self._publish_reports()
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        result = checker.check_all()
        assert result.passed is True
        assert result.checks_run > 0
        assert result.files_scanned > 0

    def test_multiple_violations(self, tmp_path):
        """Catch multiple violations across different checks."""
        _write_phase_file(
            tmp_path,
            "bad_phase",
            """
            from engine.phases.base import Phase
            class BadPhase(Phase):
                name = "bad"
                async def observe(self):
                    return {}
                async def plan(self, obs):
                    await self.llm.complete(system_prompt="x", messages=[])
                    return {}
                async def act(self, plan):
                    return {}
                async def validate(self, result):
                    return {}
                async def reflect(self, validation):
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        result = checker.check_all()
        assert result.passed is False
        codes = {v.code for v in result.violations}
        assert "GP001" in codes
        assert "GP003" in codes
        assert "GP008" in codes
        assert "GP010" in codes


# ---------------------------------------------------------------------------
# Integration with the real engine codebase
# ---------------------------------------------------------------------------


class TestRealEngineCompliance:
    """The real engine codebase must pass all golden principle checks."""

    @pytest.fixture()
    def engine_path(self) -> Path:
        return Path(__file__).parent.parent / "engine"

    def test_all_checks_pass(self, engine_path):
        if not engine_path.is_dir():
            pytest.skip("Engine directory not found")
        checker = GoldenPrinciplesChecker(engine_path)
        result = checker.check_all()
        for v in result.violations:
            print(f"  VIOLATION: {v}")
        assert result.passed, f"Golden Principles FAIL: {len(result.violations)} violation(s)"

    def test_files_scanned(self, engine_path):
        if not engine_path.is_dir():
            pytest.skip("Engine directory not found")
        checker = GoldenPrinciplesChecker(engine_path)
        result = checker.check_all()
        assert result.files_scanned >= 5

    def test_checks_run(self, engine_path):
        if not engine_path.is_dir():
            pytest.skip("Engine directory not found")
        checker = GoldenPrinciplesChecker(engine_path)
        result = checker.check_all()
        assert result.checks_run >= 15

    def test_phase_logging_triage(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_phase_logging()
        triage_violations = [v for v in checker._result.violations if "triage" in v.file.lower()]
        assert len(triage_violations) == 0

    def test_phase_logging_implement(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_phase_logging()
        impl_violations = [v for v in checker._result.violations if "implement" in v.file.lower()]
        assert len(impl_violations) == 0

    def test_phase_logging_review(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_phase_logging()
        review_violations = [v for v in checker._result.violations if "review" in v.file.lower()]
        assert len(review_violations) == 0

    def test_phase_logging_validate(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_phase_logging()
        validate_violations = [
            v for v in checker._result.violations if "validate" in v.file.lower()
        ]
        assert len(validate_violations) == 0

    def test_untrusted_separation_all_phases(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_untrusted_separation()
        assert len(checker._result.violations) == 0

    def test_llm_provenance_all_phases(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_llm_provenance()
        assert len(checker._result.violations) == 0

    def test_tool_execution_tracing(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_tool_execution_tracing()
        assert len(checker._result.violations) == 0

    def test_iteration_bounds(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_iteration_bounds()
        assert len(checker._result.violations) == 0

    def test_report_publishing(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_report_publishing()
        assert len(checker._result.violations) == 0

    def test_configuration_usage(self, engine_path):
        checker = GoldenPrinciplesChecker(engine_path)
        checker._check_configuration_usage()
        assert len(checker._result.violations) == 0


# ---------------------------------------------------------------------------
# CLI entry point tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_main_passes_on_real_engine(self):
        exit_code = main(["engine"])
        assert exit_code == 0

    def test_main_missing_path(self, capsys):
        exit_code = main(["/nonexistent/path"])
        assert exit_code == 1

    def test_main_default_engine_path(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "engine").mkdir()
        exit_code = main([])
        assert exit_code == 0

    def test_main_with_violations(self, tmp_path, capsys):
        engine_dir = tmp_path / "engine"
        engine_dir.mkdir()
        _write_phase_file(
            engine_dir,
            "bad",
            """
            from engine.phases.base import Phase
            class BadPhase(Phase):
                name = "bad"
                async def observe(self):
                    return {}
                async def plan(self, obs):
                    return {}
                async def act(self, plan):
                    return {}
                async def validate(self, result):
                    return {}
                async def reflect(self, validation):
                    return {}
            """,
        )
        exit_code = main([str(engine_dir)])
        assert exit_code == 1
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "GP001" in captured.err


# ---------------------------------------------------------------------------
# Edge case tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_phases_dir(self, tmp_path):
        (tmp_path / "phases").mkdir(parents=True)
        checker = GoldenPrinciplesChecker(tmp_path)
        result = checker.check_all()
        assert result.passed is True

    def test_no_phases_dir(self, tmp_path):
        checker = GoldenPrinciplesChecker(tmp_path)
        result = checker.check_all()
        assert result.passed is True

    def test_syntax_error_in_file(self, tmp_path):
        _write_phase_file(tmp_path, "broken", "class this is not valid python !!!")
        checker = GoldenPrinciplesChecker(tmp_path)
        result = checker.check_all()
        assert result.passed is True

    def test_non_phase_class_ignored(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "helper",
            """
            class NotAPhase:
                def act(self):
                    pass
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_phase_logging()
        assert len(checker._result.violations) == 0

    def test_multiple_phases_in_one_file(self, tmp_path):
        _write_phase_file(
            tmp_path,
            "multi",
            """
            from engine.phases.base import Phase
            class PhaseA(Phase):
                name = "a"
                async def observe(self):
                    self.logger.info("x")
                    return {}
                async def plan(self, obs):
                    self.logger.info("x")
                    return {}
                async def act(self, plan):
                    self.logger.info("x")
                    return {}
                async def validate(self, result):
                    self.logger.info("x")
                    return {}
                async def reflect(self, validation):
                    self.logger.info("x")
                    return {}
            class PhaseB(Phase):
                name = "b"
                async def observe(self):
                    return {}
                async def plan(self, obs):
                    return {}
                async def act(self, plan):
                    return {}
                async def validate(self, result):
                    return {}
                async def reflect(self, validation):
                    return {}
            """,
        )
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_phase_logging()
        violations_a = [v for v in checker._result.violations if "PhaseA" in v.message]
        violations_b = [v for v in checker._result.violations if "PhaseB" in v.message]
        assert len(violations_a) == 0
        assert len(violations_b) == 5

    def test_no_tool_executor_file(self, tmp_path):
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_tool_execution_tracing()
        assert len(checker._result.violations) == 0

    def test_no_loop_file(self, tmp_path):
        checker = GoldenPrinciplesChecker(tmp_path)
        checker._check_iteration_bounds()
        checker._check_report_publishing()
        assert len(checker._result.violations) == 0
