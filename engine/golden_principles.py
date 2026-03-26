"""Golden Principles enforcement — AST-based static analysis for SPEC §7 compliance.

Scans the engine codebase and checks that:
- P1: Every action is logged (phase methods log, tool execution traces)
- P3: Prompts never mix trusted and untrusted content
- P5: Iterations are bounded (loop checks caps and budgets)
- P8: Provenance is automatic (every LLM call is traced with model info)
- P9: Demos are a byproduct (report publishing wired into loop outputs)
- P10: Configuration is declarative (phases use self.config)

Run via: python -m engine.golden_principles [engine_path]
Exit code 0 = all checks pass; exit code 1 = violations found.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Violation:
    """A single golden principle violation found by static analysis."""

    file: str
    line: int
    principle: str
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: [{self.code}] ({self.principle}) {self.message}"


@dataclass
class CheckResult:
    """Aggregated result of all golden principle checks."""

    violations: list[Violation] = field(default_factory=list)
    checks_run: int = 0
    files_scanned: int = 0

    @property
    def passed(self) -> bool:
        return len(self.violations) == 0

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return (
            f"Golden Principles: {status} — "
            f"{self.checks_run} checks, "
            f"{self.files_scanned} files scanned, "
            f"{len(self.violations)} violation(s)"
        )


PHASE_METHODS = ("observe", "plan", "act", "validate", "reflect")


class GoldenPrinciplesChecker:
    """AST-based checker that scans the engine codebase for principle violations."""

    def __init__(self, engine_path: str | Path):
        self.engine_path = Path(engine_path)
        self._result = CheckResult()

    def check_all(self) -> CheckResult:
        """Run all golden principle checks and return aggregated result."""
        self._result = CheckResult()

        self._check_phase_logging()
        self._check_untrusted_separation()
        self._check_llm_provenance()
        self._check_tool_execution_tracing()
        self._check_iteration_bounds()
        self._check_report_publishing()
        self._check_configuration_usage()

        return self._result

    def _check_phase_logging(self) -> None:
        """P1: Every phase method must call self.logger."""
        phases_dir = self.engine_path / "phases"
        if not phases_dir.is_dir():
            return

        for py_file in sorted(phases_dir.glob("*.py")):
            if py_file.name in ("__init__.py", "base.py", "prompt_loader.py"):
                continue

            tree = self._parse_file(py_file)
            if tree is None:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not _inherits_from(node, "Phase"):
                    continue

                self._result.checks_run += 1
                for method in node.body:
                    if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if method.name not in PHASE_METHODS:
                        continue

                    if not _contains_attr_call(method, "self", "logger"):
                        self._result.violations.append(
                            Violation(
                                file=str(py_file.relative_to(self.engine_path.parent)),
                                line=method.lineno,
                                principle="P1: Every action is logged",
                                code="GP001",
                                message=(
                                    f"Phase method '{method.name}' in class "
                                    f"'{node.name}' does not call self.logger"
                                ),
                            )
                        )

    def _check_untrusted_separation(self) -> None:
        """P3: Every phase that calls self.llm.complete() must use _wrap_untrusted_content."""
        phases_dir = self.engine_path / "phases"
        if not phases_dir.is_dir():
            return

        for py_file in sorted(phases_dir.glob("*.py")):
            if py_file.name in ("__init__.py", "base.py", "prompt_loader.py"):
                continue

            tree = self._parse_file(py_file)
            if tree is None:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not _inherits_from(node, "Phase"):
                    continue

                self._result.checks_run += 1
                has_llm_call = _class_contains_attr_call(node, "self", "llm", "complete")
                has_wrap = _class_contains_call(node, "_wrap_untrusted_content")

                if has_llm_call and not has_wrap:
                    self._result.violations.append(
                        Violation(
                            file=str(py_file.relative_to(self.engine_path.parent)),
                            line=node.lineno,
                            principle="P3: Prompts separate trusted/untrusted",
                            code="GP003",
                            message=(
                                f"Class '{node.name}' calls self.llm.complete() "
                                f"but never calls _wrap_untrusted_content()"
                            ),
                        )
                    )

    def _check_llm_provenance(self) -> None:
        """P8: Every self.llm.complete() call must be paired with a record_llm_call().

        Accepts both ``self.tracer.record_llm_call()`` (legacy direct call)
        and ``self.record_llm_call()`` (preferred helper that also updates
        LoopMetrics counters).
        """
        phases_dir = self.engine_path / "phases"
        if not phases_dir.is_dir():
            return

        for py_file in sorted(phases_dir.glob("*.py")):
            if py_file.name in ("__init__.py", "base.py", "prompt_loader.py"):
                continue

            tree = self._parse_file(py_file)
            if tree is None:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not _inherits_from(node, "Phase"):
                    continue

                llm_count = _count_attr_calls(node, "self", "llm", "complete")
                tracer_count = _count_attr_calls(node, "self", "tracer", "record_llm_call")
                helper_count = _count_method_calls(node, "self", "record_llm_call")
                trace_count = tracer_count + helper_count

                self._result.checks_run += 1
                if llm_count > 0 and trace_count < llm_count:
                    self._result.violations.append(
                        Violation(
                            file=str(py_file.relative_to(self.engine_path.parent)),
                            line=node.lineno,
                            principle="P8: Provenance is automatic",
                            code="GP008",
                            message=(
                                f"Class '{node.name}' has {llm_count} llm.complete() "
                                f"call(s) but only {trace_count} record_llm_call() — "
                                f"every LLM call must record provenance"
                            ),
                        )
                    )

    def _check_tool_execution_tracing(self) -> None:
        """P1: ToolExecutor.execute() must call self.tracer.record_action()."""
        executor_path = self.engine_path / "tools" / "executor.py"
        if not executor_path.is_file():
            return

        tree = self._parse_file(executor_path)
        if tree is None:
            return

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != "ToolExecutor":
                continue

            self._result.checks_run += 1
            execute_method = _find_method(node, "execute")
            if execute_method is None:
                self._result.violations.append(
                    Violation(
                        file=str(executor_path.relative_to(self.engine_path.parent)),
                        line=node.lineno,
                        principle="P1: Every action is logged",
                        code="GP001",
                        message="ToolExecutor has no execute() method",
                    )
                )
                continue

            if not _contains_attr_call(execute_method, "self", "tracer"):
                self._result.violations.append(
                    Violation(
                        file=str(executor_path.relative_to(self.engine_path.parent)),
                        line=execute_method.lineno,
                        principle="P1: Every action is logged",
                        code="GP001",
                        message=(
                            "ToolExecutor.execute() does not call self.tracer — "
                            "every tool execution must be traced"
                        ),
                    )
                )

    def _check_iteration_bounds(self) -> None:
        """P5: RalphLoop.run() must check max_iterations and time_budget."""
        loop_path = self.engine_path / "loop.py"
        if not loop_path.is_file():
            return

        tree = self._parse_file(loop_path)
        if tree is None:
            return

        source = loop_path.read_text()

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != "RalphLoop":
                continue

            run_method = _find_method(node, "run")
            if run_method is None:
                continue

            run_source = ast.get_source_segment(source, run_method) or ""

            self._result.checks_run += 1
            if "max_iterations" not in run_source:
                self._result.violations.append(
                    Violation(
                        file=str(loop_path.relative_to(self.engine_path.parent)),
                        line=run_method.lineno,
                        principle="P5: Iterations are bounded",
                        code="GP005",
                        message="RalphLoop.run() does not check max_iterations",
                    )
                )

            self._result.checks_run += 1
            if "time_budget" not in run_source:
                self._result.violations.append(
                    Violation(
                        file=str(loop_path.relative_to(self.engine_path.parent)),
                        line=run_method.lineno,
                        principle="P5: Iterations are bounded",
                        code="GP005",
                        message="RalphLoop.run() does not check time_budget",
                    )
                )

    def _check_report_publishing(self) -> None:
        """P9: RalphLoop._write_outputs() must trigger report generation."""
        loop_path = self.engine_path / "loop.py"
        if not loop_path.is_file():
            return

        tree = self._parse_file(loop_path)
        if tree is None:
            return

        source = loop_path.read_text()

        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != "RalphLoop":
                continue

            write_method = _find_method(node, "_write_outputs")
            if write_method is None:
                self._result.checks_run += 1
                self._result.violations.append(
                    Violation(
                        file=str(loop_path.relative_to(self.engine_path.parent)),
                        line=node.lineno,
                        principle="P9: Demos are a byproduct",
                        code="GP009",
                        message="RalphLoop has no _write_outputs() method",
                    )
                )
                continue

            method_source = ast.get_source_segment(source, write_method) or ""
            self._result.checks_run += 1
            if "report" not in method_source.lower() and "publish" not in method_source.lower():
                self._result.violations.append(
                    Violation(
                        file=str(loop_path.relative_to(self.engine_path.parent)),
                        line=write_method.lineno,
                        principle="P9: Demos are a byproduct",
                        code="GP009",
                        message=(
                            "RalphLoop._write_outputs() does not reference "
                            "report publishing — demos must be a byproduct"
                        ),
                    )
                )

    def _check_configuration_usage(self) -> None:
        """P10: Phase implementations must reference self.config (not hardcode values)."""
        phases_dir = self.engine_path / "phases"
        if not phases_dir.is_dir():
            return

        for py_file in sorted(phases_dir.glob("*.py")):
            if py_file.name in ("__init__.py", "base.py", "prompt_loader.py"):
                continue

            tree = self._parse_file(py_file)
            if tree is None:
                continue

            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                if not _inherits_from(node, "Phase"):
                    continue

                self._result.checks_run += 1
                if not _class_references_attr(node, "self", "config"):
                    self._result.violations.append(
                        Violation(
                            file=str(py_file.relative_to(self.engine_path.parent)),
                            line=node.lineno,
                            principle="P10: Configuration is declarative",
                            code="GP010",
                            message=(
                                f"Class '{node.name}' never references self.config — "
                                f"behavior should be driven by configuration, not hardcoded"
                            ),
                        )
                    )

    def _parse_file(self, path: Path) -> ast.Module | None:
        """Parse a Python file into an AST, tracking it in files_scanned."""
        try:
            source = path.read_text()
            self._result.files_scanned += 1
            return ast.parse(source, filename=str(path))
        except (SyntaxError, OSError):
            return None


# ---------------------------------------------------------------------------
# AST helper functions
# ---------------------------------------------------------------------------


def _inherits_from(class_node: ast.ClassDef, base_name: str) -> bool:
    """Check if a class definition inherits from a named base (direct only)."""
    for base in class_node.bases:
        if isinstance(base, ast.Name) and base.id == base_name:
            return True
        if isinstance(base, ast.Attribute) and base.attr == base_name:
            return True
    return False


def _contains_attr_call(
    node: ast.AST,
    obj_name: str,
    attr_name: str,
) -> bool:
    """Check if a node's subtree contains a call like obj_name.attr_name.*(...)."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue
        value = func.value
        if _is_dotted_access(value, obj_name, attr_name):
            return True
        if isinstance(value, ast.Name) and value.id == obj_name and func.attr == attr_name:
            return True
    return False


