"""Tests for workflow self-monitoring — WorkflowMonitor, health checks, loop integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock, patch

import pytest

from engine.config import EngineConfig
from engine.integrations.llm import MockProvider
from engine.loop import PHASE_ORDER, RalphLoop
from engine.observability.logger import StructuredLogger
from engine.phases.base import Phase, PhaseResult
from engine.workflow.monitor import (
    CI_TIMEOUT_BUFFER_MINUTES,
    HealthCheck,
    StepFailure,
    WorkflowContext,
    WorkflowMonitor,
    recommended_workflow_timeout,
)

# ------------------------------------------------------------------
# Dataclass serialization
# ------------------------------------------------------------------


class TestStepFailure:
    def test_to_dict(self):
        sf = StepFailure(
            name="Clone repo",
            conclusion="failure",
            number=3,
            started_at="2026-03-25T10:00:00Z",
            completed_at="2026-03-25T10:01:00Z",
            log_excerpt="fatal: repository not found",
        )
        d = sf.to_dict()
        assert d["name"] == "Clone repo"
        assert d["conclusion"] == "failure"
        assert d["number"] == 3
        assert d["log_excerpt"] == "fatal: repository not found"

    def test_defaults(self):
        sf = StepFailure(name="test", conclusion="failure", number=1)
        d = sf.to_dict()
        assert d["started_at"] == ""
        assert d["completed_at"] == ""
        assert d["log_excerpt"] == ""


class TestWorkflowContext:
    def test_to_dict(self):
        ctx = WorkflowContext(
            is_ci=True,
            repository="org/repo",
            run_id="12345",
            run_url="https://github.com/org/repo/actions/runs/12345",
            job_name="run-ralph-loop",
        )
        d = ctx.to_dict()
        assert d["is_ci"] is True
        assert d["repository"] == "org/repo"
        assert d["run_id"] == "12345"
        assert d["run_url"] == "https://github.com/org/repo/actions/runs/12345"

    def test_defaults(self):
        ctx = WorkflowContext()
        d = ctx.to_dict()
        assert d["is_ci"] is False
        assert d["repository"] == ""
        assert d["run_id"] == ""


class TestHealthCheck:
    def test_healthy(self):
        hc = HealthCheck(healthy=True, run_status="in_progress")
        d = hc.to_dict()
        assert d["healthy"] is True
        assert d["run_status"] == "in_progress"
        assert d["failed_steps"] == []

    def test_unhealthy(self):
        sf = StepFailure(name="Install deps", conclusion="failure", number=2)
        hc = HealthCheck(
            healthy=False,
            run_status="in_progress",
            failed_steps=[sf],
        )
        d = hc.to_dict()
        assert d["healthy"] is False
        assert len(d["failed_steps"]) == 1
        assert d["failed_steps"][0]["name"] == "Install deps"


# ------------------------------------------------------------------
# WorkflowMonitor construction
# ------------------------------------------------------------------


class TestWorkflowMonitorInit:
    def test_basic_init(self):
        monitor = WorkflowMonitor(
            token="ghp_test",
            repository="org/repo",
            run_id="12345",
        )
        assert monitor.is_github_actions is True
        assert monitor._repository == "org/repo"
        assert monitor._run_id == "12345"

    def test_run_url(self):
        monitor = WorkflowMonitor(
            token="ghp_test",
            repository="org/repo",
            run_id="12345",
        )
        assert monitor.run_url == "https://github.com/org/repo/actions/runs/12345"

    def test_run_url_custom_server(self):
        monitor = WorkflowMonitor(
            token="ghp_test",
            repository="org/repo",
            run_id="12345",
            server_url="https://github.example.com",
        )
        assert monitor.run_url == "https://github.example.com/org/repo/actions/runs/12345"

    def test_context_property(self):
        monitor = WorkflowMonitor(
            token="ghp_test",
            repository="org/repo",
            run_id="12345",
            job_name="run-ralph-loop",
            actor="testbot",
            ref="refs/heads/main",
            sha="abc123",
            workflow="ralph-loop.yml",
            event_name="workflow_dispatch",
        )
        ctx = monitor.context
        assert ctx.is_ci is True
        assert ctx.repository == "org/repo"
        assert ctx.run_id == "12345"
        assert ctx.job_name == "run-ralph-loop"
        assert ctx.actor == "testbot"
        assert ctx.ref == "refs/heads/main"
        assert ctx.sha == "abc123"
        assert ctx.workflow == "ralph-loop.yml"
        assert ctx.event_name == "workflow_dispatch"


# ------------------------------------------------------------------
# from_environment
# ------------------------------------------------------------------


class TestFromEnvironment:
    def test_returns_none_outside_ci(self):
        with patch.dict("os.environ", {}, clear=True):
            assert WorkflowMonitor.from_environment() is None

    def test_returns_none_when_github_actions_false(self):
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "false"}, clear=True):
            assert WorkflowMonitor.from_environment() is None

    def test_returns_none_missing_token(self):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_RUN_ID": "12345",
        }
        with patch.dict("os.environ", env, clear=True):
            assert WorkflowMonitor.from_environment() is None

    def test_returns_none_missing_repository(self):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_test",
            "GITHUB_RUN_ID": "12345",
        }
        with patch.dict("os.environ", env, clear=True):
            assert WorkflowMonitor.from_environment() is None

    def test_returns_none_missing_run_id(self):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_test",
            "GITHUB_REPOSITORY": "org/repo",
        }
        with patch.dict("os.environ", env, clear=True):
            assert WorkflowMonitor.from_environment() is None

    def test_creates_from_github_token(self):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_test",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_RUN_ID": "12345",
            "GITHUB_JOB": "run-ralph-loop",
            "GITHUB_RUN_NUMBER": "7",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_ACTOR": "testbot",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "abc123",
            "GITHUB_WORKFLOW": "Ralph Loop",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_SERVER_URL": "https://github.com",
        }
        with patch.dict("os.environ", env, clear=True):
            monitor = WorkflowMonitor.from_environment()
            assert monitor is not None
            assert monitor._repository == "org/repo"
            assert monitor._run_id == "12345"
            assert monitor._job_name == "run-ralph-loop"
            assert monitor._actor == "testbot"

    def test_creates_from_gh_pat(self):
        env = {
            "GITHUB_ACTIONS": "true",
            "GH_PAT": "ghp_pat_test",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_RUN_ID": "12345",
        }
        with patch.dict("os.environ", env, clear=True):
            monitor = WorkflowMonitor.from_environment()
            assert monitor is not None
            assert monitor._token == "ghp_pat_test"

    def test_logs_warning_on_missing_vars(self):
        logger = StructuredLogger()
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "org/repo",
        }
        with patch.dict("os.environ", env, clear=True):
            result = WorkflowMonitor.from_environment(logger=logger)
            assert result is None
            entries = logger.get_entries()
            warns = [e for e in entries if e["level"] == "WARN"]
            assert len(warns) == 1
            assert "self-monitoring disabled" in warns[0]["message"]


# ------------------------------------------------------------------
# API methods (mocked)
# ------------------------------------------------------------------


def _make_monitor(**kwargs: Any) -> WorkflowMonitor:
    defaults = {
        "token": "ghp_test",
        "repository": "org/repo",
        "run_id": "12345",
    }
    defaults.update(kwargs)
    return WorkflowMonitor(**defaults)


class TestGetRunStatus:
    @pytest.mark.asyncio
    async def test_success(self):
        monitor = _make_monitor()
        mock_response = {
            "success": True,
            "status_code": 200,
            "body": {"id": 12345, "status": "in_progress", "conclusion": None},
        }
        monitor._api_get = AsyncMock(return_value=mock_response)
        result = await monitor.get_run_status()
        assert result["success"] is True
        assert result["body"]["status"] == "in_progress"
        monitor._api_get.assert_called_once_with("/repos/org/repo/actions/runs/12345")

    @pytest.mark.asyncio
    async def test_failure(self):
        monitor = _make_monitor()
        monitor._api_get = AsyncMock(return_value={"success": False, "error": "HTTP 404"})
        result = await monitor.get_run_status()
        assert result["success"] is False


class TestGetJobs:
    @pytest.mark.asyncio
    async def test_returns_jobs_list(self):
        monitor = _make_monitor()
        mock_response = {
            "success": True,
            "status_code": 200,
            "body": {
                "total_count": 1,
                "jobs": [
                    {
                        "id": 999,
                        "name": "Execute Ralph Loop",
                        "status": "in_progress",
                        "steps": [
                            {"name": "Checkout", "number": 1, "conclusion": "success"},
                            {"name": "Setup Python", "number": 2, "conclusion": "success"},
                        ],
                    }
                ],
            },
        }
        monitor._api_get = AsyncMock(return_value=mock_response)
        jobs = await monitor.get_jobs()
        assert len(jobs) == 1
        assert jobs[0]["name"] == "Execute Ralph Loop"

    @pytest.mark.asyncio
    async def test_returns_empty_on_failure(self):
        monitor = _make_monitor()
        monitor._api_get = AsyncMock(return_value={"success": False, "error": "timeout"})
        jobs = await monitor.get_jobs()
        assert jobs == []


class TestGetFailedSteps:
    @pytest.mark.asyncio
    async def test_no_failures(self):
        monitor = _make_monitor()
        monitor.get_jobs = AsyncMock(
            return_value=[
                {
                    "id": 999,
                    "steps": [
                        {"name": "Checkout", "number": 1, "conclusion": "success"},
                        {"name": "Setup Python", "number": 2, "conclusion": "success"},
                    ],
                }
            ]
        )
        failures = await monitor.get_failed_steps()
        assert failures == []

    @pytest.mark.asyncio
    async def test_with_failures(self):
        monitor = _make_monitor()
        monitor.get_jobs = AsyncMock(
            return_value=[
                {
                    "id": 999,
                    "steps": [
                        {"name": "Checkout", "number": 1, "conclusion": "success"},
                        {
                            "name": "Clone target",
                            "number": 2,
                            "conclusion": "failure",
                            "started_at": "2026-03-25T10:00:00Z",
                            "completed_at": "2026-03-25T10:01:00Z",
                        },
                    ],
                }
            ]
        )
        failures = await monitor.get_failed_steps()
        assert len(failures) == 1
        assert failures[0].name == "Clone target"
        assert failures[0].conclusion == "failure"
        assert failures[0].number == 2

    @pytest.mark.asyncio
    async def test_cancelled_counted_as_failure(self):
        monitor = _make_monitor()
        monitor.get_jobs = AsyncMock(
            return_value=[
                {
                    "id": 999,
                    "steps": [
                        {"name": "Long step", "number": 1, "conclusion": "cancelled"},
                    ],
                }
            ]
        )
        failures = await monitor.get_failed_steps()
        assert len(failures) == 1
        assert failures[0].conclusion == "cancelled"

    @pytest.mark.asyncio
    async def test_multiple_jobs(self):
        monitor = _make_monitor()
        monitor.get_jobs = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "steps": [
                        {"name": "Step A", "number": 1, "conclusion": "failure"},
                    ],
                },
                {
                    "id": 2,
                    "steps": [
                        {"name": "Step B", "number": 1, "conclusion": "success"},
                        {"name": "Step C", "number": 2, "conclusion": "failure"},
                    ],
                },
            ]
        )
        failures = await monitor.get_failed_steps()
        assert len(failures) == 2
        names = {f.name for f in failures}
        assert names == {"Step A", "Step C"}


class TestGetJobLog:
    @pytest.mark.asyncio
    async def test_success(self):
        monitor = _make_monitor()
        monitor._api_get = AsyncMock(return_value={"success": True, "body": "log output here"})
        log = await monitor.get_job_log(999)
        assert log == "log output here"

    @pytest.mark.asyncio
    async def test_failure(self):
        monitor = _make_monitor()
        monitor._api_get = AsyncMock(return_value={"success": False, "error": "HTTP 404"})
        log = await monitor.get_job_log(999)
        assert "[failed to fetch log" in log


class TestCheckHealth:
    @pytest.mark.asyncio
    async def test_healthy(self):
        monitor = _make_monitor()
        monitor.get_run_status = AsyncMock(
            return_value={
                "success": True,
                "body": {"status": "in_progress", "conclusion": None},
            }
        )
        monitor.get_failed_steps = AsyncMock(return_value=[])
        health = await monitor.check_health()
        assert health.healthy is True
        assert health.run_status == "in_progress"
        assert health.failed_steps == []
        assert health.context.is_ci is True

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        monitor = _make_monitor()
        monitor.get_run_status = AsyncMock(
            return_value={
                "success": True,
                "body": {"status": "in_progress"},
            }
        )
        sf = StepFailure(name="Clone repo", conclusion="failure", number=3)
        monitor.get_failed_steps = AsyncMock(return_value=[sf])
        health = await monitor.check_health()
        assert health.healthy is False
        assert len(health.failed_steps) == 1

    @pytest.mark.asyncio
    async def test_logs_healthy(self):
        logger = StructuredLogger()
        monitor = _make_monitor(logger=logger)
        monitor.get_run_status = AsyncMock(
            return_value={"success": True, "body": {"status": "in_progress"}}
        )
        monitor.get_failed_steps = AsyncMock(return_value=[])
        await monitor.check_health()
        entries = logger.get_entries()
        debugs = [e for e in entries if e["level"] == "DEBUG"]
        assert any("healthy" in e["message"] for e in debugs)

    @pytest.mark.asyncio
    async def test_logs_unhealthy(self):
        logger = StructuredLogger()
        monitor = _make_monitor(logger=logger)
        monitor.get_run_status = AsyncMock(
            return_value={"success": True, "body": {"status": "in_progress"}}
        )
        sf = StepFailure(name="Bad step", conclusion="failure", number=1)
        monitor.get_failed_steps = AsyncMock(return_value=[sf])
        await monitor.check_health()
        entries = logger.get_entries()
        warns = [e for e in entries if e["level"] == "WARN"]
        assert any("failed step" in e["message"] for e in warns)

    @pytest.mark.asyncio
    async def test_run_status_api_failure(self):
        monitor = _make_monitor()
        monitor.get_run_status = AsyncMock(return_value={"success": False, "error": "timeout"})
        monitor.get_failed_steps = AsyncMock(return_value=[])
        health = await monitor.check_health()
        assert health.healthy is True
        assert health.run_status == ""


# ------------------------------------------------------------------
# recommended_workflow_timeout
# ------------------------------------------------------------------


class TestRecommendedTimeout:
    def test_default_budget(self):
        assert recommended_workflow_timeout(30) == 30 + CI_TIMEOUT_BUFFER_MINUTES

    def test_custom_budget(self):
        assert recommended_workflow_timeout(60) == 60 + CI_TIMEOUT_BUFFER_MINUTES

    def test_zero_budget(self):
        assert recommended_workflow_timeout(0) == CI_TIMEOUT_BUFFER_MINUTES


# ------------------------------------------------------------------
# Loop integration
# ------------------------------------------------------------------


def _success_result(phase: str, next_phase: str = "") -> PhaseResult:
    return PhaseResult(phase=phase, success=True, should_continue=True, next_phase=next_phase)


def _make_stub(phase_name: str, result: PhaseResult) -> type[Phase]:
    class _Stub(Phase):
        name = phase_name
        allowed_tools: ClassVar[list[str]] = []

        async def observe(self) -> dict[str, Any]:
            return {}

        async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
            return {}

        async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
            return PhaseResult(
                phase=result.phase or phase_name,
                success=result.success,
                should_continue=result.should_continue,
                next_phase=result.next_phase,
                escalate=result.escalate,
                escalation_reason=result.escalation_reason,
                findings=dict(result.findings),
                artifacts=dict(result.artifacts),
            )

    return _Stub


def _all_success_registry() -> dict[str, type[Phase]]:
    registry: dict[str, type[Phase]] = {}
    for i, name in enumerate(PHASE_ORDER):
        next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
        registry[name] = _make_stub(name, _success_result(name, next_p))
    return registry


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.fixture()
def output_dir(tmp_path: Path) -> Path:
    out = tmp_path / "output"
    out.mkdir()
    return out


class TestLoopWithMonitor:
    @pytest.mark.asyncio
    async def test_loop_with_monitor_records_context(self, tmp_repo, output_dir):
        monitor = _make_monitor()
        monitor.check_health = AsyncMock(
            return_value=HealthCheck(
                healthy=True,
                run_status="in_progress",
                context=monitor.context,
            )
        )

        loop = RalphLoop(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
            workflow_monitor=monitor,
        )
        execution = await loop.run()

        assert "workflow" in execution.target
        assert execution.target["workflow"]["is_ci"] is True
        assert execution.target["workflow"]["repository"] == "org/repo"

    @pytest.mark.asyncio
    async def test_loop_without_monitor_no_workflow_context(self, tmp_repo, output_dir):
        loop = RalphLoop(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
        )
        execution = await loop.run()

        assert "workflow" not in execution.target

    @pytest.mark.asyncio
    async def test_loop_health_check_called_each_iteration(self, tmp_repo, output_dir):
        monitor = _make_monitor()
        monitor.check_health = AsyncMock(
            return_value=HealthCheck(healthy=True, run_status="in_progress")
        )

        loop = RalphLoop(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
            workflow_monitor=monitor,
        )
        execution = await loop.run()

        assert execution.result["status"] == "success"
        assert monitor.check_health.call_count == len(PHASE_ORDER)

    @pytest.mark.asyncio
    async def test_loop_records_unhealthy_step(self, tmp_repo, output_dir):
        sf = StepFailure(name="Clone target", conclusion="failure", number=3)
        monitor = _make_monitor()
        monitor.check_health = AsyncMock(
            return_value=HealthCheck(
                healthy=False,
                run_status="in_progress",
                failed_steps=[sf],
            )
        )

        loop = RalphLoop(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
            workflow_monitor=monitor,
        )
        execution = await loop.run()

        health_actions = [
            a for a in execution.actions if a["action_type"] == "workflow_health_check"
        ]
        assert len(health_actions) >= 1
        assert health_actions[0]["output"]["success"] is False

    @pytest.mark.asyncio
    async def test_loop_survives_health_check_exception(self, tmp_repo, output_dir):
        monitor = _make_monitor()
        monitor.check_health = AsyncMock(side_effect=Exception("API unreachable"))

        loop = RalphLoop(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
            workflow_monitor=monitor,
        )
        execution = await loop.run()

        assert execution.result["status"] == "success"

    @pytest.mark.asyncio
    async def test_execution_json_contains_workflow(self, tmp_repo, output_dir):
        monitor = _make_monitor()
        monitor.check_health = AsyncMock(
            return_value=HealthCheck(healthy=True, run_status="in_progress")
        )

        loop = RalphLoop(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=_all_success_registry(),
            workflow_monitor=monitor,
        )
        await loop.run()

        execution_file = output_dir / "execution.json"
        data = json.loads(execution_file.read_text())
        assert "workflow" in data["execution"]["target"]
        wf = data["execution"]["target"]["workflow"]
        assert wf["is_ci"] is True
        assert wf["run_id"] == "12345"


# ------------------------------------------------------------------
# CLI integration
# ------------------------------------------------------------------


class TestCLIMonitorIntegration:
    def test_from_environment_not_in_ci(self):
        with patch.dict("os.environ", {}, clear=True):
            monitor = WorkflowMonitor.from_environment()
            assert monitor is None

    def test_from_environment_in_ci(self):
        env = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_test",
            "GITHUB_REPOSITORY": "org/repo",
            "GITHUB_RUN_ID": "42",
        }
        with patch.dict("os.environ", env, clear=True):
            monitor = WorkflowMonitor.from_environment()
            assert monitor is not None
            assert monitor._run_id == "42"
