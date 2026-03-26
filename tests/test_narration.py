"""Tests for live narration and progress.md writer (D8 — 7.8).

Covers:
- StructuredLogger.narrate() writes to stderr with >>> prefix
- StructuredLogger.narrate() stores narrations in _narrations list
- StructuredLogger.narrate() appends to progress.md when configured
- StructuredLogger.write_progress_heading() writes markdown headings
- Redaction in narrate()
- StructuredLogger.get_narrations() returns narration entries
- Loop narrates at phase boundaries (start, end, escalation, transitions)
- Each phase narrates at OODA steps (triage, implement, review, validate)
- progress.md has expected structure from a full loop run
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from engine.config import EngineConfig
from engine.integrations.llm import LLMResponse, MockProvider
from engine.loop import RalphLoop
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.phases.base import Phase, PhaseResult
from engine.phases.implement import ImplementPhase
from engine.phases.review import ReviewPhase
from engine.phases.triage import TriagePhase
from engine.phases.validate import ValidatePhase
from engine.secrets import SecretRedactor

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_logger(
    tmp_path: Path,
    *,
    with_progress: bool = True,
    redactor: SecretRedactor | None = None,
) -> StructuredLogger:
    progress_path = tmp_path / "progress.md" if with_progress else None
    return StructuredLogger(
        execution_id="test-narration",
        output_path=tmp_path / "log.json",
        progress_path=progress_path,
        redactor=redactor,
    )


def _triage_json(**overrides: object) -> str:
    base = {
        "classification": "bug",
        "confidence": 0.85,
        "severity": "high",
        "affected_components": ["pkg/controller/reconciler.go"],
        "reproduction": {"existing_tests": [], "can_reproduce": False},
        "injection_detected": False,
        "recommendation": "implement",
        "reasoning": "Nil pointer dereference in reconciler",
    }
    base.update(overrides)
    return json.dumps(base)


def _impl_json(**overrides: object) -> str:
    base = {
        "root_cause": "Nil pointer dereference",
        "fix_description": "Add nil check before calling Process()",
        "file_changes": [
            {"path": "pkg/controller/reconciler.go", "content": "package controller\n"}
        ],
        "tests_passing": True,
        "linters_passing": True,
        "confidence": 0.9,
    }
    base.update(overrides)
    return json.dumps(base)


def _review_json(**overrides: object) -> str:
    base = {
        "verdict": "approve",
        "findings": [],
        "scope_assessment": "bug_fix",
        "injection_detected": False,
        "confidence": 0.9,
        "summary": "Fix looks correct. Nil check is appropriate.",
    }
    base.update(overrides)
    return json.dumps(base)


def _validate_json(**overrides: object) -> str:
    base = {
        "tests_passing": True,
        "test_summary": "All tests pass",
        "linters_passing": True,
        "lint_issues": [],
        "diff_is_minimal": True,
        "unnecessary_changes": [],
        "pr_description": "Fixes nil pointer dereference in reconciler",
        "ready_to_submit": True,
        "blocking_issues": [],
        "confidence": 0.95,
    }
    base.update(overrides)
    return json.dumps(base)


def _make_mock_provider(*responses: str) -> MockProvider:
    provider = MockProvider()
    provider._responses = list(responses)
    provider._call_count = 0

    async def _rotating_complete(**kwargs):
        idx = provider._call_count % len(provider._responses)
        provider._call_count += 1
        return LLMResponse(
            content=provider._responses[idx],
            model="mock-model",
            provider="mock",
            tokens_in=100,
            tokens_out=50,
            latency_ms=10.0,
        )

    provider.complete = _rotating_complete
    return provider


def _make_phase(
    cls: type[Phase],
    tmp_path: Path,
    provider: MockProvider | None = None,
    prior_results: list[PhaseResult] | None = None,
    issue_data: dict | None = None,
) -> Phase:
    logger = _make_logger(tmp_path)
    logger.set_phase(cls.name)
    logger.set_iteration(1)
    return cls(
        llm=provider or MockProvider(),
        logger=logger,
        tracer=Tracer(),
        repo_path=str(tmp_path),
        issue_data=issue_data or {"url": "https://github.com/org/repo/issues/1"},
        prior_phase_results=prior_results,
        config=EngineConfig(),
        metrics=LoopMetrics(),
    )


# ===========================================================================
# 1. StructuredLogger.narrate() core behavior
# ===========================================================================


class TestNarrateCore:
    """Test the narrate() method on StructuredLogger."""

    def test_narrate_writes_to_stderr(self, tmp_path: Path, capsys):
        logger = _make_logger(tmp_path, with_progress=False)
        logger.set_phase("triage")
        logger.narrate("Classified as bug.")

        captured = capsys.readouterr()
        assert ">>> [TRIAGE] Classified as bug." in captured.err

    def test_narrate_stores_in_narrations_list(self, tmp_path: Path):
        logger = _make_logger(tmp_path, with_progress=False)
        logger.set_phase("implement")
        logger.set_iteration(3)
        logger.narrate("Writing 2 files.")

        narrations = logger.get_narrations()
        assert len(narrations) == 1
        assert narrations[0]["phase"] == "implement"
        assert narrations[0]["iteration"] == 3
        assert narrations[0]["message"] == "Writing 2 files."
        assert "timestamp" in narrations[0]

    def test_narrate_appends_to_progress_md(self, tmp_path: Path):
        logger = _make_logger(tmp_path)
        logger.narrate("First message.")
        logger.narrate("Second message.")

        progress = (tmp_path / "progress.md").read_text()
        assert "- First message.\n" in progress
        assert "- Second message.\n" in progress

    def test_narrate_no_progress_without_path(self, tmp_path: Path):
        logger = _make_logger(tmp_path, with_progress=False)
        logger.narrate("No file expected.")

        assert not (tmp_path / "progress.md").exists()

    def test_narrate_creates_parent_dirs(self, tmp_path: Path):
        nested = tmp_path / "deep" / "nested"
        logger = StructuredLogger(
            progress_path=nested / "progress.md",
        )
        logger.narrate("Should create dirs.")

        assert (nested / "progress.md").exists()
        assert "- Should create dirs." in (nested / "progress.md").read_text()

    def test_narrate_no_phase_prefix_for_init(self, tmp_path: Path, capsys):
        logger = _make_logger(tmp_path, with_progress=False)
        logger.narrate("Starting up.")

        captured = capsys.readouterr()
        assert ">>> Starting up." in captured.err
        assert "[INIT]" not in captured.err

    def test_narrate_multiple_phases(self, tmp_path: Path):
        logger = _make_logger(tmp_path)
        logger.set_phase("triage")
        logger.narrate("Triage step.")
        logger.set_phase("implement")
        logger.narrate("Implement step.")

        narrations = logger.get_narrations()
        assert len(narrations) == 2
        assert narrations[0]["phase"] == "triage"
        assert narrations[1]["phase"] == "implement"

    def test_get_narrations_returns_copy(self, tmp_path: Path):
        logger = _make_logger(tmp_path, with_progress=False)
        logger.narrate("Test.")
        narrations = logger.get_narrations()
        narrations.clear()
        assert len(logger.get_narrations()) == 1


class TestNarrateRedaction:
    """Test that secrets are redacted in narrate() output."""

    def test_redacts_secrets_in_stderr(self, tmp_path: Path, capsys):
        redactor = SecretRedactor({"GEMINI_API_KEY": "sk-secret-12345"})
        logger = _make_logger(tmp_path, with_progress=False, redactor=redactor)
        logger.narrate("Using key sk-secret-12345 for LLM.")

        captured = capsys.readouterr()
        assert "sk-secret-12345" not in captured.err
        assert "***" in captured.err

    def test_redacts_secrets_in_narrations_list(self, tmp_path: Path):
        redactor = SecretRedactor({"GH_PAT": "ghp_abcdef12345678"})
        logger = _make_logger(tmp_path, with_progress=False, redactor=redactor)
        logger.narrate("Token is ghp_abcdef12345678.")

        narrations = logger.get_narrations()
        assert "ghp_abcdef12345678" not in narrations[0]["message"]

    def test_redacts_secrets_in_progress_md(self, tmp_path: Path):
        redactor = SecretRedactor({"KEY": "super-secret-value"})
        logger = _make_logger(tmp_path, redactor=redactor)
        logger.narrate("Key is super-secret-value.")

        progress = (tmp_path / "progress.md").read_text()
        assert "super-secret-value" not in progress


class TestWriteProgressHeading:
    """Test the write_progress_heading() method."""

    def test_writes_heading(self, tmp_path: Path):
        logger = _make_logger(tmp_path)
        logger.write_progress_heading("## Iteration 1 — triage")

        progress = (tmp_path / "progress.md").read_text()
        assert "## Iteration 1 — triage" in progress

    def test_heading_then_narration(self, tmp_path: Path):
        logger = _make_logger(tmp_path)
        logger.write_progress_heading("## Phase Start")
        logger.narrate("Doing something.")

        progress = (tmp_path / "progress.md").read_text()
        assert progress.index("## Phase Start") < progress.index("- Doing something.")

    def test_no_file_without_progress_path(self, tmp_path: Path):
        logger = _make_logger(tmp_path, with_progress=False)
        logger.write_progress_heading("## Heading")
        assert not (tmp_path / "progress.md").exists()


# ===========================================================================
# 2. Loop narration at phase boundaries
# ===========================================================================


class TestLoopNarration:
    """Test that the loop emits narration at phase boundaries."""

    def _make_loop(
        self,
        tmp_path: Path,
        provider: MockProvider | None = None,
        config: EngineConfig | None = None,
    ) -> RalphLoop:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(repo),
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_COMMITTER_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        cfg = config or EngineConfig()
        cfg.phases.implement.run_tests_after_each_edit = False
        cfg.phases.implement.run_linters = False
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False

        loop = RalphLoop(
            config=cfg,
            llm=provider or MockProvider(),
            issue_url="https://github.com/org/repo/issues/1",
            repo_path=str(repo),
            output_dir=str(tmp_path / "output"),
        )
        return loop

    @pytest.mark.asyncio
    async def test_loop_narrates_start(self, tmp_path: Path):
        provider = _make_mock_provider(
            _triage_json(),
            _impl_json(),
            _review_json(),
            _validate_json(),
        )
        loop = self._make_loop(tmp_path, provider=provider)
        loop.register_phase("triage", TriagePhase)

        config = loop.config
        config.loop.max_iterations = 1
        await loop.run()

        narrations = loop.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Starting Ralph Loop" in m for m in messages)

    @pytest.mark.asyncio
    async def test_loop_narrates_phase_start(self, tmp_path: Path):
        provider = _make_mock_provider(_triage_json())
        loop = self._make_loop(tmp_path, provider=provider)
        loop.register_phase("triage", TriagePhase)
        loop.config.loop.max_iterations = 1
        await loop.run()

        narrations = loop.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Starting triage phase" in m for m in messages)

    @pytest.mark.asyncio
    async def test_loop_narrates_phase_result(self, tmp_path: Path):
        provider = _make_mock_provider(_triage_json())
        loop = self._make_loop(tmp_path, provider=provider)
        loop.register_phase("triage", TriagePhase)
        loop.config.loop.max_iterations = 1
        await loop.run()

        narrations = loop.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Phase triage succeeded" in m or "Phase triage failed" in m for m in messages)

    @pytest.mark.asyncio
    async def test_loop_narrates_completion(self, tmp_path: Path):
        provider = _make_mock_provider(_triage_json())
        loop = self._make_loop(tmp_path, provider=provider)
        loop.register_phase("triage", TriagePhase)
        loop.config.loop.max_iterations = 1
        await loop.run()

        narrations = loop.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Ralph Loop complete" in m for m in messages)

    @pytest.mark.asyncio
    async def test_loop_narrates_escalation(self, tmp_path: Path):
        provider = _make_mock_provider(
            _triage_json(classification="feature", recommendation="escalate")
        )
        loop = self._make_loop(tmp_path, provider=provider)
        loop.register_phase("triage", TriagePhase)
        await loop.run()

        narrations = loop.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("ESCALATION" in m for m in messages)

    @pytest.mark.asyncio
    async def test_loop_narrates_iteration_cap(self, tmp_path: Path):
        loop = self._make_loop(tmp_path)
        loop.config.loop.max_iterations = 0
        await loop.run()

        narrations = loop.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Iteration cap" in m for m in messages)

    @pytest.mark.asyncio
    async def test_loop_writes_progress_md(self, tmp_path: Path):
        provider = _make_mock_provider(_triage_json())
        loop = self._make_loop(tmp_path, provider=provider)
        loop.register_phase("triage", TriagePhase)
        loop.config.loop.max_iterations = 1
        await loop.run()

        progress_path = tmp_path / "output" / "progress.md"
        assert progress_path.exists()
        content = progress_path.read_text()
        assert "# Ralph Loop Progress" in content
        assert "## Iteration" in content

    @pytest.mark.asyncio
    async def test_loop_progress_has_phase_headings(self, tmp_path: Path):
        provider = _make_mock_provider(_triage_json())
        loop = self._make_loop(tmp_path, provider=provider)
        loop.register_phase("triage", TriagePhase)
        loop.config.loop.max_iterations = 1
        await loop.run()

        content = (tmp_path / "output" / "progress.md").read_text()
        assert "triage" in content


# ===========================================================================
# 3. Phase-level narration
# ===========================================================================


class TestTriageNarration:
    """Test that triage phase emits narration at OODA steps."""

    @pytest.mark.asyncio
    async def test_observe_narrates(self, tmp_path: Path):
        phase = _make_phase(
            TriagePhase,
            tmp_path,
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Nil pointer crash",
                "body": "The reconciler crashes with a nil pointer",
            },
        )
        await phase.observe()

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Fetched issue" in m or "source files" in m for m in messages)

    @pytest.mark.asyncio
    async def test_plan_narrates_classification(self, tmp_path: Path):
        provider = _make_mock_provider(_triage_json(classification="bug", confidence=0.85))
        phase = _make_phase(
            TriagePhase,
            tmp_path,
            provider=provider,
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Test bug",
                "body": "Details",
            },
        )
        observation = await phase.observe()
        await phase.plan(observation)

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Classified as bug" in m for m in messages)
        assert any("0.85" in m for m in messages)

    @pytest.mark.asyncio
    async def test_reflect_narrates_success(self, tmp_path: Path):
        provider = _make_mock_provider(_triage_json())
        phase = _make_phase(
            TriagePhase,
            tmp_path,
            provider=provider,
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Test bug",
                "body": "Details",
            },
        )
        await phase.execute()

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("implement" in m.lower() for m in messages)


class TestImplementNarration:
    """Test that implement phase emits narration at OODA steps."""

    @pytest.mark.asyncio
    async def test_observe_narrates_context(self, tmp_path: Path):
        triage_result = PhaseResult(
            phase="triage",
            success=True,
            findings={"classification": "bug"},
            artifacts={
                "triage_report": {"classification": "bug", "affected_components": []},
            },
        )
        phase = _make_phase(
            ImplementPhase,
            tmp_path,
            prior_results=[triage_result],
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Test bug",
                "body": "Details",
            },
        )
        await phase.observe()

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Gathered context" in m for m in messages)

    @pytest.mark.asyncio
    async def test_plan_narrates_fix_strategy(self, tmp_path: Path):
        provider = _make_mock_provider(_impl_json())
        triage_result = PhaseResult(
            phase="triage",
            success=True,
            findings={"classification": "bug"},
            artifacts={
                "triage_report": {"classification": "bug", "affected_components": []},
            },
        )
        phase = _make_phase(
            ImplementPhase,
            tmp_path,
            provider=provider,
            prior_results=[triage_result],
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Test bug",
                "body": "Details",
            },
        )
        obs = await phase.observe()
        await phase.plan(obs)

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Fix strategy" in m for m in messages)
        assert any("file change" in m for m in messages)


class TestReviewNarration:
    """Test that review phase emits narration at OODA steps."""

    @pytest.mark.asyncio
    async def test_observe_narrates_diff(self, tmp_path: Path):
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            artifacts={"diff": "--- a/file\n+++ b/file\n+fix", "files_changed": ["file.go"]},
            findings={"root_cause": "nil pointer"},
        )
        phase = _make_phase(ReviewPhase, tmp_path, prior_results=[impl_result])
        await phase.observe()

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Reviewing" in m for m in messages)

    @pytest.mark.asyncio
    async def test_plan_narrates_verdict(self, tmp_path: Path):
        provider = _make_mock_provider(_review_json(verdict="approve"))
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            artifacts={"diff": "+fix", "files_changed": ["f.go"]},
            findings={"root_cause": "x"},
        )
        phase = _make_phase(
            ReviewPhase,
            tmp_path,
            provider=provider,
            prior_results=[impl_result],
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Bug",
                "body": "Details",
            },
        )
        obs = await phase.observe()
        await phase.plan(obs)

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("verdict" in m.lower() and "approve" in m.lower() for m in messages)


class TestValidateNarration:
    """Test that validate phase emits narration at OODA steps."""

    @pytest.mark.asyncio
    async def test_observe_narrates_files(self, tmp_path: Path):
        review_result = PhaseResult(
            phase="review",
            success=True,
            artifacts={"review_report": {"verdict": "approve", "summary": "ok"}},
            findings={"verdict": "approve"},
        )
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            artifacts={"diff": "+fix", "files_changed": ["f.go"]},
            findings={"root_cause": "x"},
        )
        phase = _make_phase(
            ValidatePhase,
            tmp_path,
            prior_results=[impl_result, review_result],
        )
        await phase.observe()

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("Gathered" in m for m in messages)

    @pytest.mark.asyncio
    async def test_plan_narrates_check_results(self, tmp_path: Path):
        provider = _make_mock_provider(_validate_json())
        review_result = PhaseResult(
            phase="review",
            success=True,
            artifacts={"review_report": {"verdict": "approve", "summary": "ok"}},
            findings={"verdict": "approve"},
        )
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            artifacts={"diff": "+fix", "files_changed": ["f.go"]},
            findings={"root_cause": "x"},
        )
        cfg = EngineConfig()
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False

        phase = _make_phase(
            ValidatePhase,
            tmp_path,
            provider=provider,
            prior_results=[impl_result, review_result],
            issue_data={
                "url": "https://github.com/org/repo/issues/1",
                "title": "Bug",
                "body": "Details",
            },
        )
        phase.config = cfg
        obs = await phase.observe()
        await phase.plan(obs)

        narrations = phase.logger.get_narrations()
        messages = [n["message"] for n in narrations]
        assert any("tests" in m.lower() or "lint" in m.lower() for m in messages)


# ===========================================================================
# 4. E2E: progress.md structure from a full loop run
# ===========================================================================


class TestProgressMdStructure:
    """Test the overall structure of progress.md from a full loop execution."""

    def _make_loop(self, tmp_path: Path, provider: MockProvider) -> RalphLoop:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=str(repo),
            capture_output=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "test",
                "GIT_COMMITTER_NAME": "test",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_EMAIL": "t@t",
            },
        )

        cfg = EngineConfig()
        cfg.phases.implement.run_tests_after_each_edit = False
        cfg.phases.implement.run_linters = False
        cfg.phases.validate.full_test_suite = False
        cfg.phases.validate.ci_equivalent = False

        loop = RalphLoop(
            config=cfg,
            llm=provider,
            issue_url="https://github.com/org/repo/issues/42",
            repo_path=str(repo),
            output_dir=str(tmp_path / "output"),
        )
        loop.register_phase("triage", TriagePhase)
        loop.register_phase("implement", ImplementPhase)
        loop.register_phase("review", ReviewPhase)
        loop.register_phase("validate", ValidatePhase)
        return loop

    @pytest.mark.asyncio
    async def test_full_run_progress_structure(self, tmp_path: Path):
        provider = _make_mock_provider(
            _triage_json(),
            _impl_json(),
            _review_json(),
            _validate_json(),
        )
        loop = self._make_loop(tmp_path, provider)
        await loop.run()

        progress_path = tmp_path / "output" / "progress.md"
        assert progress_path.exists()
        content = progress_path.read_text()

        assert "# Ralph Loop Progress" in content
        assert "## Iteration" in content
        assert "Ralph Loop complete" in content

    @pytest.mark.asyncio
    async def test_narrations_have_timestamps(self, tmp_path: Path):
        provider = _make_mock_provider(
            _triage_json(),
            _impl_json(),
            _review_json(),
            _validate_json(),
        )
        loop = self._make_loop(tmp_path, provider)
        await loop.run()

        narrations = loop.logger.get_narrations()
        assert len(narrations) > 0
        for n in narrations:
            assert "timestamp" in n
            assert "phase" in n
            assert "message" in n

    @pytest.mark.asyncio
    async def test_narrations_count_reasonable(self, tmp_path: Path):
        provider = _make_mock_provider(
            _triage_json(),
            _impl_json(),
            _review_json(),
            _validate_json(),
        )
        loop = self._make_loop(tmp_path, provider)
        loop.config.loop.max_iterations = 5
        await loop.run()

        narrations = loop.logger.get_narrations()
        assert len(narrations) >= 5, "Expected at least 5 narrations from a multi-phase run"