def _class_contains_attr_call(
    class_node: ast.ClassDef,
    obj_name: str,
    attr_name: str,
    method_name: str | None = None,
) -> bool:
    """Check if any method in a class calls obj_name.attr_name[.method_name](...)."""
    for child in ast.walk(class_node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue

        if method_name is not None:
            value = func.value
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Attribute)
                and isinstance(value.value.value, ast.Name)
                and value.value.value.id == obj_name
                and value.value.attr == attr_name
                and value.attr == method_name
            ):
                return True
            if (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id == obj_name
                and value.attr == attr_name
                and func.attr == method_name
            ):
                return True
        else:
            value = func.value
            if _is_dotted_access(value, obj_name, attr_name):
                return True
            if isinstance(value, ast.Name) and value.id == obj_name and func.attr == attr_name:
                return True
    return False


def _class_contains_call(class_node: ast.ClassDef, func_name: str) -> bool:
    """Check if any method in a class calls a function containing func_name."""
    for child in ast.walk(class_node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute) and func_name in func.attr:
            return True
        if isinstance(func, ast.Name) and func_name in func.id:
            return True
    return False


def _class_references_attr(
    class_node: ast.ClassDef,
    obj_name: str,
    attr_name: str,
) -> bool:
    """Check if any code in a class references obj_name.attr_name (access or call)."""
    for child in ast.walk(class_node):
        if not isinstance(child, ast.Attribute):
            continue
        value = child.value
        if isinstance(value, ast.Name) and value.id == obj_name and child.attr == attr_name:
            return True
        if _is_dotted_access(child, obj_name, attr_name):
            return True
    return False


