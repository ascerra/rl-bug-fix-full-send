"""Tests for engine.tools.test_runner — repo language detection and test/lint command selection.

Covers:
- RepoStack dataclass and serialization
- _detect_language() with manifest files, extension counts, and edge cases
- detect_repo_stack() with config overrides
- Integration with phases: implement, validate, triage using detected commands
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.integrations.llm import MockProvider
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.phases.base import PhaseResult
from engine.tools.executor import ToolExecutor
from engine.tools.test_runner import (
    FALLBACK_LINT_COMMAND,
    FALLBACK_TEST_COMMAND,
    LINT_COMMANDS,
    TEST_COMMANDS,
    RepoStack,
    _detect_language,
    detect_repo_stack,
)

# ------------------------------------------------------------------
# RepoStack dataclass
# ------------------------------------------------------------------


class TestRepoStack:
    def test_to_dict(self):
        stack = RepoStack(
            language="go",
            test_command="go test ./...",
            lint_command="golangci-lint run ./...",
            detected_from="go.mod",
            confidence=0.95,
        )
        d = stack.to_dict()
        assert d["language"] == "go"
        assert d["test_command"] == "go test ./..."
        assert d["lint_command"] == "golangci-lint run ./..."
        assert d["detected_from"] == "go.mod"
        assert d["confidence"] == 0.95


# ------------------------------------------------------------------
# _detect_language — manifest file detection
# ------------------------------------------------------------------


class TestDetectLanguageManifest:
    def test_go_mod(self):
        lang, source, conf = _detect_language(["./cmd/main.go", "./go.mod", "./go.sum"])
        assert lang == "go"
        assert source == "go.mod"
        assert conf == 0.95

    def test_go_sum_without_go_mod(self):
        lang, source, _conf = _detect_language(["./cmd/main.go", "./go.sum"])
        assert lang == "go"
        assert source == "go.sum"

    def test_package_json(self):
        lang, source, _conf = _detect_language(
            ["./src/index.ts", "./package.json", "./tsconfig.json"]
        )
        assert lang == "node"
        assert source == "package.json"
        assert _conf == 0.95

    def test_cargo_toml(self):
        lang, source, conf = _detect_language(["./src/main.rs", "./Cargo.toml"])
        assert lang == "rust"
        assert source == "Cargo.toml"
        assert conf == 0.95

    def test_pyproject_toml(self):
        lang, source, conf = _detect_language(["./engine/__init__.py", "./pyproject.toml"])
        assert lang == "python"
        assert source == "pyproject.toml"
        assert conf == 0.95

    def test_setup_py(self):
        lang, source, conf = _detect_language(["./mypackage/main.py", "./setup.py"])
        assert lang == "python"
        assert source == "setup.py"
        assert conf == 0.95

    def test_requirements_txt(self):
        lang, source, conf = _detect_language(["./app.py", "./requirements.txt"])
        assert lang == "python"
        assert source == "requirements.txt"
        assert conf == 0.95

    def test_pipfile(self):
        lang, source, _conf = _detect_language(["./app.py", "./Pipfile"])
        assert lang == "python"
        assert source == "Pipfile"

    def test_manifest_in_subdirectory(self):
        lang, source, _conf = _detect_language(["./cmd/server/main.go", "./vendor/go.mod"])
        assert lang == "go"
        assert source == "go.mod"

    def test_first_manifest_wins(self):
        files = ["./main.go", "./go.mod", "./package.json"]
        lang, source, _ = _detect_language(files)
        assert lang == "go"
        assert source == "go.mod"


# ------------------------------------------------------------------
# _detect_language — extension frequency fallback
# ------------------------------------------------------------------


class TestDetectLanguageExtensions:
    def test_mostly_go_files(self):
        files = [
            "./cmd/main.go",
            "./pkg/handler.go",
            "./pkg/utils.go",
            "./internal/server.go",
            "./config.yaml",
        ]
        lang, source, conf = _detect_language(files)
        assert lang == "go"
        assert source == "file_extensions"
        assert 0.5 <= conf <= 0.9

    def test_mostly_python_files(self):
        files = [
            "./app.py",
            "./models.py",
            "./views.py",
            "./tests/test_app.py",
            "./config.yaml",
        ]
        lang, source, conf = _detect_language(files)
        assert lang == "python"
        assert source == "file_extensions"
        assert conf > 0.5

    def test_mostly_node_files(self):
        files = [
            "./src/index.ts",
            "./src/App.tsx",
            "./src/utils.ts",
            "./README.md",
        ]
        lang, source, _conf = _detect_language(files)
        assert lang == "node"
        assert source == "file_extensions"

    def test_mostly_rust_files(self):
        files = [
            "./src/main.rs",
            "./src/lib.rs",
            "./src/utils.rs",
        ]
        lang, source, _conf = _detect_language(files)
        assert lang == "rust"
        assert source == "file_extensions"

    def test_mixed_extensions_picks_dominant(self):
        files = [
            "./go/main.go",
            "./go/handler.go",
            "./go/utils.go",
            "./scripts/helper.py",
        ]
        lang, source, _conf = _detect_language(files)
        assert lang == "go"
        assert source == "file_extensions"

    def test_confidence_higher_when_dominant(self):
        all_go = [f"./pkg/file{i}.go" for i in range(10)]
        _, _, conf_pure = _detect_language(all_go)

        mixed = [f"./pkg/file{i}.go" for i in range(5)] + [
            f"./scripts/file{i}.py" for i in range(5)
        ]
        _, _, conf_mixed = _detect_language(mixed)

        assert conf_pure > conf_mixed


# ------------------------------------------------------------------
# _detect_language — edge cases
# ------------------------------------------------------------------


class TestDetectLanguageEdgeCases:
    def test_empty_file_list(self):
        lang, source, conf = _detect_language([])
        assert lang == "unknown"
        assert source == "none"
        assert conf == 0.0

    def test_only_yaml_and_markdown(self):
        lang, _source, conf = _detect_language(["./config.yaml", "./README.md"])
        assert lang == "unknown"
        assert conf == 0.0

    def test_makefile_only(self):
        lang, source, conf = _detect_language(["./Makefile", "./config.yaml"])
        assert lang == "unknown"
        assert source == "makefile_only"
        assert conf == 0.1

    def test_files_with_no_extension(self):
        lang, _source, _conf = _detect_language(["./Dockerfile", "./LICENSE"])
        assert lang == "unknown"

    def test_dotfiles_ignored(self):
        lang, _source, _conf = _detect_language(["./.gitignore", "./.editorconfig"])
        assert lang == "unknown"


# ------------------------------------------------------------------
# detect_repo_stack — full API
# ------------------------------------------------------------------


class TestDetectRepoStack:
    def test_go_repo(self):
        listing = "./cmd/main.go\n./go.mod\n./go.sum\n./pkg/handler.go\n"
        stack = detect_repo_stack(listing)
        assert stack.language == "go"
        assert stack.test_command == TEST_COMMANDS["go"]
        assert stack.lint_command == LINT_COMMANDS["go"]
        assert stack.confidence == 0.95
        assert stack.detected_from == "go.mod"

    def test_python_repo(self):
        listing = "./app.py\n./pyproject.toml\n./tests/test_app.py\n"
        stack = detect_repo_stack(listing)
        assert stack.language == "python"
        assert stack.test_command == TEST_COMMANDS["python"]
        assert stack.lint_command == LINT_COMMANDS["python"]

    def test_node_repo(self):
        listing = "./src/index.ts\n./package.json\n"
        stack = detect_repo_stack(listing)
        assert stack.language == "node"
        assert stack.test_command == TEST_COMMANDS["node"]
        assert stack.lint_command == LINT_COMMANDS["node"]

    def test_rust_repo(self):
        listing = "./src/main.rs\n./Cargo.toml\n"
        stack = detect_repo_stack(listing)
        assert stack.language == "rust"
        assert stack.test_command == TEST_COMMANDS["rust"]
        assert stack.lint_command == LINT_COMMANDS["rust"]

    def test_unknown_repo(self):
        listing = "./README.md\n./Dockerfile\n"
        stack = detect_repo_stack(listing)
        assert stack.language == "unknown"
        assert stack.test_command == FALLBACK_TEST_COMMAND
        assert stack.lint_command == FALLBACK_LINT_COMMAND

    def test_empty_listing(self):
        stack = detect_repo_stack("")
        assert stack.language == "unknown"
        assert stack.test_command == FALLBACK_TEST_COMMAND
        assert stack.lint_command == FALLBACK_LINT_COMMAND

    def test_config_test_command_override(self):
        listing = "./main.go\n./go.mod\n"
        stack = detect_repo_stack(listing, test_command_override="make test 2>&1")
        assert stack.language == "go"
        assert stack.test_command == "make test 2>&1"
        assert stack.lint_command == LINT_COMMANDS["go"]
        assert stack.confidence >= 1.0
        assert "config_override" in stack.detected_from

    def test_config_lint_command_override(self):
        listing = "./main.go\n./go.mod\n"
        stack = detect_repo_stack(listing, lint_command_override="make lint 2>&1")
        assert stack.language == "go"
        assert stack.test_command == TEST_COMMANDS["go"]
        assert stack.lint_command == "make lint 2>&1"

    def test_config_both_overrides(self):
        listing = "./main.go\n./go.mod\n"
        stack = detect_repo_stack(
            listing,
            test_command_override="make test",
            lint_command_override="make lint",
        )
        assert stack.test_command == "make test"
        assert stack.lint_command == "make lint"

    def test_whitespace_handling(self):
        listing = "  ./main.go  \n  ./go.mod  \n\n  \n"
        stack = detect_repo_stack(listing)
        assert stack.language == "go"

    def test_to_dict_roundtrip(self):
        listing = "./main.go\n./go.mod\n"
        stack = detect_repo_stack(listing)
        d = stack.to_dict()
        assert isinstance(d, dict)
        assert d["language"] == "go"


# ------------------------------------------------------------------
# Config integration
# ------------------------------------------------------------------


class TestConfigIntegration:
    def test_implement_config_has_test_command(self):
        cfg = EngineConfig()
        assert cfg.phases.implement.test_command == ""
        assert cfg.phases.implement.lint_command == ""

    def test_validate_config_has_test_command(self):
        cfg = EngineConfig()
        assert cfg.phases.validate.test_command == ""
        assert cfg.phases.validate.lint_command == ""

    def test_implement_config_via_yaml(self):
        from engine.config import load_config

        cfg = load_config(
            overrides={
                "phases": {
                    "implement": {
                        "test_command": "make test",
                        "lint_command": "make lint",
                    }
                }
            }
        )
        assert cfg.phases.implement.test_command == "make test"
        assert cfg.phases.implement.lint_command == "make lint"

    def test_validate_config_via_yaml(self):
        from engine.config import load_config

        cfg = load_config(
            overrides={
                "phases": {
                    "validate": {
                        "test_command": "make test",
                        "lint_command": "make lint",
                    }
                }
            }
        )
        assert cfg.phases.validate.test_command == "make test"
        assert cfg.phases.validate.lint_command == "make lint"


# ------------------------------------------------------------------
# Phase integration — verify phases use detected commands, not chained fallback
# ------------------------------------------------------------------


def _fix_response() -> str:
    return json.dumps(
        {
            "root_cause": "Nil pointer dereference",
            "fix_description": "Added nil check",
            "files_changed": ["pkg/controller/reconciler.go"],
            "file_changes": [
                {
                    "path": "pkg/controller/reconciler.go",
                    "content": "package controller\n\nfunc Reconcile() error {\n\treturn nil\n}\n",
                }
            ],
            "test_added": "",
            "tests_passing": True,
            "linters_passing": True,
            "confidence": 0.9,
            "diff_summary": "nil check",
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
            "affected_components": ["pkg/controller/reconciler.go"],
            "reasoning": "Nil pointer",
        },
        artifacts={
            "triage_report": {
                "classification": "bug",
                "severity": "high",
                "affected_components": ["pkg/controller/reconciler.go"],
                "reasoning": "Nil pointer",
            },
        },
    )


def _impl_result() -> PhaseResult:
    return PhaseResult(
        phase="implement",
        success=True,
        should_continue=True,
        next_phase="review",
        findings={
            "root_cause": "nil pointer",
            "fix_description": "Added nil check",
            "confidence": 0.9,
        },
        artifacts={
            "diff": "diff --git a/pkg/controller/reconciler.go",
            "files_changed": ["pkg/controller/reconciler.go"],
        },
    )


def _review_result() -> PhaseResult:
    return PhaseResult(
        phase="review",
        success=True,
        should_continue=True,
        next_phase="validate",
        findings={"verdict": "approve", "summary": "Looks good"},
        artifacts={"review_report": {"verdict": "approve", "summary": "Looks good"}},
    )


def _validate_response() -> str:
    return json.dumps(
        {
            "tests_passing": True,
            "test_summary": "All pass",
            "linters_passing": True,
            "lint_issues": [],
            "diff_is_minimal": True,
            "unnecessary_changes": [],
            "pr_description": "Fix nil pointer",
            "ready_to_submit": True,
            "blocking_issues": [],
            "confidence": 0.95,
        }
    )


def _triage_response() -> str:
    return json.dumps(
        {
            "classification": "bug",
            "confidence": 0.9,
            "severity": "high",
            "affected_components": ["pkg/controller/reconciler.go"],
            "reproduction": {"existing_tests": [], "can_reproduce": False},
            "injection_detected": False,
            "reasoning": "Clear nil pointer panic",
        }
    )


def _make_go_repo(tmp_path: Path) -> Path:
    """Create a minimal Go-shaped repo for detection tests."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/myproject\n\ngo 1.21\n")
    (repo / "cmd").mkdir()
    (repo / "cmd" / "main.go").write_text("package main\n")
    (repo / "pkg" / "controller").mkdir(parents=True)
    (repo / "pkg" / "controller" / "reconciler.go").write_text(
        "package controller\n\nfunc Reconcile() error {\n\treturn nil\n}\n"
    )
    return repo


