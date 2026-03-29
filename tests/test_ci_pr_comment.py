"""Tests for 10.5 — CI Remediation PR Comment Reporting.

Covers:
- CIRemediationAttempt and CIRemediationHistory dataclasses and serialization
- build_ci_pr_comment for success, escalation, timeout, and generic outcomes
- Flake section rendering when flake_reruns > 0
- _format_elapsed helper
- _generate_suggestions for each failure category
- Escalation comment with full failure details (tests, errors, annotations)
- Success comment with file changes and action details
- Empty attempts handling
- Loop integration: _extract_pr_number_from_url, _post_ci_pr_comment wiring
- CI monitoring loop tracks history and posts comment
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.loop import RalphLoop
from engine.workflow.ci_monitor import (
    CheckRunResult,
    CIFailureCategory,
    CIRemediationAttempt,
    CIRemediationHistory,
    CIResult,
    FailureDetails,
    _format_elapsed,
    _generate_suggestions,
    build_ci_pr_comment,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_attempt(
    *,
    iteration: int = 1,
    category: str = "test_failure",
    summary: str = "1 check(s) failed",
    failing_checks: list[str] | None = None,
    failing_tests: list[str] | None = None,
    action_taken: str = "pushed",
    files_changed: list[str] | None = None,
    fix_pushed: bool = True,
    success: bool = True,
) -> CIRemediationAttempt:
    return CIRemediationAttempt(
        iteration=iteration,
        category=category,
        summary=summary,
        failing_checks=failing_checks or ["ci/test"],
        failing_tests=failing_tests or [],
        action_taken=action_taken,
        files_changed=files_changed or [],
        fix_pushed=fix_pushed,
        success=success,
    )


def _make_failure_details(
    *,
    category: CIFailureCategory = CIFailureCategory.TEST_FAILURE,
    summary: str = "1 check(s) failed (test_failure): ci/test",
    failing_checks: list[str] | None = None,
    failing_tests: list[str] | None = None,
    error_messages: list[str] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> FailureDetails:
    return FailureDetails(
        category=category,
        summary=summary,
        failing_checks=failing_checks or ["ci/test"],
        failing_tests=failing_tests or [],
        error_messages=error_messages or [],
        annotations=annotations or [],
    )


def _make_history(
    *,
    outcome: str = "success",
    total_iterations: int = 1,
    flake_reruns: int = 0,
    elapsed_seconds: float = 120.0,
    attempts: list[CIRemediationAttempt] | None = None,
    final_failure: FailureDetails | None = None,
    escalation_reason: str = "",
) -> CIRemediationHistory:
    return CIRemediationHistory(
        outcome=outcome,
        total_iterations=total_iterations,
        flake_reruns=flake_reruns,
        elapsed_seconds=elapsed_seconds,
        attempts=attempts or [],
        final_failure=final_failure,
        escalation_reason=escalation_reason,
    )


# ------------------------------------------------------------------
# Dataclass serialization
# ------------------------------------------------------------------


class TestCIRemediationAttempt:
    def test_to_dict(self):
        a = _make_attempt(iteration=2, category="build_error", files_changed=["a.go"])
        d = a.to_dict()
        assert d["iteration"] == 2
        assert d["category"] == "build_error"
        assert d["files_changed"] == ["a.go"]

    def test_defaults(self):
        a = CIRemediationAttempt()
        assert a.iteration == 0
        assert a.category == "unknown"
        assert a.files_changed == []


class TestCIRemediationHistory:
    def test_to_dict(self):
        h = _make_history(
            outcome="escalated",
            attempts=[_make_attempt()],
            final_failure=_make_failure_details(),
        )
        d = h.to_dict()
        assert d["outcome"] == "escalated"
        assert len(d["attempts"]) == 1
        assert d["final_failure"] is not None
        assert d["final_failure"]["category"] == "test_failure"

    def test_to_dict_no_final_failure(self):
        h = _make_history()
        d = h.to_dict()
        assert d["final_failure"] is None

    def test_defaults(self):
        h = CIRemediationHistory()
        assert h.outcome == ""
        assert h.attempts == []
        assert h.final_failure is None


# ------------------------------------------------------------------
# _format_elapsed
# ------------------------------------------------------------------


class TestFormatElapsed:
    def test_seconds(self):
        assert _format_elapsed(42.3) == "42s"

    def test_minutes(self):
        assert _format_elapsed(150.0) == "2.5m"

    def test_hours(self):
        assert _format_elapsed(7200.0) == "2.0h"

    def test_boundary_60(self):
        assert _format_elapsed(60.0) == "1.0m"


# ------------------------------------------------------------------
# _generate_suggestions
# ------------------------------------------------------------------


class TestGenerateSuggestions:
    def test_test_failure(self):
        fd = _make_failure_details(
            category=CIFailureCategory.TEST_FAILURE,
            failing_tests=["TestFoo", "TestBar"],
        )
        s = _generate_suggestions(fd)
        assert any("assertion" in line for line in s)
        assert any("TestFoo" in line for line in s)

    def test_build_error(self):
        fd = _make_failure_details(category=CIFailureCategory.BUILD_ERROR)
        s = _generate_suggestions(fd)
        assert any("import" in line.lower() or "type" in line.lower() for line in s)

    def test_lint_violation(self):
        fd = _make_failure_details(category=CIFailureCategory.LINT_VIOLATION)
        s = _generate_suggestions(fd)
        assert any("linter" in line.lower() for line in s)

    def test_infrastructure_flake(self):
        fd = _make_failure_details(category=CIFailureCategory.INFRASTRUCTURE_FLAKE)
        s = _generate_suggestions(fd)
        assert any("re-run" in line.lower() for line in s)

    def test_timeout(self):
        fd = _make_failure_details(category=CIFailureCategory.TIMEOUT)
        s = _generate_suggestions(fd)
        assert any("timed out" in line.lower() or "timeout" in line.lower() for line in s)

    def test_unknown(self):
        fd = _make_failure_details(category=CIFailureCategory.UNKNOWN)
        s = _generate_suggestions(fd)
        assert len(s) >= 1


# ------------------------------------------------------------------
# Success comment
# ------------------------------------------------------------------


class TestSuccessComment:
    def test_no_attempts(self):
        h = _make_history(outcome="success", attempts=[])
        comment = build_ci_pr_comment(h)
        assert "CI Remediation Report" in comment
        assert "without requiring remediation" in comment

    def test_with_attempts(self):
        h = _make_history(
            outcome="success",
            attempts=[
                _make_attempt(
                    iteration=1,
                    success=False,
                    failing_tests=["TestA"],
                    files_changed=["fix.go"],
                ),
                _make_attempt(iteration=2, success=True),
            ],
        )
        comment = build_ci_pr_comment(h)
        assert "automatically resolved" in comment
        assert "2 remediation attempt" in comment
        assert "CI Failure History" in comment
        assert "`TestA`" in comment
        assert "`fix.go`" in comment

    def test_many_failing_tests_truncated(self):
        tests = [f"Test{i}" for i in range(10)]
        h = _make_history(
            outcome="success",
            attempts=[_make_attempt(failing_tests=tests)],
        )
        comment = build_ci_pr_comment(h)
        assert "+5 more" in comment

    def test_footer_present(self):
        h = _make_history(outcome="success", elapsed_seconds=90.0)
        comment = build_ci_pr_comment(h)
        assert "Ralph Loop Engine" in comment
        assert "1.5m" in comment


# ------------------------------------------------------------------
# Escalation comment
# ------------------------------------------------------------------


class TestEscalationComment:
    def test_basic_escalation(self):
        h = _make_history(
            outcome="escalated",
            escalation_reason="CI remediation iteration cap reached (3)",
            final_failure=_make_failure_details(
                failing_tests=["TestBroken"],
                error_messages=["assertion failed: expected 1, got 2"],
                annotations=[
                    {
                        "path": "pkg/foo.go",
                        "start_line": 42,
                        "annotation_level": "failure",
                        "message": "nil pointer dereference",
                    }
                ],
            ),
        )
        comment = build_ci_pr_comment(h)
        assert "escalated to human review" in comment
        assert "iteration cap reached" in comment
        assert "Current Failure Details" in comment
        assert "`TestBroken`" in comment
        assert "assertion failed" in comment
        assert "nil pointer dereference" in comment
        assert "Suggestions for Manual Fix" in comment

    def test_escalation_with_attempts(self):
        h = _make_history(
            outcome="escalated",
            attempts=[
                _make_attempt(iteration=1, success=False, files_changed=["a.go"]),
            ],
            final_failure=_make_failure_details(),
        )
        comment = build_ci_pr_comment(h)
        assert "What Was Tried" in comment
        assert "`a.go`" in comment

    def test_timeout_outcome(self):
        h = _make_history(
            outcome="timeout",
            escalation_reason="CI time budget exceeded",
        )
        comment = build_ci_pr_comment(h)
        assert "escalated to human review" in comment
        assert "CI time budget exceeded" in comment

    def test_no_final_failure(self):
        h = _make_history(outcome="escalated")
        comment = build_ci_pr_comment(h)
        assert "Review the CI logs" in comment


# ------------------------------------------------------------------
# Flake section
# ------------------------------------------------------------------


class TestFlakeSection:
    def test_flake_reruns(self):
        h = _make_history(outcome="success", flake_reruns=2)
        comment = build_ci_pr_comment(h)
        assert "Infrastructure flakes detected" in comment
        assert "re-run 2 time(s)" in comment

    def test_no_flake(self):
        h = _make_history(outcome="success", flake_reruns=0)
        comment = build_ci_pr_comment(h)
        assert "Infrastructure flakes" not in comment


# ------------------------------------------------------------------
# Generic comment
# ------------------------------------------------------------------


class TestGenericComment:
    def test_unknown_outcome(self):
        h = _make_history(outcome="something_else")
        comment = build_ci_pr_comment(h)
        assert "something_else" in comment

    def test_empty_outcome(self):
        h = _make_history(outcome="")
        comment = build_ci_pr_comment(h)
        assert "CI Remediation Report" in comment


# ------------------------------------------------------------------
# Loop integration: _extract_pr_number_from_url
# ------------------------------------------------------------------


class TestExtractPRNumber:
    def test_valid_url(self):
        from engine.loop import RalphLoop

        assert RalphLoop._extract_pr_number_from_url("https://github.com/org/repo/pull/42") == 42

    def test_url_with_trailing(self):
        from engine.loop import RalphLoop

        assert (
            RalphLoop._extract_pr_number_from_url("https://github.com/org/repo/pull/7/files") == 7
        )

    def test_url_with_query(self):
        from engine.loop import RalphLoop

        assert (
            RalphLoop._extract_pr_number_from_url("https://github.com/org/repo/pull/99?diff=split")
            == 99
        )

    def test_not_a_pr_url(self):
        from engine.loop import RalphLoop

        assert RalphLoop._extract_pr_number_from_url("https://github.com/org/repo/issues/5") == 0

    def test_empty_string(self):
        from engine.loop import RalphLoop

        assert RalphLoop._extract_pr_number_from_url("") == 0


# ------------------------------------------------------------------
# Loop integration: _post_ci_pr_comment
# ------------------------------------------------------------------


class TestPostCIPRComment:
    @pytest.fixture()
    def loop(self, tmp_path):
        from engine.config import EngineConfig
        from engine.integrations.llm import MockProvider

        return RalphLoop(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/org/repo/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )

    def test_skips_without_pr_number(self, loop):
        build_fn = MagicMock(return_value="comment body")
        asyncio.get_event_loop().run_until_complete(
            loop._post_ci_pr_comment(
                owner="org",
                repo="repo",
                pr_number=0,
                history=_make_history(),
                gh_token="tok",
                build_comment_fn=build_fn,
            )
        )
        build_fn.assert_not_called()

    @patch("engine.integrations.github.GitHubAdapter", autospec=False)
    def test_posts_comment(self, mock_adapter_cls, loop):
        mock_instance = MagicMock()
        mock_instance.post_comment = AsyncMock(return_value={"success": True, "id": 1})
        mock_adapter_cls.return_value = mock_instance

        build_fn = MagicMock(return_value="CI report body")

        asyncio.get_event_loop().run_until_complete(
            loop._post_ci_pr_comment(
                owner="org",
                repo="repo",
                pr_number=42,
                history=_make_history(),
                gh_token="tok",
                build_comment_fn=build_fn,
            )
        )

        build_fn.assert_called_once()
        mock_instance.post_comment.assert_awaited_once_with(42, "CI report body")

    def test_handles_post_failure(self, loop):
        build_fn = MagicMock(return_value="body")

        with patch("engine.integrations.github.GitHubAdapter") as mock_cls:
            mock_inst = MagicMock()
            mock_inst.post_comment = AsyncMock(
                return_value={"success": False, "error": "forbidden"}
            )
            mock_cls.return_value = mock_inst

            asyncio.get_event_loop().run_until_complete(
                loop._post_ci_pr_comment(
                    owner="org",
                    repo="repo",
                    pr_number=5,
                    history=_make_history(),
                    gh_token="tok",
                    build_comment_fn=build_fn,
                )
            )

    def test_handles_exception(self, loop):
        build_fn = MagicMock(return_value="body")

        with patch("engine.integrations.github.GitHubAdapter", side_effect=Exception("boom")):
            asyncio.get_event_loop().run_until_complete(
                loop._post_ci_pr_comment(
                    owner="org",
                    repo="repo",
                    pr_number=5,
                    history=_make_history(),
                    gh_token="tok",
                    build_comment_fn=build_fn,
                )
            )


# ------------------------------------------------------------------
# CI monitoring loop posts comment
# ------------------------------------------------------------------


class TestCIMonitoringLoopComment:
    """Verify the CI monitoring loop builds history and posts a comment."""

    @pytest.fixture()
    def loop(self, tmp_path):
        from engine.config import CIRemediationConfig, EngineConfig
        from engine.integrations.llm import MockProvider
        from engine.phases.base import Phase, PhaseResult

        class StubCI(Phase):
            name = "ci_remediate"
            allowed_tools: ClassVar[list[str]] = []

            async def observe(self):
                return {}

            async def plan(self, obs):
                return {}

            async def act(self, plan):
                return {"files_changed": [], "pushed": False}

            async def validate(self, act):
                return {"valid": True, "action_result": act}

            async def reflect(self, val):
                return PhaseResult(
                    phase="ci_remediate",
                    success=False,
                    should_continue=True,
                    findings={"action": "no_fix"},
                    artifacts={"files_changed": [], "pushed": False},
                )

        config = EngineConfig()
        config.ci_remediation = CIRemediationConfig(
            enabled=True,
            max_iterations=2,
            time_budget_minutes=60,
            ci_poll_interval_seconds=0,
            ci_poll_timeout_minutes=1,
        )

        lp = RalphLoop(
            config=config,
            llm=MockProvider(),
            issue_url="https://github.com/org/repo/issues/1",
            repo_path=str(tmp_path),
            output_dir=str(tmp_path / "output"),
        )
        lp.register_phase("ci_remediate", StubCI)
        lp._start_time = __import__("time").monotonic()
        return lp

    def test_comment_posted_on_escalation(self, loop):
        from engine.phases.base import PhaseResult

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            should_continue=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/10",
                "branch_name": "rl/fix-issue-1",
            },
        )

        ci_fail = CIResult(
            sha="abc",
            overall_state="failure",
            completed=True,
            check_runs=[
                CheckRunResult(id=1, name="ci/test", status="completed", conclusion="failure")
            ],
            total_count=1,
        )

        posted_comments: list[str] = []

        async def mock_post(pr_num, body):
            posted_comments.append(body)
            return {"success": True, "id": 1}

        with (
            patch.dict(os.environ, {"GH_PAT": "fake-token"}),
            patch(
                "engine.workflow.ci_monitor.CIMonitor.poll_ci_status", new_callable=AsyncMock
            ) as mock_poll,
            patch("engine.integrations.github.GitHubAdapter") as mock_adapter_cls,
        ):
            mock_poll.return_value = ci_fail

            mock_inst = MagicMock()
            mock_inst.post_comment = AsyncMock(side_effect=mock_post)
            mock_adapter_cls.return_value = mock_inst

            outcome = asyncio.get_event_loop().run_until_complete(
                loop._run_ci_monitoring_loop(validate_result, [])
            )

        assert outcome == "escalated"
        assert len(posted_comments) == 1
        assert "CI Remediation Report" in posted_comments[0]
        assert "escalated" in posted_comments[0].lower()

    def test_no_comment_on_immediate_success(self, loop):
        from engine.phases.base import PhaseResult

        validate_result = PhaseResult(
            phase="validate",
            success=True,
            should_continue=True,
            artifacts={
                "pr_created": True,
                "pr_url": "https://github.com/org/repo/pull/10",
                "branch_name": "rl/fix-issue-1",
            },
        )

        ci_pass = CIResult(sha="abc", overall_state="success", completed=True, total_count=1)

        with (
            patch.dict(os.environ, {"GH_PAT": "fake-token"}),
            patch(
                "engine.workflow.ci_monitor.CIMonitor.poll_ci_status", new_callable=AsyncMock
            ) as mock_poll,
            patch("engine.integrations.github.GitHubAdapter") as mock_adapter_cls,
        ):
            mock_poll.return_value = ci_pass
            mock_inst = MagicMock()
            mock_inst.post_comment = AsyncMock()
            mock_adapter_cls.return_value = mock_inst

            outcome = asyncio.get_event_loop().run_until_complete(
                loop._run_ci_monitoring_loop(validate_result, [])
            )

        assert outcome == "success"
        mock_inst.post_comment.assert_not_awaited()


# ------------------------------------------------------------------
# Import-level verification
# ------------------------------------------------------------------


class TestImports:
    def test_all_exports(self):
        from engine.workflow import ci_monitor

        assert hasattr(ci_monitor, "build_ci_pr_comment")
        assert hasattr(ci_monitor, "CIRemediationAttempt")
        assert hasattr(ci_monitor, "CIRemediationHistory")

    def test_loop_import(self):
        from engine.loop import RalphLoop

        assert hasattr(RalphLoop, "_post_ci_pr_comment")
        assert hasattr(RalphLoop, "_extract_pr_number_from_url")