def _is_dotted_access(node: ast.AST, obj_name: str, attr_name: str) -> bool:
    """Check if node represents obj_name.attr_name (nested Attribute access)."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == obj_name
        and node.attr == attr_name
    )


def _count_attr_calls(
    class_node: ast.ClassDef,
    obj_name: str,
    attr_name: str,
    method_name: str,
) -> int:
    """Count how many times obj_name.attr_name.method_name(...) is called in a class."""
    count = 0
    for child in ast.walk(class_node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != method_name:
            continue
        value = func.value
        if _is_dotted_access(value, obj_name, attr_name):
            count += 1
    return count


def _count_method_calls(
    class_node: ast.ClassDef,
    obj_name: str,
    method_name: str,
) -> int:
    """Count how many times obj_name.method_name(...) is called in a class.

    Unlike ``_count_attr_calls`` which matches ``obj.attr.method()``, this
    matches a single-level attribute call like ``self.record_llm_call()``.
    """
    count = 0
    for child in ast.walk(class_node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != method_name:
            continue
        value = func.value
        if isinstance(value, ast.Name) and value.id == obj_name:
            count += 1
    return count


def _find_method(
    class_node: ast.ClassDef,
    method_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find a method by name in a class definition."""
    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
            return item
    return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run golden principles checks. Returns 0 on success, 1 on violations."""
    args = argv if argv is not None else sys.argv[1:]
    engine_path = Path(args[0]) if args else Path("engine")

    if not engine_path.is_dir():
        print(f"Error: engine path not found: {engine_path}", file=sys.stderr)
        return 1

    checker = GoldenPrinciplesChecker(engine_path)
    result = checker.check_all()

    for v in result.violations:
        print(str(v), file=sys.stderr)

    print(result.summary())
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
