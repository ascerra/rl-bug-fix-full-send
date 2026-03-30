"""Tests for the observer reconstructor and cross-checker (Phase 8.1).

Covers:
- TimelineEvent / CrossCheckResult / CrossCheckReport / ModelInfo dataclasses
- ExecutionReconstructor: load_artifacts, build_timeline, extract_model_info,
  extract_prompt_digests, extract_tool_definitions, get_file_changes
- CrossChecker: all 5 cross-checks with pass and fail cases
- Malformed input handling
- Integration: reconstructor → cross-checker pipeline
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from engine.observer import CrossCheckReport, CrossCheckResult, ModelInfo, TimelineEvent
from engine.observer.cross_checker import CrossChecker, _extract_modified_files, _unique_ordered
from engine.observer.reconstructor import ExecutionReconstructor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_execution_data(
    actions: list[dict[str, Any]] | None = None,
    iterations: list[dict[str, Any]] | None = None,
    result: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal execution.json structure for tests."""
    return {
        "execution": {
            "id": "test-exec-001",
            "started_at": "2026-03-28T10:00:00Z",
            "completed_at": "2026-03-28T10:25:00Z",
            "trigger": {"type": "github_issue", "source_url": "https://github.com/o/r/issues/1"},
            "target": {"repo_path": "/tmp/repo"},
            "config": config if config is not None else {},
            "iterations": iterations
            if iterations is not None
            else [
                {
                    "number": 1,
                    "phase": "triage",
                    "started_at": "2026-03-28T10:00:00Z",
                    "completed_at": "2026-03-28T10:02:00Z",
                    "duration_ms": 120000,
                    "result": {"success": True},
                    "findings": {},
                },
                {
                    "number": 2,
                    "phase": "implement",
                    "started_at": "2026-03-28T10:02:00Z",
                    "completed_at": "2026-03-28T10:10:00Z",
                    "duration_ms": 480000,
                    "result": {"success": True},
                    "findings": {},
                },
                {
                    "number": 3,
                    "phase": "review",
                    "started_at": "2026-03-28T10:10:00Z",
                    "completed_at": "2026-03-28T10:12:00Z",
                    "duration_ms": 120000,
                    "result": {"success": True},
                    "findings": {},
                },
                {
                    "number": 4,
                    "phase": "validate",
                    "started_at": "2026-03-28T10:12:00Z",
                    "completed_at": "2026-03-28T10:15:00Z",
                    "duration_ms": 180000,
                    "result": {"success": True},
                    "findings": {},
                },
            ],
            "actions": actions
            if actions is not None
            else [
                {
                    "id": "a1",
                    "iteration": 1,
                    "phase": "triage",
                    "action_type": "llm_query",
                    "timestamp": "2026-03-28T10:00:30Z",
                    "input": {"description": "Classify issue", "context": {}},
                    "output": {"success": True, "data": {}},
                    "duration_ms": 2500,
                    "llm_context": {
                        "model": "gemini-2.5-pro",
                        "provider": "gemini",
                        "tokens_in": 5000,
                        "tokens_out": 1000,
                    },
                    "provenance": {},
                },
                {
                    "id": "a2",
                    "iteration": 2,
                    "phase": "implement",
                    "action_type": "file_write",
                    "timestamp": "2026-03-28T10:03:00Z",
                    "input": {
                        "description": "Write fix",
                        "context": {"path": "pkg/controller/reconciler.go"},
                    },
                    "output": {"success": True, "data": {}},
                    "duration_ms": 50,
                    "llm_context": {},
                    "provenance": {},
                },
                {
                    "id": "a3",
                    "iteration": 2,
                    "phase": "implement",
                    "action_type": "llm_query",
                    "timestamp": "2026-03-28T10:04:00Z",
                    "input": {"description": "Generate fix", "context": {}},
                    "output": {"success": True, "data": {}},
                    "duration_ms": 3000,
                    "llm_context": {
                        "model": "gemini-2.5-pro",
                        "provider": "gemini",
                        "tokens_in": 15000,
                        "tokens_out": 2000,
                    },
                    "provenance": {},
                },
                {
                    "id": "a4",
                    "iteration": 3,
                    "phase": "review",
                    "action_type": "llm_query",
                    "timestamp": "2026-03-28T10:10:30Z",
                    "input": {"description": "Review diff", "context": {}},
                    "output": {"success": True, "data": {}},
                    "duration_ms": 2000,
                    "llm_context": {
                        "model": "gemini-2.5-pro",
                        "provider": "gemini",
                        "tokens_in": 8000,
                        "tokens_out": 1500,
                    },
                    "provenance": {},
                },
                {
                    "id": "a5",
                    "iteration": 2,
                    "phase": "implement",
                    "action_type": "shell_run",
                    "timestamp": "2026-03-28T10:05:00Z",
                    "input": {"description": "Run go test ./...", "context": {}},
                    "output": {"success": True, "data": {}},
                    "duration_ms": 5000,
                    "llm_context": {},
                    "provenance": {},
                },
            ],
            "result": result or {"status": "success", "total_iterations": 4},
            "metrics": {
                "total_iterations": 4,
                "total_llm_calls": 3,
                "total_tokens_in": 28000,
                "total_tokens_out": 4500,
            },
        }
    }


