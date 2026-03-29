"""Tests for 10.2 — CI Monitor and Result Downloader.

Covers:
- CheckRunResult, CIResult, FailureDetails dataclasses and serialisation
- CIResult properties (passed, failed_runs)
- CIFailureCategory enum values
- CIMonitor.poll_ci_status (pending→success, pending→failure, timeout)
- CIMonitor.download_ci_results and annotation fetching
- CIMonitor.categorize_failure for each failure category
- CIMonitor.extract_failure_details structured output
- CIMonitor.trigger_rerun and trigger_rerun_failed_jobs
- Module-level helpers: URL parsing, keyword matching, test name extraction
- Config integration (poll interval, timeout from CIRemediationConfig)
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from engine.config import CIRemediationConfig
from engine.workflow.ci_monitor import (
    CheckRunResult,
    CIFailureCategory,
    CIMonitor,
    CIResult,
    FailureDetails,
    _aggregate_failure_text,
    _extract_annotations,
    _extract_failing_test_names,
    _extract_run_id_from_url,
    _has_test_signal,
    _matches_keywords,
)

# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------


def _make_check_run(
    *,
    name: str = "ci/test",
    status: str = "completed",
    conclusion: str = "success",
    cr_id: int = 1,
    output_title: str = "",
    output_summary: str = "",
    output_text: str = "",
    annotations: list[dict[str, Any]] | None = None,
    details_url: str = "",
) -> dict[str, Any]:
    """Build a raw GitHub API check_run response dict."""
    output: dict[str, Any] = {
        "title": output_title,
        "summary": output_summary,
        "text": output_text,
        "annotations_count": len(annotations) if annotations else 0,
    }
    if annotations:
        output["annotations"] = annotations
    return {
        "id": cr_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/org/repo/runs/{cr_id}",
        "details_url": details_url or f"https://github.com/org/repo/actions/runs/9000/jobs/{cr_id}",
        "output": output,
        "started_at": "2026-03-29T10:00:00Z",
        "completed_at": "2026-03-29T10:05:00Z" if status == "completed" else "",
        "app": {"slug": "github-actions"},
    }


def _make_ci_result(
    *,
    check_runs: list[CheckRunResult] | None = None,
    overall_state: str = "success",
    completed: bool = True,
) -> CIResult:
    return CIResult(
        sha="abc123",
        overall_state=overall_state,
        check_runs=check_runs or [],
        total_count=len(check_runs) if check_runs else 0,
        completed=completed,
        workflow_run_ids=[9000],
    )


def _failed_cr(
    name: str = "ci/test",
    conclusion: str = "failure",
    output_text: str = "",
    output_title: str = "",
    output_summary: str = "",
    annotations: list[dict[str, Any]] | None = None,
) -> CheckRunResult:
    return CheckRunResult(
        id=1,
        name=name,
        status="completed",
        conclusion=conclusion,
        output_title=output_title,
        output_summary=output_summary,
        output_text=output_text,
        annotations=annotations or [],
    )


def _monitor(
    config: CIRemediationConfig | None = None,
    logger: Any = None,
) -> CIMonitor:
    return CIMonitor(
        token="test-token",
        owner="org",
        repo="repo",
        config=config,
        logger=logger,
    )


# ==================================================================
# Dataclass tests
# ==================================================================


class TestCheckRunResult:
    def test_defaults(self) -> None:
        cr = CheckRunResult()
        assert cr.id == 0
        assert cr.name == ""
        assert cr.status == ""
        assert cr.conclusion == ""
        assert cr.annotations == []

    def test_to_dict(self) -> None:
        cr = CheckRunResult(id=42, name="lint", status="completed", conclusion="success")
        d = cr.to_dict()
        assert d["id"] == 42
        assert d["name"] == "lint"
        assert d["conclusion"] == "success"

    def test_to_dict_truncates_output_text(self) -> None:
        cr = CheckRunResult(output_text="x" * 5000)
        d = cr.to_dict()
        assert len(d["output_text"]) == 2000

    def test_to_dict_caps_annotations(self) -> None:
        anns = [{"message": f"ann-{i}"} for i in range(100)]
        cr = CheckRunResult(annotations=anns)
        d = cr.to_dict()
        assert len(d["annotations"]) == 50


class TestCIResult:
    def test_defaults(self) -> None:
        ci = CIResult()
        assert ci.sha == ""
        assert ci.overall_state == ""
        assert ci.check_runs == []
        assert ci.completed is False

    def test_passed_true(self) -> None:
        ci = CIResult(completed=True, overall_state="success")
        assert ci.passed is True

    def test_passed_false_not_completed(self) -> None:
        ci = CIResult(completed=False, overall_state="success")
        assert ci.passed is False

    def test_passed_false_failure(self) -> None:
        ci = CIResult(completed=True, overall_state="failure")
        assert ci.passed is False

    def test_failed_runs_filters(self) -> None:
        runs = [
            CheckRunResult(name="ok", conclusion="success"),
            CheckRunResult(name="bad", conclusion="failure"),
            CheckRunResult(name="timeout", conclusion="timed_out"),
            CheckRunResult(name="cancel", conclusion="cancelled"),
            CheckRunResult(name="skip", conclusion="skipped"),
        ]
        ci = CIResult(check_runs=runs)
        failed = ci.failed_runs
        assert len(failed) == 3
        names = {cr.name for cr in failed}
        assert names == {"bad", "timeout", "cancel"}

    def test_to_dict(self) -> None:
        ci = CIResult(sha="abc", overall_state="failure", completed=True, elapsed_seconds=42.6)
        d = ci.to_dict()
        assert d["sha"] == "abc"
        assert d["elapsed_seconds"] == 42.6
        assert d["completed"] is True

    def test_to_dict_nested_check_runs(self) -> None:
        cr = CheckRunResult(id=1, name="test")
        ci = CIResult(check_runs=[cr])
        d = ci.to_dict()
        assert len(d["check_runs"]) == 1
        assert d["check_runs"][0]["name"] == "test"


class TestFailureDetails:
    def test_defaults(self) -> None:
        fd = FailureDetails()
        assert fd.category == CIFailureCategory.UNKNOWN
        assert fd.failing_checks == []

    def test_to_dict(self) -> None:
        fd = FailureDetails(
            category=CIFailureCategory.TEST_FAILURE,
            summary="1 check(s) failed",
            failing_checks=["ci/test"],
            recommended_action="remediate",
        )
        d = fd.to_dict()
        assert d["category"] == "test_failure"
        assert d["recommended_action"] == "remediate"
        assert d["failing_checks"] == ["ci/test"]

    def test_to_dict_truncates_log_excerpts(self) -> None:
        fd = FailureDetails(log_excerpts=["x" * 5000])
        d = fd.to_dict()
        assert len(d["log_excerpts"][0]) == 2000

    def test_to_dict_caps_lists(self) -> None:
        fd = FailureDetails(
            error_messages=[f"e{i}" for i in range(50)],
            failing_tests=[f"t{i}" for i in range(100)],
            annotations=[{"msg": f"a{i}"} for i in range(60)],
        )
        d = fd.to_dict()
        assert len(d["error_messages"]) == 20
        assert len(d["failing_tests"]) == 50
        assert len(d["annotations"]) == 30


class TestCIFailureCategory:
    def test_values(self) -> None:
        assert CIFailureCategory.TEST_FAILURE.value == "test_failure"
        assert CIFailureCategory.BUILD_ERROR.value == "build_error"
        assert CIFailureCategory.LINT_VIOLATION.value == "lint_violation"
        assert CIFailureCategory.INFRASTRUCTURE_FLAKE.value == "infrastructure_flake"
        assert CIFailureCategory.TIMEOUT.value == "timeout"
        assert CIFailureCategory.UNKNOWN.value == "unknown"

    def test_is_str(self) -> None:
        assert isinstance(CIFailureCategory.TEST_FAILURE, str)
        assert CIFailureCategory.TEST_FAILURE == "test_failure"


# ==================================================================
# Module-level helper tests
# ==================================================================


class TestExtractRunIdFromUrl:
    def test_valid_url(self) -> None:
        url = "https://github.com/org/repo/actions/runs/12345/jobs/67890"
        assert _extract_run_id_from_url(url) == 12345

    def test_url_without_jobs(self) -> None:
        url = "https://github.com/org/repo/actions/runs/99999"
        assert _extract_run_id_from_url(url) == 99999

    def test_not_actions_url(self) -> None:
        assert _extract_run_id_from_url("https://github.com/org/repo/pulls/1") is None

    def test_empty(self) -> None:
        assert _extract_run_id_from_url("") is None

    def test_malformed(self) -> None:
        assert _extract_run_id_from_url("https://github.com/actions/runs/notanumber") is None


class TestExtractAnnotations:
    def test_with_annotations(self) -> None:
        output = {
            "annotations_count": 2,
            "annotations": [
                {"path": "foo.go", "start_line": 10, "end_line": 10, "message": "err"},
                {"path": "bar.go", "start_line": 5, "end_line": 5, "message": "warn"},
            ],
        }
        result = _extract_annotations(output)
        assert len(result) == 2
        assert result[0]["path"] == "foo.go"

    def test_no_annotations(self) -> None:
        assert _extract_annotations({"annotations_count": 0}) == []
        assert _extract_annotations({}) == []

    def test_malformed_annotations(self) -> None:
        assert _extract_annotations({"annotations_count": 1, "annotations": "bad"}) == []


class TestMatchesKeywords:
    def test_match(self) -> None:
        assert _matches_keywords("the build failed", frozenset({"build", "compile"})) is True

    def test_no_match(self) -> None:
        assert _matches_keywords("all tests passed", frozenset({"build", "compile"})) is False


class TestHasTestSignal:
    def test_test_word_in_text(self) -> None:
        ci = _make_ci_result(check_runs=[], overall_state="failure", completed=True)
        assert _has_test_signal("test failed", ci) is True

    def test_test_word_in_check_name(self) -> None:
        cr = _failed_cr(name="unit-test")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _has_test_signal("something happened", ci) is True

    def test_no_signal(self) -> None:
        cr = _failed_cr(name="deploy")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _has_test_signal("deployment broke", ci) is False


class TestExtractFailingTestNames:
    def test_go_failures(self) -> None:
        text = "--- FAIL: TestReconcile (0.05s)\n--- FAIL: TestHandler (0.01s)"
        names = _extract_failing_test_names(text)
        assert "TestReconcile" in names
        assert "TestHandler" in names

    def test_pytest_failures(self) -> None:
        text = "FAILED tests/test_main.py::TestFoo::test_bar\nFAILED tests/test_utils.py::test_x"
        names = _extract_failing_test_names(text)
        assert any("test_bar" in n for n in names)
        assert any("test_x" in n for n in names)

    def test_rust_failures(self) -> None:
        text = "test my_module::test_something ... FAILED"
        names = _extract_failing_test_names(text)
        assert "my_module::test_something" in names

    def test_dedup(self) -> None:
        text = "--- FAIL: TestA (0.1s)\n--- FAIL: TestA (0.2s)"
        names = _extract_failing_test_names(text)
        assert names.count("TestA") == 1

    def test_empty_text(self) -> None:
        assert _extract_failing_test_names("") == []


class TestAggregateFailureText:
    def test_aggregates_all_fields(self) -> None:
        cr = _failed_cr(
            name="ci/test",
            output_title="Tests failed",
            output_summary="3 failures",
            output_text="--- FAIL: TestX",
            annotations=[{"message": "nil pointer"}],
        )
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        text = _aggregate_failure_text(ci)
        assert "ci/test" in text
        assert "Tests failed" in text
        assert "3 failures" in text
        assert "FAIL: TestX" in text
        assert "nil pointer" in text


# ==================================================================
# CIMonitor constructor and config integration
# ==================================================================


class TestCIMonitorInit:
    def test_defaults_without_config(self) -> None:
        mon = _monitor()
        assert mon.repo_slug == "org/repo"
        assert mon._poll_interval == 30
        assert mon._poll_timeout == 20 * 60

    def test_config_overrides(self) -> None:
        cfg = CIRemediationConfig(ci_poll_interval_seconds=10, ci_poll_timeout_minutes=5)
        mon = _monitor(config=cfg)
        assert mon._poll_interval == 10
        assert mon._poll_timeout == 5 * 60

    def test_api_base_trailing_slash(self) -> None:
        mon = CIMonitor(token="t", owner="o", repo="r", api_base="https://api.example.com/")
        assert mon._api_base == "https://api.example.com"


# ==================================================================
# poll_ci_status tests
# ==================================================================


class TestPollCIStatus:
    @pytest.mark.asyncio
    async def test_immediate_success(self) -> None:
        mon = _monitor()
        runs = [_make_check_run(name="ci/test", conclusion="success")]
        resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert result.completed is True
        assert result.overall_state == "success"
        assert result.passed is True
        assert len(result.check_runs) == 1

    @pytest.mark.asyncio
    async def test_pending_then_success(self) -> None:
        mon = _monitor()
        pending_run = _make_check_run(name="ci/test", status="in_progress", conclusion="")
        done_run = _make_check_run(name="ci/test", conclusion="success")

        pending_resp = {
            "success": True,
            "body": {"check_runs": [pending_run], "total_count": 1},
            "status_code": 200,
        }
        done_resp = {
            "success": True,
            "body": {"check_runs": [done_run], "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(side_effect=[pending_resp, done_resp])

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert result.completed is True
        assert result.overall_state == "success"

    @pytest.mark.asyncio
    async def test_pending_then_failure(self) -> None:
        mon = _monitor()
        pending_run = _make_check_run(name="ci/test", status="in_progress", conclusion="")
        failed_run = _make_check_run(name="ci/test", conclusion="failure")

        pending_resp = {
            "success": True,
            "body": {"check_runs": [pending_run], "total_count": 1},
            "status_code": 200,
        }
        failed_resp = {
            "success": True,
            "body": {"check_runs": [failed_run], "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(side_effect=[pending_resp, failed_resp])

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert result.completed is True
        assert result.overall_state == "failure"
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        mon = _monitor()
        pending_run = _make_check_run(name="ci/test", status="in_progress", conclusion="")
        pending_resp = {
            "success": True,
            "body": {"check_runs": [pending_run], "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=pending_resp)

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=0)
        assert result.completed is False
        assert result.elapsed_seconds >= 0

    @pytest.mark.asyncio
    async def test_api_error_returns_incomplete(self) -> None:
        mon = _monitor()
        mon._api_get = AsyncMock(return_value={"success": False, "error": "HTTP 500"})

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=0)
        assert result.completed is False
        assert result.overall_state == "error"

    @pytest.mark.asyncio
    async def test_no_check_runs_stays_pending(self) -> None:
        mon = _monitor()
        resp = {
            "success": True,
            "body": {"check_runs": [], "total_count": 0},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=0)
        assert result.completed is False
        assert result.overall_state == "pending"

    @pytest.mark.asyncio
    async def test_multiple_check_runs(self) -> None:
        mon = _monitor()
        runs = [
            _make_check_run(name="ci/test", cr_id=1, conclusion="success"),
            _make_check_run(name="ci/lint", cr_id=2, conclusion="success"),
            _make_check_run(name="ci/build", cr_id=3, conclusion="success"),
        ]
        resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 3},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert result.completed is True
        assert result.overall_state == "success"
        assert result.total_count == 3

    @pytest.mark.asyncio
    async def test_one_failure_among_many(self) -> None:
        mon = _monitor()
        runs = [
            _make_check_run(name="ci/test", cr_id=1, conclusion="success"),
            _make_check_run(name="ci/lint", cr_id=2, conclusion="failure"),
            _make_check_run(name="ci/build", cr_id=3, conclusion="success"),
        ]
        resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 3},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert result.completed is True
        assert result.overall_state == "failure"
        assert len(result.failed_runs) == 1

    @pytest.mark.asyncio
    async def test_extracts_workflow_run_ids(self) -> None:
        mon = _monitor()
        run = _make_check_run(
            name="ci/test",
            details_url="https://github.com/org/repo/actions/runs/12345/jobs/1",
        )
        resp = {
            "success": True,
            "body": {"check_runs": [run], "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert 12345 in result.workflow_run_ids

    @pytest.mark.asyncio
    async def test_logs_with_logger(self) -> None:
        logger = MagicMock()
        mon = _monitor(logger=logger)
        runs = [_make_check_run(conclusion="success")]
        resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        logger.info.assert_called()

    @pytest.mark.asyncio
    async def test_poll_interval_override(self) -> None:
        cfg = CIRemediationConfig(ci_poll_interval_seconds=999, ci_poll_timeout_minutes=1)
        mon = _monitor(config=cfg)
        runs = [_make_check_run(conclusion="success")]
        resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        result = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert result.completed is True


# ==================================================================
# download_ci_results tests
# ==================================================================


class TestDownloadCIResults:
    @pytest.mark.asyncio
    async def test_fetches_annotations(self) -> None:
        mon = _monitor()
        runs = [_make_check_run(name="ci/test", cr_id=42, conclusion="failure")]
        check_resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 1},
            "status_code": 200,
        }
        ann_resp = {
            "success": True,
            "body": [
                {
                    "path": "main.go",
                    "start_line": 10,
                    "end_line": 10,
                    "annotation_level": "failure",
                    "message": "nil pointer dereference",
                    "title": "test error",
                }
            ],
            "status_code": 200,
        }

        async def mock_get(endpoint: str, **kwargs: Any) -> dict[str, Any]:
            if "annotations" in endpoint:
                return ann_resp
            return check_resp

        mon._api_get = AsyncMock(side_effect=mock_get)

        result = await mon.download_ci_results("abc123")
        assert len(result.check_runs) == 1
        assert len(result.check_runs[0].annotations) == 1
        assert result.check_runs[0].annotations[0]["path"] == "main.go"

    @pytest.mark.asyncio
    async def test_skips_annotation_fetch_when_already_present(self) -> None:
        mon = _monitor()
        anns = [{"path": "f.go", "start_line": 1, "end_line": 1, "message": "err"}]
        runs = [_make_check_run(name="ci/test", cr_id=42, conclusion="failure", annotations=anns)]
        check_resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=check_resp)

        await mon.download_ci_results("abc123")
        calls = [str(c) for c in mon._api_get.call_args_list]
        assert not any("annotations" in c for c in calls)

    @pytest.mark.asyncio
    async def test_handles_annotation_fetch_failure(self) -> None:
        mon = _monitor()
        runs = [_make_check_run(name="ci/test", cr_id=42, conclusion="failure")]
        check_resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 1},
            "status_code": 200,
        }

        async def mock_get(endpoint: str, **kwargs: Any) -> dict[str, Any]:
            if "annotations" in endpoint:
                return {"success": False, "error": "HTTP 500"}
            return check_resp

        mon._api_get = AsyncMock(side_effect=mock_get)

        result = await mon.download_ci_results("abc123")
        assert result.check_runs[0].annotations == []


# ==================================================================
# download_workflow_log tests
# ==================================================================


class TestDownloadWorkflowLog:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        mon = _monitor()
        mon._api_get = AsyncMock(
            return_value={"success": True, "body": {"raw": "log content here"}, "status_code": 200}
        )

        log = await mon.download_workflow_log(9000)
        assert "log content" in log

    @pytest.mark.asyncio
    async def test_failure(self) -> None:
        mon = _monitor()
        mon._api_get = AsyncMock(return_value={"success": False, "error": "HTTP 404"})

        log = await mon.download_workflow_log(9000)
        assert "failed to download" in log

    @pytest.mark.asyncio
    async def test_truncation(self) -> None:
        mon = _monitor()
        mon._api_get = AsyncMock(
            return_value={"success": True, "body": {"raw": "x" * 100000}, "status_code": 200}
        )

        log = await mon.download_workflow_log(9000)
        assert len(log) <= 50000


# ==================================================================
# categorize_failure tests
# ==================================================================


class TestCategorizeFailure:
    def test_no_failures(self) -> None:
        ci = _make_ci_result(check_runs=[], overall_state="success")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.UNKNOWN

    def test_infrastructure_flake(self) -> None:
        cr = _failed_cr(output_text="Error: runner timed out waiting for connection")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.INFRASTRUCTURE_FLAKE

    def test_infrastructure_service_unavailable(self) -> None:
        cr = _failed_cr(output_text="503 service unavailable")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.INFRASTRUCTURE_FLAKE

    def test_infrastructure_docker_daemon(self) -> None:
        cr = _failed_cr(output_text="Cannot connect to the docker daemon")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.INFRASTRUCTURE_FLAKE

    def test_timeout(self) -> None:
        cr = _failed_cr(conclusion="timed_out", output_text="The job exceeded max time")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.TIMEOUT

    def test_build_error(self) -> None:
        cr = _failed_cr(
            name="ci/build",
            output_text="compilation failed: cannot find module 'github.com/foo/bar'",
        )
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.BUILD_ERROR

    def test_build_error_go(self) -> None:
        cr = _failed_cr(output_text="go build ./...: undefined reference to 'main'")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.BUILD_ERROR

    def test_lint_violation(self) -> None:
        cr = _failed_cr(name="ci/lint", output_text="golangci-lint found 5 issues")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.LINT_VIOLATION

    def test_lint_eslint(self) -> None:
        cr = _failed_cr(output_text="eslint reported 3 problems")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.LINT_VIOLATION

    def test_test_failure(self) -> None:
        cr = _failed_cr(
            name="ci/test",
            output_text="--- FAIL: TestReconcile (0.05s)\n    reconciler_test.go:42: expected nil",
        )
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.TEST_FAILURE

    def test_test_failure_from_name(self) -> None:
        cr = _failed_cr(name="unit-test-suite", output_text="exited with code 1")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.TEST_FAILURE

    def test_unknown_failure(self) -> None:
        cr = _failed_cr(name="deploy-staging", output_text="deployment script returned error")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.UNKNOWN

    def test_infrastructure_trumps_test(self) -> None:
        """Infrastructure flakes have priority over test failures."""
        cr = _failed_cr(
            name="ci/test",
            output_text="test failed because the runner timed out",
        )
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.INFRASTRUCTURE_FLAKE

    def test_build_trumps_lint(self) -> None:
        """Build errors have priority over lint violations."""
        cr = _failed_cr(
            output_text="lint: compilation failed: cannot find module 'foo'",
        )
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        assert _monitor().categorize_failure(ci) == CIFailureCategory.BUILD_ERROR


# ==================================================================
# extract_failure_details tests
# ==================================================================


class TestExtractFailureDetails:
    def test_basic_extraction(self) -> None:
        cr = _failed_cr(
            name="ci/test",
            output_title="Tests failed",
            output_summary="3 tests failed",
            output_text="--- FAIL: TestX (0.1s)\n--- FAIL: TestY (0.2s)",
        )
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        details = _monitor().extract_failure_details(ci)

        assert details.category == CIFailureCategory.TEST_FAILURE
        assert "ci/test" in details.failing_checks
        assert "Tests failed" in details.error_messages[0]
        assert "TestX" in details.failing_tests
        assert "TestY" in details.failing_tests
        assert details.recommended_action == "remediate"

    def test_with_annotations(self) -> None:
        anns = [
            {"path": "main.go", "start_line": 10, "message": "err", "annotation_level": "failure"}
        ]
        cr = _failed_cr(name="ci/test", annotations=anns)
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        details = _monitor().extract_failure_details(ci)

        assert len(details.annotations) > 0
        assert details.annotations[0]["path"] == "main.go"

    def test_with_log_excerpts(self) -> None:
        cr = _failed_cr(name="ci/build", output_text="compile error: undefined var")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        details = _monitor().extract_failure_details(ci, CIFailureCategory.BUILD_ERROR)

        assert len(details.log_excerpts) == 1
        assert "ci/build" in details.log_excerpts[0]
        assert "compile error" in details.log_excerpts[0]

    def test_infrastructure_recommended_rerun(self) -> None:
        cr = _failed_cr(output_text="runner connection refused")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        ci.workflow_run_ids = [9000]
        details = _monitor().extract_failure_details(ci, CIFailureCategory.INFRASTRUCTURE_FLAKE)

        assert details.recommended_action == "rerun"
        assert 9000 in details.workflow_run_ids

    def test_timeout_recommended_escalate(self) -> None:
        cr = _failed_cr(conclusion="timed_out")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        details = _monitor().extract_failure_details(ci, CIFailureCategory.TIMEOUT)

        assert details.recommended_action == "escalate"

    def test_explicit_category_overrides(self) -> None:
        cr = _failed_cr(name="ci/test", output_text="test failed")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        details = _monitor().extract_failure_details(ci, CIFailureCategory.BUILD_ERROR)

        assert details.category == CIFailureCategory.BUILD_ERROR

    def test_summary_format(self) -> None:
        cr = _failed_cr(name="ci/test")
        ci = _make_ci_result(check_runs=[cr], overall_state="failure")
        details = _monitor().extract_failure_details(ci)
        assert "1 check(s) failed" in details.summary
        assert "ci/test" in details.summary

    def test_multiple_failures(self) -> None:
        cr1 = _failed_cr(name="ci/test", output_text="FAILED test_a.py::test_one")
        cr2 = _failed_cr(name="ci/lint", output_text="ruff found 2 errors")
        ci = _make_ci_result(check_runs=[cr1, cr2], overall_state="failure")
        details = _monitor().extract_failure_details(ci)

        assert len(details.failing_checks) == 2
        assert "ci/test" in details.failing_checks
        assert "ci/lint" in details.failing_checks

    def test_no_failures_returns_empty_details(self) -> None:
        ci = _make_ci_result(check_runs=[], overall_state="success")
        details = _monitor().extract_failure_details(ci)
        assert details.failing_checks == []
        assert details.category == CIFailureCategory.UNKNOWN


# ==================================================================
# trigger_rerun tests
# ==================================================================


class TestTriggerRerun:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        mon = _monitor()
        mon._api_post = AsyncMock(return_value={"success": True, "status_code": 201, "body": {}})

        result = await mon.trigger_rerun(9000)
        assert result["success"] is True
        assert result["run_id"] == 9000

        call_args = mon._api_post.call_args
        assert "/actions/runs/9000/rerun" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_failure(self) -> None:
        mon = _monitor()
        mon._api_post = AsyncMock(return_value={"success": False, "error": "HTTP 403: forbidden"})

        result = await mon.trigger_rerun(9000)
        assert result["success"] is False
        assert "403" in result["error"]

    @pytest.mark.asyncio
    async def test_logs_on_success(self) -> None:
        logger = MagicMock()
        mon = _monitor(logger=logger)
        mon._api_post = AsyncMock(return_value={"success": True, "status_code": 201, "body": {}})

        await mon.trigger_rerun(9000)
        logger.info.assert_called()

    @pytest.mark.asyncio
    async def test_logs_on_failure(self) -> None:
        logger = MagicMock()
        mon = _monitor(logger=logger)
        mon._api_post = AsyncMock(return_value={"success": False, "error": "HTTP 500"})

        await mon.trigger_rerun(9000)
        logger.warn.assert_called()


class TestTriggerRerunFailedJobs:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        mon = _monitor()
        mon._api_post = AsyncMock(return_value={"success": True, "status_code": 201, "body": {}})

        result = await mon.trigger_rerun_failed_jobs(9000)
        assert result["success"] is True
        assert result["run_id"] == 9000

        call_args = mon._api_post.call_args
        assert "rerun-failed-jobs" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_failure(self) -> None:
        mon = _monitor()
        mon._api_post = AsyncMock(return_value={"success": False, "error": "HTTP 403: forbidden"})

        result = await mon.trigger_rerun_failed_jobs(9000)
        assert result["success"] is False


# ==================================================================
# HTTP layer tests
# ==================================================================


class TestHTTPHelpers:
    def test_headers_with_token(self) -> None:
        mon = _monitor()
        h = mon._headers()
        assert "Bearer test-token" in h["Authorization"]
        assert h["X-GitHub-Api-Version"] == "2022-11-28"

    def test_headers_without_token(self) -> None:
        mon = CIMonitor(token="", owner="o", repo="r")
        h = mon._headers()
        assert "Authorization" not in h


# ==================================================================
# Integration/round-trip tests
# ==================================================================


class TestIntegration:
    @pytest.mark.asyncio
    async def test_poll_categorize_extract_pipeline(self) -> None:
        """Full pipeline: poll → categorize → extract."""
        mon = _monitor()
        runs = [
            _make_check_run(
                name="ci/test",
                cr_id=1,
                conclusion="failure",
                output_title="2 tests failed",
                output_text="--- FAIL: TestReconcile (0.05s)\n    expected nil, got err",
                details_url="https://github.com/org/repo/actions/runs/9000/jobs/1",
            ),
            _make_check_run(name="ci/lint", cr_id=2, conclusion="success"),
        ]
        resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 2},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)

        ci = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        assert ci.completed is True
        assert ci.overall_state == "failure"

        cat = mon.categorize_failure(ci)
        assert cat == CIFailureCategory.TEST_FAILURE

        details = mon.extract_failure_details(ci, cat)
        assert "ci/test" in details.failing_checks
        assert "TestReconcile" in details.failing_tests
        assert details.recommended_action == "remediate"

        d = details.to_dict()
        assert d["category"] == "test_failure"
        assert "TestReconcile" in d["failing_tests"]

    @pytest.mark.asyncio
    async def test_flake_rerun_pipeline(self) -> None:
        """Infrastructure flake → rerun pipeline."""
        mon = _monitor()
        runs = [
            _make_check_run(
                name="ci/test",
                cr_id=1,
                conclusion="failure",
                output_text="Error: the runner connection timed out",
                details_url="https://github.com/org/repo/actions/runs/8888/jobs/1",
            ),
        ]
        resp = {
            "success": True,
            "body": {"check_runs": runs, "total_count": 1},
            "status_code": 200,
        }
        mon._api_get = AsyncMock(return_value=resp)
        mon._api_post = AsyncMock(return_value={"success": True, "status_code": 201, "body": {}})

        ci = await mon.poll_ci_status("abc123", poll_interval=0, poll_timeout=10)
        cat = mon.categorize_failure(ci)
        assert cat == CIFailureCategory.INFRASTRUCTURE_FLAKE

        details = mon.extract_failure_details(ci, cat)
        assert details.recommended_action == "rerun"
        assert 8888 in ci.workflow_run_ids

        result = await mon.trigger_rerun(8888)
        assert result["success"] is True

    def test_ci_result_serialization_round_trip(self) -> None:
        cr = CheckRunResult(id=1, name="test", status="completed", conclusion="failure")
        ci = CIResult(
            sha="abc",
            overall_state="failure",
            check_runs=[cr],
            total_count=1,
            completed=True,
            workflow_run_ids=[9000],
            elapsed_seconds=42.0,
        )
        d = ci.to_dict()
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["sha"] == "abc"
        assert parsed["check_runs"][0]["name"] == "test"
        assert parsed["workflow_run_ids"] == [9000]

    def test_failure_details_serialization(self) -> None:
        fd = FailureDetails(
            category=CIFailureCategory.LINT_VIOLATION,
            summary="1 check failed",
            failing_checks=["lint"],
            error_messages=["ruff: E501"],
            recommended_action="remediate",
        )
        d = fd.to_dict()
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        assert parsed["category"] == "lint_violation"
        assert parsed["recommended_action"] == "remediate"