class TestImplementPhaseUsesDetectedRunner:
    @pytest.mark.asyncio
    async def test_run_tests_uses_go_command_for_go_repo(self, tmp_path):
        from engine.phases.implement import ImplementPhase

        repo = _make_go_repo(tmp_path)
        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)

        phase = ImplementPhase(
            llm=MockProvider(responses=[_fix_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic",
                "body": "Panic when reconciling.",
            },
            config=EngineConfig(),
            tool_executor=executor,
            prior_phase_results=[_triage_result()],
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.language == "go"
        assert "go test" in phase._detected_stack.test_command
        assert "golangci-lint" in phase._detected_stack.lint_command

    @pytest.mark.asyncio
    async def test_run_tests_uses_config_override(self, tmp_path):
        from engine.phases.implement import ImplementPhase

        repo = _make_go_repo(tmp_path)
        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)
        cfg = EngineConfig()
        cfg.phases.implement.test_command = "make test 2>&1"
        cfg.phases.implement.lint_command = "make lint 2>&1"

        phase = ImplementPhase(
            llm=MockProvider(responses=[_fix_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic",
                "body": "Panic when reconciling.",
            },
            config=cfg,
            tool_executor=executor,
            prior_phase_results=[_triage_result()],
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.test_command == "make test 2>&1"
        assert phase._detected_stack.lint_command == "make lint 2>&1"

    @pytest.mark.asyncio
    async def test_no_generic_chained_command(self, tmp_path):
        """Verify the old chained fallback is gone from the test command."""
        from engine.phases.implement import ImplementPhase

        repo = _make_go_repo(tmp_path)
        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)

        phase = ImplementPhase(
            llm=MockProvider(responses=[_fix_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic",
                "body": "Panic when reconciling.",
            },
            config=EngineConfig(),
            tool_executor=executor,
            prior_phase_results=[_triage_result()],
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert "||" not in phase._detected_stack.test_command
        assert "||" not in phase._detected_stack.lint_command


class TestValidatePhaseUsesDetectedRunner:
    @pytest.mark.asyncio
    async def test_observe_detects_go_repo(self, tmp_path):
        from engine.phases.validate import ValidatePhase

        repo = _make_go_repo(tmp_path)
        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)

        phase = ValidatePhase(
            llm=MockProvider(responses=[_validate_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic",
                "body": "Panic when reconciling.",
            },
            config=EngineConfig(),
            tool_executor=executor,
            prior_phase_results=[_triage_result(), _impl_result(), _review_result()],
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.language == "go"
        assert "go test" in phase._detected_stack.test_command

    @pytest.mark.asyncio
    async def test_validate_uses_config_override(self, tmp_path):
        from engine.phases.validate import ValidatePhase

        repo = _make_go_repo(tmp_path)
        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)
        cfg = EngineConfig()
        cfg.phases.validate.test_command = "make test 2>&1"

        phase = ValidatePhase(
            llm=MockProvider(responses=[_validate_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic",
                "body": "Panic when reconciling.",
            },
            config=cfg,
            tool_executor=executor,
            prior_phase_results=[_triage_result(), _impl_result(), _review_result()],
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.test_command == "make test 2>&1"


class TestTriagePhaseUsesDetectedRunner:
    @pytest.mark.asyncio
    async def test_observe_detects_go_repo(self, tmp_path):
        from engine.phases.triage import TriagePhase

        repo = _make_go_repo(tmp_path)
        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)

        phase = TriagePhase(
            llm=MockProvider(responses=[_triage_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/42",
                "title": "nil pointer panic",
                "body": "Panic when reconciling.",
            },
            config=EngineConfig(),
            tool_executor=executor,
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.language == "go"
        assert "go test" in phase._detected_stack.test_command


class TestDetectPythonRepo:
    @pytest.mark.asyncio
    async def test_python_repo_detects_correctly(self, tmp_path):
        from engine.phases.implement import ImplementPhase

        repo = tmp_path / "pyrepo"
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\nname='myapp'\n")
        (repo / "myapp").mkdir()
        (repo / "myapp" / "__init__.py").write_text("")
        (repo / "myapp" / "main.py").write_text("def main(): pass\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "test_main.py").write_text("def test_it(): pass\n")

        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)

        phase = ImplementPhase(
            llm=MockProvider(responses=[_fix_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/1",
                "title": "import error",
                "body": "ImportError on startup.",
            },
            config=EngineConfig(),
            tool_executor=executor,
            prior_phase_results=[_triage_result()],
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.language == "python"
        assert "pytest" in phase._detected_stack.test_command
        assert "ruff" in phase._detected_stack.lint_command


class TestDetectNodeRepo:
    @pytest.mark.asyncio
    async def test_node_repo_detects_correctly(self, tmp_path):
        from engine.phases.implement import ImplementPhase

        repo = tmp_path / "noderepo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "myapp"}\n')
        (repo / "src").mkdir()
        (repo / "src" / "index.ts").write_text("export const x = 1;\n")

        tracer = Tracer()
        logger = StructuredLogger()
        metrics = LoopMetrics()
        executor = ToolExecutor(repo, logger, tracer, metrics)

        phase = ImplementPhase(
            llm=MockProvider(responses=[_fix_response()]),
            logger=logger,
            tracer=tracer,
            repo_path=str(repo),
            issue_data={
                "url": "https://github.com/test/repo/issues/1",
                "title": "build error",
                "body": "Build fails.",
            },
            config=EngineConfig(),
            tool_executor=executor,
            prior_phase_results=[_triage_result()],
            metrics=metrics,
        )

        await phase.observe()
        assert phase._detected_stack is not None
        assert phase._detected_stack.language == "node"
        assert "npm test" in phase._detected_stack.test_command
        assert "eslint" in phase._detected_stack.lint_command


# ------------------------------------------------------------------
# Negative: verify old chained pattern is NOT used
# ------------------------------------------------------------------


class TestNoChainedFallback:
    """Ensure the old ``pytest || go test || npm test`` pattern is gone from phases."""

    def test_implement_source_has_no_chained_test_runner(self):
        import inspect

        from engine.phases.implement import ImplementPhase

        source = inspect.getsource(ImplementPhase._run_tests)
        assert "python -m pytest" not in source
        assert "go test" not in source
        assert "npm test" not in source

    def test_implement_source_has_no_chained_linter(self):
        import inspect

        from engine.phases.implement import ImplementPhase

        source = inspect.getsource(ImplementPhase._run_linters)
        assert "ruff check" not in source
        assert "golangci-lint" not in source
        assert "npx eslint" not in source

    def test_validate_source_has_no_chained_test_runner(self):
        import inspect

        from engine.phases.validate import ValidatePhase

        source = inspect.getsource(ValidatePhase._run_full_tests)
        assert "python -m pytest" not in source
        assert "go test" not in source

    def test_validate_source_has_no_chained_linter(self):
        import inspect

        from engine.phases.validate import ValidatePhase

        source = inspect.getsource(ValidatePhase._run_linters)
        assert "ruff check" not in source
        assert "golangci-lint" not in source

    def test_triage_source_has_no_chained_test_runner(self):
        import inspect

        from engine.phases.triage import TriagePhase

        source = inspect.getsource(TriagePhase._attempt_reproduction)
        assert "python -m pytest" not in source
        assert "go test" not in source