@pytest.fixture
def exec_data() -> dict[str, Any]:
    return _make_execution_data()


@pytest.fixture
def artifacts_dir(tmp_path: Path, exec_data: dict[str, Any]) -> Path:
    """Write fixture artifacts to a temp directory."""
    (tmp_path / "execution.json").write_text(json.dumps(exec_data))
    (tmp_path / "log.json").write_text(
        json.dumps(
            [
                {"timestamp": "2026-03-28T10:00:00Z", "level": "INFO", "message": "Starting"},
                {"timestamp": "2026-03-28T10:25:00Z", "level": "INFO", "message": "Done"},
            ]
        )
    )
    transcripts_dir = tmp_path / "transcripts"
    transcripts_dir.mkdir()
    (transcripts_dir / "transcript-calls.json").write_text(
        json.dumps(
            [
                {"description": "Classify issue", "phase": "triage"},
                {"description": "Generate fix", "phase": "implement"},
                {"description": "Review diff", "phase": "review"},
            ]
        )
    )
    (tmp_path / "progress.md").write_text("# RL Engine Progress\n- Started\n- Done\n")
    return tmp_path


# ===========================================================================
# Dataclass tests
# ===========================================================================


class TestTimelineEvent:
    def test_to_dict(self):
        event = TimelineEvent(
            timestamp="2026-03-28T10:00:00Z",
            event_type="llm_call",
            phase="triage",
            iteration=1,
            description="Classify issue",
            details={"model": "gemini-2.5-pro"},
        )
        d = event.to_dict()
        assert d["event_type"] == "llm_call"
        assert d["phase"] == "triage"
        assert d["details"]["model"] == "gemini-2.5-pro"

    def test_defaults(self):
        event = TimelineEvent()
        d = event.to_dict()
        assert d["timestamp"] == ""
        assert d["event_type"] == ""
        assert d["details"] == {}


class TestCrossCheckResult:
    def test_to_dict(self):
        result = CrossCheckResult(
            check_name="diff_consistency",
            passed=True,
            details="All files match",
            evidence={"git_files": ["a.go"]},
        )
        d = result.to_dict()
        assert d["check_name"] == "diff_consistency"
        assert d["passed"] is True

    def test_defaults(self):
        r = CrossCheckResult()
        assert r.passed is False
        assert r.check_name == ""


class TestCrossCheckReport:
    def test_add_updates_all_passed(self):
        report = CrossCheckReport()
        report.add(CrossCheckResult(check_name="a", passed=True))
        assert report.all_passed is True

        report.add(CrossCheckResult(check_name="b", passed=False))
        assert report.all_passed is False

    def test_to_dict(self):
        report = CrossCheckReport()
        report.add(CrossCheckResult(check_name="x", passed=True, details="ok"))
        d = report.to_dict()
        assert len(d["checks"]) == 1
        assert d["all_passed"] is True

    def test_empty_report(self):
        report = CrossCheckReport()
        assert report.all_passed is False
        assert report.to_dict()["checks"] == []


