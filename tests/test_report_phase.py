"""Tests for the Report Phase — visual report generation and artifact publishing.

Covers: ReportPhase class attributes, OODA cycle (observe, plan, act, validate,
reflect), integration with ReportPublisher, loop integration (execution snapshot
passing, _publish_reports fallback logic), error handling, and narration.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from engine.config import EngineConfig, LoopConfig
from engine.integrations.llm import MockProvider
from engine.loop import PHASE_ORDER, PipelineEngine
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.phases.base import REPORT_TOOLS, Phase, PhaseResult
from engine.phases.report import ReportPhase

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _make_phase(
    tmp_path: Path,
    *,
    execution_snapshot: dict[str, Any] | None = None,
    output_dir: str = "",
    config: EngineConfig | None = None,
) -> ReportPhase:
    """Construct a ReportPhase with sensible test defaults."""
    config = config or EngineConfig()
    logger = StructuredLogger(output_path=tmp_path / "log.json")
    tracer = Tracer()
    metrics = LoopMetrics()
    issue_data: dict[str, Any] = {"url": "https://github.com/test/repo/issues/1"}
    if execution_snapshot is not None:
        issue_data["_execution_snapshot"] = execution_snapshot
    if output_dir:
        issue_data["_output_dir"] = output_dir

    return ReportPhase(
        llm=MockProvider(responses=[]),
        logger=logger,
        tracer=tracer,
        repo_path=str(tmp_path / "repo"),
        issue_data=issue_data,
        config=config,
        metrics=metrics,
    )


def _minimal_execution_snapshot() -> dict[str, Any]:
    return {
        "execution": {
            "id": "test-exec-1",
            "started_at": "2026-03-25T10:00:00Z",
            "completed_at": "2026-03-25T10:05:00Z",
            "trigger": {"source_url": "https://github.com/test/repo/issues/1"},
            "target": {"repo_path": "/tmp/repo"},
            "config": {},
            "iterations": [
                {
                    "number": 1,
                    "phase": "triage",
                    "result": {"success": True},
                    "findings": {"classification": "bug"},
                }
            ],
            "result": {"status": "success", "total_iterations": 1},
            "metrics": {"total_llm_calls": 1},
            "actions": [],
        }
    }


# ==================================================================
# Class attributes
# ==================================================================


class TestReportPhaseAttributes:
    def test_phase_name(self):
        assert ReportPhase.name == "report"

    def test_allowed_tools(self):
        assert ReportPhase.allowed_tools == REPORT_TOOLS

    def test_get_allowed_tools_returns_report_tools(self):
        tools = ReportPhase.get_allowed_tools()
        assert sorted(tools) == sorted(REPORT_TOOLS)

    def test_is_subclass_of_phase(self):
        assert issubclass(ReportPhase, Phase)


# ==================================================================
# observe()
# ==================================================================


class TestObserve:
    @pytest.mark.asyncio
    async def test_observe_reports_config_flags(self, tmp_path):
        cfg = EngineConfig()
        cfg.reporting.decision_tree = True
        cfg.reporting.action_map = False
        cfg.reporting.comparison_mode = True
        phase = _make_phase(tmp_path, config=cfg)

        obs = await phase.observe()

        assert obs["decision_tree_enabled"] is True
        assert obs["action_map_enabled"] is False
        assert obs["comparison_mode"] is True

    @pytest.mark.asyncio
    async def test_observe_detects_execution_data_present(self, tmp_path):
        phase = _make_phase(tmp_path, execution_snapshot=_minimal_execution_snapshot())
        obs = await phase.observe()
        assert obs["has_execution_data"] is True

    @pytest.mark.asyncio
    async def test_observe_detects_execution_data_missing(self, tmp_path):
        phase = _make_phase(tmp_path)
        obs = await phase.observe()
        assert obs["has_execution_data"] is False

    @pytest.mark.asyncio
    async def test_observe_includes_output_dir(self, tmp_path):
        phase = _make_phase(tmp_path, output_dir="/some/output")
        obs = await phase.observe()
        assert obs["output_dir"] == "/some/output"


# ==================================================================
# plan()
# ==================================================================


class TestPlan:
    @pytest.mark.asyncio
    async def test_plan_lists_enabled_reports(self, tmp_path):
        phase = _make_phase(tmp_path)
        obs = {
            "decision_tree_enabled": True,
            "action_map_enabled": True,
            "comparison_mode": False,
            "has_execution_data": True,
            "output_dir": str(tmp_path),
        }
        plan = await phase.plan(obs)
        assert "decision_tree" in plan["reports_to_generate"]
        assert "action_map" in plan["reports_to_generate"]
        assert "comparison" not in plan["reports_to_generate"]

    @pytest.mark.asyncio
    async def test_plan_includes_comparison_when_enabled(self, tmp_path):
        phase = _make_phase(tmp_path)
        obs = {
            "decision_tree_enabled": False,
            "action_map_enabled": False,
            "comparison_mode": True,
            "has_execution_data": True,
            "output_dir": str(tmp_path),
        }
        plan = await phase.plan(obs)
        assert plan["reports_to_generate"] == ["comparison"]

    @pytest.mark.asyncio
    async def test_plan_empty_when_all_disabled(self, tmp_path):
        cfg = EngineConfig()
        cfg.reporting.decision_tree = False
        cfg.reporting.action_map = False
        cfg.reporting.comparison_mode = False
        phase = _make_phase(tmp_path, config=cfg)

        obs = await phase.observe()
        plan = await phase.plan(obs)
        assert plan["reports_to_generate"] == []


# ==================================================================
# act()
# ==================================================================


class TestAct:
    @pytest.mark.asyncio
    async def test_act_no_execution_snapshot(self, tmp_path):
        phase = _make_phase(tmp_path, output_dir=str(tmp_path))
        plan = {"execution_available": False, "output_dir": str(tmp_path)}

        result = await phase.act(plan)

        assert result["published"] is False
        assert "No execution snapshot" in result["reason"]

    @pytest.mark.asyncio
    async def test_act_no_output_dir(self, tmp_path):
        phase = _make_phase(
            tmp_path,
            execution_snapshot=_minimal_execution_snapshot(),
        )
        plan = {"execution_available": True, "output_dir": ""}
        result = await phase.act(plan)
        assert result["published"] is False
        assert "No output directory" in result["reason"]

    @pytest.mark.asyncio
    async def test_act_publisher_success(self, tmp_path):
        output_dir = tmp_path / "output"
        phase = _make_phase(
            tmp_path,
            execution_snapshot=_minimal_execution_snapshot(),
            output_dir=str(output_dir),
        )
        plan = {"execution_available": True, "output_dir": str(output_dir)}

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.files_generated = ["report.html", "summary.md"]
        mock_result.errors = []

        with patch("engine.visualization.publisher.ReportPublisher") as mock_cls:
            mock_cls.return_value.publish.return_value = mock_result
            result = await phase.act(plan)

        assert result["published"] is True
        assert result["files_generated"] == ["report.html", "summary.md"]
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_act_publisher_with_errors(self, tmp_path):
        output_dir = tmp_path / "output"
        phase = _make_phase(
            tmp_path,
            execution_snapshot=_minimal_execution_snapshot(),
            output_dir=str(output_dir),
        )
        plan = {"execution_available": True, "output_dir": str(output_dir)}

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.files_generated = ["summary.md"]
        mock_result.errors = ["Failed to generate report.html: template missing"]

        with patch("engine.visualization.publisher.ReportPublisher") as mock_cls:
            mock_cls.return_value.publish.return_value = mock_result
            result = await phase.act(plan)

        assert result["published"] is False
        assert len(result["errors"]) == 1

    @pytest.mark.asyncio
    async def test_act_publisher_exception(self, tmp_path):
        output_dir = tmp_path / "output"
        phase = _make_phase(
            tmp_path,
            execution_snapshot=_minimal_execution_snapshot(),
            output_dir=str(output_dir),
        )
        plan = {"execution_available": True, "output_dir": str(output_dir)}

        with patch("engine.visualization.publisher.ReportPublisher") as mock_cls:
            mock_cls.return_value.publish.side_effect = RuntimeError("boom")
            result = await phase.act(plan)

        assert result["published"] is False
        assert "boom" in result["reason"]

    @pytest.mark.asyncio
    async def test_act_import_error(self, tmp_path):
        output_dir = tmp_path / "output"
        phase = _make_phase(
            tmp_path,
            execution_snapshot=_minimal_execution_snapshot(),
            output_dir=str(output_dir),
        )
        plan = {"execution_available": True, "output_dir": str(output_dir)}

        with (
            patch.dict("sys.modules", {"engine.visualization.publisher": None}),
            patch("builtins.__import__", side_effect=ImportError("no module")),
        ):
            result = await phase.act(plan)

        assert result["published"] is False
        assert "not importable" in result.get("reason", "") or "not available" in result.get(
            "reason", ""
        )


# ==================================================================
# validate()
# ==================================================================


class TestValidate:
    @pytest.mark.asyncio
    async def test_validate_not_published_passthrough(self, tmp_path):
        phase = _make_phase(tmp_path)
        action_result = {"published": False, "reason": "test"}
        result = await phase.validate(action_result)
        assert result == action_result

    @pytest.mark.asyncio
    async def test_validate_all_files_exist(self, tmp_path):
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        (report_dir / "report.html").write_text("<html></html>")
        (report_dir / "summary.md").write_text("# Summary")
        (report_dir / "artifact-manifest.json").write_text("{}")

        phase = _make_phase(tmp_path)
        action_result = {
            "published": True,
            "report_dir": str(report_dir),
            "files_generated": [],
        }
        result = await phase.validate(action_result)
        assert result["missing_files"] == []

    @pytest.mark.asyncio
    async def test_validate_detects_missing_files(self, tmp_path):
        report_dir = tmp_path / "reports"
        report_dir.mkdir()
        (report_dir / "summary.md").write_text("# Summary")

        phase = _make_phase(tmp_path)
        action_result = {
            "published": True,
            "report_dir": str(report_dir),
            "files_generated": [],
        }
        result = await phase.validate(action_result)
        assert "report.html" in result["missing_files"]
        assert "artifact-manifest.json" in result["missing_files"]


# ==================================================================
# reflect()
# ==================================================================


class TestReflect:
    @pytest.mark.asyncio
    async def test_reflect_always_succeeds_on_publish(self, tmp_path):
        phase = _make_phase(tmp_path)
        validation = {
            "published": True,
            "files_generated": ["report.html"],
            "missing_files": [],
            "errors": [],
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert result.phase == "report"
        assert result.findings["published"] is True

    @pytest.mark.asyncio
    async def test_reflect_succeeds_even_on_failure(self, tmp_path):
        phase = _make_phase(tmp_path)
        validation = {
            "published": False,
            "files_generated": [],
            "reason": "no snapshot",
            "errors": ["something broke"],
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert result.findings["published"] is False
        assert result.findings["errors"] == ["something broke"]

    @pytest.mark.asyncio
    async def test_reflect_succeeds_with_missing_files(self, tmp_path):
        phase = _make_phase(tmp_path)
        validation = {
            "published": True,
            "files_generated": ["summary.md"],
            "missing_files": ["report.html"],
            "errors": [],
            "report_dir": "/tmp/reports",
        }
        result = await phase.reflect(validation)
        assert result.success is True
        assert result.artifacts["report_dir"] == "/tmp/reports"


# ==================================================================
# Full execute() cycle
# ==================================================================


class TestFullExecute:
    @pytest.mark.asyncio
    async def test_execute_no_snapshot(self, tmp_path):
        phase = _make_phase(tmp_path, output_dir=str(tmp_path))
        result = await phase.execute()
        assert result.success is True
        assert result.phase == "report"

    @pytest.mark.asyncio
    async def test_execute_with_publisher(self, tmp_path):
        output_dir = tmp_path / "output"
        phase = _make_phase(
            tmp_path,
            execution_snapshot=_minimal_execution_snapshot(),
            output_dir=str(output_dir),
        )

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.files_generated = ["report.html", "summary.md", "manifest.json"]
        mock_result.errors = []

        with patch("engine.visualization.publisher.ReportPublisher") as mock_cls:
            mock_cls.return_value.publish.return_value = mock_result
            result = await phase.execute()

        assert result.success is True
        assert result.findings["published"] is True
        assert result.findings["files_count"] == 3


# ==================================================================
# Loop integration
# ==================================================================


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
            return result

    return _Stub


def _success_result(phase: str, next_phase: str = "") -> PhaseResult:
    return PhaseResult(phase=phase, success=True, should_continue=True, next_phase=next_phase)


class TestLoopIntegration:
    @pytest.fixture
    def config(self):
        return EngineConfig(loop=LoopConfig(max_iterations=10, time_budget_minutes=5))

    @pytest.fixture
    def mock_llm(self):
        return MockProvider(responses=[])

    @pytest.fixture
    def output_dir(self, tmp_path):
        d = tmp_path / "output"
        d.mkdir()
        return d

    @pytest.fixture
    def tmp_repo(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        return repo

    @pytest.mark.asyncio
    async def test_report_phase_receives_execution_snapshot(
        self, tmp_repo, output_dir, config, mock_llm
    ):
        """When ReportPhase is registered, it receives _execution_snapshot in issue_data."""
        received_data: dict[str, Any] = {}

        class SpyReportPhase(ReportPhase):
            async def observe(self_inner) -> dict[str, Any]:
                received_data.update(self_inner.issue_data)
                return await super().observe()

        registry: dict[str, type[Phase]] = {}
        for i, name in enumerate(PHASE_ORDER[:-1]):
            next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
            registry[name] = _make_stub(name, _success_result(name, next_p))
        registry["report"] = SpyReportPhase

        loop = PipelineEngine(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )
        await loop.run()

        assert "_execution_snapshot" in received_data
        assert "_output_dir" in received_data
        assert received_data["_output_dir"] == str(output_dir)
        snapshot = received_data["_execution_snapshot"]
        assert "execution" in snapshot

    @pytest.mark.asyncio
    async def test_publish_reports_skipped_when_phase_published(
        self, tmp_repo, output_dir, config, mock_llm
    ):
        """_publish_reports() is skipped when report phase produced files."""

        class PublishingReportPhase(Phase):
            name = "report"
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
                    phase="report",
                    success=True,
                    should_continue=True,
                    artifacts={"files_generated": ["report.html"]},
                )

        registry: dict[str, type[Phase]] = {}
        for i, name in enumerate(PHASE_ORDER[:-1]):
            next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
            registry[name] = _make_stub(name, _success_result(name, next_p))
        registry["report"] = PublishingReportPhase

        loop = PipelineEngine(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )

        with patch.object(loop, "_publish_reports", wraps=loop._publish_reports) as spy:
            await loop.run()

        # _publish_reports was called but returned early (skipped actual generation)
        spy.assert_called_once()
        exec_json = json.loads((output_dir / "execution.json").read_text())
        report_iter = next(
            it for it in exec_json["execution"]["iterations"] if it["phase"] == "report"
        )
        assert report_iter["result"]["success"] is True

    @pytest.mark.asyncio
    async def test_publish_reports_fallback_when_no_report_phase(
        self, tmp_repo, output_dir, config, mock_llm
    ):
        """_publish_reports() runs normally when report phase is not registered."""
        registry: dict[str, type[Phase]] = {}
        for i, name in enumerate(PHASE_ORDER):
            next_p = PHASE_ORDER[i + 1] if i + 1 < len(PHASE_ORDER) else ""
            if name == "report":
                continue
            registry[name] = _make_stub(name, _success_result(name, next_p))

        loop = PipelineEngine(
            config=config,
            llm=mock_llm,
            issue_url="https://github.com/test/repo/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(output_dir),
            phase_registry=registry,
        )

        target = "engine.loop.PipelineEngine._publish_reports"
        with patch(target, wraps=loop._publish_reports) as spy:
            await loop.run()

        spy.assert_called_once()

    @pytest.mark.asyncio
    async def test_report_phase_in_phase_order(self):
        """report is the last entry in PHASE_ORDER."""
        assert "report" in PHASE_ORDER
        assert PHASE_ORDER[-1] == "report"

    @pytest.mark.asyncio
    async def test_report_phase_registered_in_cli(self):
        """__main__.py registers ReportPhase."""
        import engine.__main__ as cli_mod

        assert "ReportPhase" in dir(cli_mod)
        assert cli_mod.ReportPhase.name == "report"


# ==================================================================
# Narration
# ==================================================================


class TestNarration:
    @staticmethod
    def _messages(narrations: list[dict[str, Any]]) -> list[str]:
        return [n["message"].lower() for n in narrations]

    @pytest.mark.asyncio
    async def test_observe_narrates(self, tmp_path):
        phase = _make_phase(tmp_path)
        await phase.observe()
        msgs = self._messages(phase.logger._narrations)
        assert any("configuration" in m for m in msgs)

    @pytest.mark.asyncio
    async def test_plan_narrates(self, tmp_path):
        phase = _make_phase(tmp_path)
        obs = await phase.observe()
        await phase.plan(obs)
        msgs = self._messages(phase.logger._narrations)
        assert any("planning" in m or "report generation" in m for m in msgs)

    @pytest.mark.asyncio
    async def test_act_narrates_on_missing_snapshot(self, tmp_path):
        phase = _make_phase(tmp_path)
        plan = {"execution_available": False, "output_dir": str(tmp_path)}
        await phase.act(plan)
        msgs = self._messages(phase.logger._narrations)
        assert any("skip" in m or "no execution" in m for m in msgs)

    @pytest.mark.asyncio
    async def test_reflect_narrates(self, tmp_path):
        phase = _make_phase(tmp_path)
        validation = {"published": True, "files_generated": ["a.html"], "missing_files": []}
        await phase.reflect(validation)
        msgs = self._messages(phase.logger._narrations)
        assert any("report phase" in m for m in msgs)