class TestModelInfo:
    def test_to_dict(self):
        info = ModelInfo(
            model="gemini-2.5-pro",
            provider="gemini",
            total_calls=5,
            total_tokens_in=25000,
            total_tokens_out=5000,
        )
        d = info.to_dict()
        assert d["model"] == "gemini-2.5-pro"
        assert d["total_calls"] == 5

    def test_defaults(self):
        info = ModelInfo()
        assert info.total_calls == 0
        assert info.total_tokens_in == 0


# ===========================================================================
# Reconstructor tests
# ===========================================================================


class TestReconstructorLoadArtifacts:
    def test_loads_all_files(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)

        assert recon.execution_data.get("execution", {}).get("id") == "test-exec-001"
        assert len(recon.get_transcript_calls()) == 3
        assert "RL Engine Progress" in recon.get_progress_text()

    def test_missing_files_handled_gracefully(self, tmp_path: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(tmp_path)

        assert recon.execution_data == {}
        assert recon.get_transcript_calls() == []
        assert recon.get_progress_text() == ""

    def test_partial_artifacts(self, tmp_path: Path):
        (tmp_path / "execution.json").write_text(json.dumps({"execution": {"id": "partial"}}))
        recon = ExecutionReconstructor()
        recon.load_artifacts(tmp_path)

        assert recon.execution_data["execution"]["id"] == "partial"
        assert recon.get_transcript_calls() == []


class TestReconstructorBuildTimeline:
    def test_builds_timeline_from_execution(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()

        assert len(timeline) > 0
        types = {e.event_type for e in timeline}
        assert "phase_transition" in types
        assert "llm_call" in types
        assert "file_operation" in types
        assert "shell_command" in types

    def test_timeline_sorted_by_timestamp(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()

        timestamps = [e.timestamp for e in timeline if e.timestamp]
        assert timestamps == sorted(timestamps)

    def test_empty_execution(self):
        recon = ExecutionReconstructor()
        timeline = recon.build_timeline()
        assert timeline == []

    def test_phase_transitions_in_timeline(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()

        phase_events = [e for e in timeline if e.event_type == "phase_transition"]
        phases = [e.phase for e in phase_events]
        assert "triage" in phases
        assert "implement" in phases

    def test_event_type_mapping(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()

        llm_events = [e for e in timeline if e.event_type == "llm_call"]
        assert len(llm_events) == 3

        file_events = [e for e in timeline if e.event_type == "file_operation"]
        assert len(file_events) == 1

        shell_events = [e for e in timeline if e.event_type == "shell_command"]
        assert len(shell_events) == 1

    def test_flat_execution_data(self):
        """Handles execution data without the 'execution' wrapper."""
        recon = ExecutionReconstructor()
        recon._execution_data = {
            "id": "flat-test",
            "iterations": [
                {"number": 1, "phase": "triage", "started_at": "2026-03-28T10:00:00Z"},
            ],
            "actions": [],
        }
        timeline = recon.build_timeline()
        assert len(timeline) == 1
        assert timeline[0].phase == "triage"


class TestReconstructorExtractModelInfo:
    def test_extracts_deduplicated_models(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        models = recon.extract_model_info()

        assert len(models) == 1
        assert models[0].model == "gemini-2.5-pro"
        assert models[0].provider == "gemini"
        assert models[0].total_calls == 3
        assert models[0].total_tokens_in == 28000
        assert models[0].total_tokens_out == 4500

    def test_multiple_models(self):
        recon = ExecutionReconstructor()
        recon._execution_data = _make_execution_data(
            actions=[
                {
                    "id": "a1",
                    "action_type": "llm_query",
                    "timestamp": "T1",
                    "phase": "triage",
                    "iteration": 1,
                    "input": {"description": "call 1"},
                    "output": {},
                    "llm_context": {
                        "model": "gemini-2.5-pro",
                        "provider": "gemini",
                        "tokens_in": 1000,
                        "tokens_out": 200,
                    },
                },
                {
                    "id": "a2",
                    "action_type": "llm_query",
                    "timestamp": "T2",
                    "phase": "implement",
                    "iteration": 2,
                    "input": {"description": "call 2"},
                    "output": {},
                    "llm_context": {
                        "model": "claude-sonnet-4-20250514",
                        "provider": "anthropic",
                        "tokens_in": 2000,
                        "tokens_out": 500,
                    },
                },
            ]
        )
        models = recon.extract_model_info()
        assert len(models) == 2
        model_names = {m.model for m in models}
        assert "gemini-2.5-pro" in model_names
        assert "claude-sonnet-4-20250514" in model_names

    def test_no_llm_calls(self):
        recon = ExecutionReconstructor()
        recon._execution_data = _make_execution_data(actions=[])
        models = recon.extract_model_info()
        assert models == []


class TestReconstructorPromptDigests:
    def test_computes_digests(self, tmp_path: Path):
        prompts_dir = tmp_path / "templates" / "prompts"
        prompts_dir.mkdir(parents=True)
        (prompts_dir / "triage.md").write_text("You are a triage agent.")
        (prompts_dir / "implement.md").write_text("You are an implementation agent.")
        (prompts_dir / "readme.txt").write_text("Not a prompt")

        recon = ExecutionReconstructor()
        digests = recon.extract_prompt_digests(templates_dir=prompts_dir)

        assert len(digests) == 2
        assert "prompt://triage.md" in digests
        assert "prompt://implement.md" in digests
        assert all(v.startswith("sha256:") for v in digests.values())

    def test_missing_dir(self, tmp_path: Path):
        recon = ExecutionReconstructor()
        digests = recon.extract_prompt_digests(templates_dir=tmp_path / "nonexistent")
        assert digests == {}

    def test_digest_deterministic(self, tmp_path: Path):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "test.md").write_text("fixed content")

        recon = ExecutionReconstructor()
        d1 = recon.extract_prompt_digests(templates_dir=prompts_dir)
        d2 = recon.extract_prompt_digests(templates_dir=prompts_dir)
        assert d1 == d2


class TestReconstructorToolDefinitions:
    def test_extracts_tools(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        tools = recon.extract_tool_definitions()

        assert "tools" in tools
        assert "digest" in tools
        assert "file_write" in tools["tools"]
        assert "shell_run" in tools["tools"]
        assert "llm_query" not in tools["tools"]
        assert tools["digest"].startswith("sha256:")

    def test_empty_actions(self):
        recon = ExecutionReconstructor()
        recon._execution_data = _make_execution_data(actions=[])
        tools = recon.extract_tool_definitions()
        assert tools["tools"] == []

    def test_digest_deterministic(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        t1 = recon.extract_tool_definitions()
        t2 = recon.extract_tool_definitions()
        assert t1["digest"] == t2["digest"]


class TestReconstructorFileChanges:
    def test_extracts_file_changes(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        changes = recon.get_file_changes()

        assert len(changes) == 1
        assert changes[0]["path"] == "pkg/controller/reconciler.go"
        assert changes[0]["action_type"] == "file_write"

    def test_no_file_changes(self):
        recon = ExecutionReconstructor()
        recon._execution_data = _make_execution_data(
            actions=[
                {
                    "id": "a1",
                    "action_type": "llm_query",
                    "timestamp": "T1",
                    "phase": "triage",
                    "iteration": 1,
                    "input": {"description": "call"},
                    "output": {},
                    "llm_context": {},
                },
            ]
        )
        assert recon.get_file_changes() == []


class TestReconstructorMetadata:
    def test_get_execution_result(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        result = recon.get_execution_result()
        assert result["status"] == "success"

    def test_get_execution_config(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        config = recon.get_execution_config()
        assert isinstance(config, dict)

    def test_get_execution_metadata(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        meta = recon.get_execution_metadata()
        assert meta["id"] == "test-exec-001"
        assert meta["started_at"] == "2026-03-28T10:00:00Z"
        assert "trigger" in meta


# ===========================================================================
# Cross-checker tests
# ===========================================================================


class TestCrossCheckerDiffConsistency:
    def test_no_branch_dir(self):
        checker = CrossChecker()
        result = checker.check_diff_consistency([], {}, branch_dir=None)
        assert result.passed is True
        assert "Skipped" in result.details

    def test_not_a_git_repo(self, tmp_path: Path):
        checker = CrossChecker()
        result = checker.check_diff_consistency([], {}, branch_dir=tmp_path)
        assert result.passed is True
        assert "not a git repository" in result.details

    def test_consistent_diff(self, tmp_path: Path):
        """Create a real git repo with a commit and verify consistency."""
        import subprocess

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        (tmp_path / "initial.txt").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        (tmp_path / "fix.go").write_text("package main")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "fix"],
            cwd=str(tmp_path),
            capture_output=True,
        )

        exec_data = _make_execution_data(
            actions=[
                {
                    "id": "a1",
                    "action_type": "file_write",
                    "timestamp": "T1",
                    "phase": "implement",
                    "iteration": 1,
                    "input": {"description": "Write fix", "context": {"path": "fix.go"}},
                    "output": {},
                    "llm_context": {},
                },
            ]
        )

        checker = CrossChecker()
        result = checker.check_diff_consistency([], exec_data, branch_dir=tmp_path)
        assert result.passed is True

    def test_inconsistent_diff(self, tmp_path: Path):
        """Git shows a file not in the execution record."""
        import subprocess

        subprocess.run(["git", "init", str(tmp_path)], capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        (tmp_path / "initial.txt").write_text("initial")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=str(tmp_path),
            capture_output=True,
        )
        (tmp_path / "surprise.go").write_text("unexpected")
        subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "surprise"],
            cwd=str(tmp_path),
            capture_output=True,
        )

        exec_data = _make_execution_data(actions=[])
        checker = CrossChecker()
        result = checker.check_diff_consistency([], exec_data, branch_dir=tmp_path)
        assert result.passed is False
        assert "surprise.go" in result.details


class TestCrossCheckerActionCompleteness:
    def test_all_complete(self, exec_data: dict[str, Any]):
        checker = CrossChecker()
        recon = ExecutionReconstructor()
        recon._execution_data = exec_data
        timeline = recon.build_timeline()
        result = checker.check_action_completeness(timeline, exec_data)
        assert result.passed is True

    def test_no_file_actions(self):
        data = _make_execution_data(
            actions=[
                {
                    "id": "a1",
                    "action_type": "llm_query",
                    "timestamp": "T1",
                    "phase": "triage",
                    "iteration": 1,
                    "input": {"description": "call"},
                    "output": {},
                    "llm_context": {},
                },
            ]
        )
        checker = CrossChecker()
        result = checker.check_action_completeness([], data)
        assert result.passed is True
        assert "No file modification" in result.details

    def test_orphaned_file_action(self):
        data = _make_execution_data(
            actions=[
                {
                    "id": "orphan1",
                    "action_type": "file_write",
                    "timestamp": "T1",
                    "phase": "implement",
                    "iteration": 1,
                    "input": {"description": "", "context": {}},
                    "output": {},
                    "llm_context": {},
                },
            ]
        )
        checker = CrossChecker()
        result = checker.check_action_completeness([], data)
        assert result.passed is False
        assert result.evidence["orphaned"] == ["orphan1"]


class TestCrossCheckerPhaseOrdering:
    def test_valid_ordering(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()

        checker = CrossChecker()
        result = checker.check_phase_ordering(timeline)
        assert result.passed is True

    def test_no_phase_events(self):
        checker = CrossChecker()
        result = checker.check_phase_ordering([])
        assert result.passed is True

    def test_allows_implement_review_backtrack(self):
        events = [
            TimelineEvent(event_type="phase_transition", phase="triage", timestamp="T1"),
            TimelineEvent(event_type="phase_transition", phase="implement", timestamp="T2"),
            TimelineEvent(event_type="phase_transition", phase="review", timestamp="T3"),
            TimelineEvent(event_type="phase_transition", phase="implement", timestamp="T4"),
            TimelineEvent(event_type="phase_transition", phase="review", timestamp="T5"),
            TimelineEvent(event_type="phase_transition", phase="validate", timestamp="T6"),
        ]
        checker = CrossChecker()
        result = checker.check_phase_ordering(events)
        assert result.passed is True

    def test_detects_unexpected_backward_transition(self):
        events = [
            TimelineEvent(event_type="phase_transition", phase="triage", timestamp="T1"),
            TimelineEvent(event_type="phase_transition", phase="implement", timestamp="T2"),
            TimelineEvent(event_type="phase_transition", phase="review", timestamp="T3"),
            TimelineEvent(event_type="phase_transition", phase="validate", timestamp="T4"),
            TimelineEvent(event_type="phase_transition", phase="triage", timestamp="T5"),
        ]
        checker = CrossChecker()
        result = checker.check_phase_ordering(events)
        assert result.passed is False
        has_violation = (
            "backward transition" in result.details.lower()
            or len(result.evidence["violations"]) > 0
        )
        assert has_violation

    def test_unknown_phase(self):
        events = [
            TimelineEvent(event_type="phase_transition", phase="mystery", timestamp="T1"),
        ]
        checker = CrossChecker()
        result = checker.check_phase_ordering(events)
        assert result.passed is False
        assert "Unknown phase" in result.details


class TestCrossCheckerTokenPlausibility:
    def test_plausible_tokens(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()

        checker = CrossChecker()
        result = checker.check_token_plausibility(timeline)
        assert result.passed is True

    def test_no_llm_calls(self):
        checker = CrossChecker()
        result = checker.check_token_plausibility([])
        assert result.passed is True

    def test_negative_tokens(self):
        events = [
            TimelineEvent(
                event_type="llm_call",
                timestamp="T1",
                description="bad call",
                details={"llm_context": {"tokens_in": -100, "tokens_out": 50}},
            ),
        ]
        checker = CrossChecker()
        result = checker.check_token_plausibility(events)
        assert result.passed is False
        assert "Negative" in result.details

    def test_implausible_tokens_out(self):
        events = [
            TimelineEvent(
                event_type="llm_call",
                timestamp="T1",
                description="huge output",
                details={"llm_context": {"tokens_in": 1000, "tokens_out": 999_999_999}},
            ),
        ]
        checker = CrossChecker()
        result = checker.check_token_plausibility(events)
        assert result.passed is False
        assert "Implausible" in result.details

    def test_non_numeric_tokens(self):
        events = [
            TimelineEvent(
                event_type="llm_call",
                timestamp="T1",
                description="bad type",
                details={"llm_context": {"tokens_in": "many", "tokens_out": 50}},
            ),
        ]
        checker = CrossChecker()
        result = checker.check_token_plausibility(events)
        assert result.passed is False
        assert "Non-numeric" in result.details

    def test_zero_tokens_valid(self):
        events = [
            TimelineEvent(
                event_type="llm_call",
                timestamp="T1",
                description="zero call",
                details={"llm_context": {"tokens_in": 0, "tokens_out": 0}},
            ),
        ]
        checker = CrossChecker()
        result = checker.check_token_plausibility(events)
        assert result.passed is True


class TestCrossCheckerToolCallIntegrity:
    def test_all_matched(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()
        calls = recon.get_transcript_calls()

        checker = CrossChecker()
        result = checker.check_tool_call_integrity(timeline, calls)
        assert result.passed is True

    def test_no_transcripts(self):
        checker = CrossChecker()
        result = checker.check_tool_call_integrity([], None)
        assert result.passed is True

    def test_unmatched_transcript(self):
        events = [
            TimelineEvent(
                event_type="llm_call",
                timestamp="T1",
                description="Classify issue",
            ),
        ]
        calls = [
            {"description": "Classify issue", "phase": "triage"},
            {"description": "Ghost call", "phase": "implement"},
        ]
        checker = CrossChecker()
        result = checker.check_tool_call_integrity(events, calls)
        assert result.passed is False
        assert "Ghost call" in str(result.evidence["unmatched_descriptions"])

    def test_empty_descriptions_ignored(self):
        events = [
            TimelineEvent(event_type="llm_call", timestamp="T1", description="call"),
        ]
        calls = [
            {"description": "call"},
            {"description": ""},
        ]
        checker = CrossChecker()
        result = checker.check_tool_call_integrity(events, calls)
        assert result.passed is True


class TestCrossCheckerRunAllChecks:
    def test_all_pass(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()
        calls = recon.get_transcript_calls()

        checker = CrossChecker()
        report = checker.run_all_checks(
            timeline=timeline,
            execution_data=recon.execution_data,
            transcript_calls=calls,
        )
        assert report.all_passed is True
        assert len(report.checks) == 5
        check_names = {c.check_name for c in report.checks}
        assert check_names == {
            "diff_consistency",
            "action_completeness",
            "phase_ordering",
            "token_plausibility",
            "tool_call_integrity",
        }

    def test_mixed_results(self):
        events = [
            TimelineEvent(
                event_type="llm_call",
                timestamp="T1",
                description="call",
                details={"llm_context": {"tokens_in": -5, "tokens_out": 10}},
            ),
        ]
        checker = CrossChecker()
        report = checker.run_all_checks(
            timeline=events,
            execution_data=_make_execution_data(actions=[]),
        )
        assert report.all_passed is False
        failed = [c for c in report.checks if not c.passed]
        assert len(failed) >= 1


# ===========================================================================
# Helper function tests
# ===========================================================================


class TestHelpers:
    def test_extract_modified_files(self):
        data = _make_execution_data(
            actions=[
                {
                    "id": "a1",
                    "action_type": "file_write",
                    "timestamp": "T1",
                    "phase": "implement",
                    "iteration": 1,
                    "input": {"description": "Write", "context": {"path": "src/main.go"}},
                    "output": {},
                    "llm_context": {},
                },
                {
                    "id": "a2",
                    "action_type": "file_edit",
                    "timestamp": "T2",
                    "phase": "implement",
                    "iteration": 1,
                    "input": {"description": "Edit", "context": {"path": "src/util.go"}},
                    "output": {},
                    "llm_context": {},
                },
            ]
        )
        files = _extract_modified_files(data)
        assert "src/main.go" in files
        assert "src/util.go" in files

    def test_extract_modified_files_empty(self):
        files = _extract_modified_files({"execution": {"actions": []}})
        assert files == set()

    def test_unique_ordered(self):
        assert _unique_ordered(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
        assert _unique_ordered([]) == []
        assert _unique_ordered(["x"]) == ["x"]


# ===========================================================================
# Integration: reconstructor → cross-checker
# ===========================================================================


class TestIntegration:
    def test_full_pipeline(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)

        timeline = recon.build_timeline()
        assert len(timeline) > 0

        models = recon.extract_model_info()
        assert len(models) >= 1

        tools = recon.extract_tool_definitions()
        assert len(tools["tools"]) >= 1

        checker = CrossChecker()
        report = checker.run_all_checks(
            timeline=timeline,
            execution_data=recon.execution_data,
            transcript_calls=recon.get_transcript_calls(),
        )
        assert report.all_passed is True
        assert len(report.checks) == 5

    def test_pipeline_with_malformed_execution(self, tmp_path: Path):
        (tmp_path / "execution.json").write_text(json.dumps({"garbage": True}))

        recon = ExecutionReconstructor()
        recon.load_artifacts(tmp_path)

        timeline = recon.build_timeline()
        assert timeline == []

        models = recon.extract_model_info()
        assert models == []

        checker = CrossChecker()
        report = checker.run_all_checks(
            timeline=timeline,
            execution_data=recon.execution_data,
        )
        assert len(report.checks) == 5

    def test_report_serialization_roundtrip(self, artifacts_dir: Path):
        recon = ExecutionReconstructor()
        recon.load_artifacts(artifacts_dir)
        timeline = recon.build_timeline()

        checker = CrossChecker()
        report = checker.run_all_checks(
            timeline=timeline,
            execution_data=recon.execution_data,
            transcript_calls=recon.get_transcript_calls(),
        )
        d = report.to_dict()
        assert isinstance(json.dumps(d), str)
        assert d["all_passed"] is True
