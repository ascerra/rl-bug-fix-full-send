"""End-to-end tests — full pipeline against simulated known-solved bugs.

Phase 5.3: Tests the entire Ralph Loop pipeline from issue through to PR attempt,
using MockProvider with realistic phase responses. Simulates three Konflux-style
bugs: nil pointer in Go controller, Python import error, YAML config parsing error.

Each test creates a real git repo, configures MockProvider with phase-specific
JSON responses, registers all real phase implementations, and runs the full
RalphLoop end-to-end. Tests verify: all phases run in order, execution record
is complete, metrics are populated, reports are generated, comparison mode works,
and execution completes within time budget.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from engine.config import EngineConfig, LoopConfig
from engine.integrations.llm import MockProvider
from engine.loop import PHASE_ORDER, RalphLoop
from engine.phases.implement import ImplementPhase
from engine.phases.review import ReviewPhase
from engine.phases.triage import TriagePhase
from engine.phases.validate import ValidatePhase
from engine.visualization.comparison import build_comparison
from engine.visualization.report_generator import ReportGenerator, extract_report_data

# ====================================================================
# Bug scenario fixtures — simulated known-solved Konflux-style bugs
# ====================================================================

_TRIAGE_BASE = {
    "injection_detected": False,
    "recommendation": "proceed",
}

_REVIEW_APPROVE = {
    "verdict": "approve",
    "findings": [],
    "scope_assessment": "bug_fix",
    "injection_detected": False,
}

_VALIDATE_READY = {
    "tests_passing": True,
    "linters_passing": True,
    "lint_issues": [],
    "diff_is_minimal": True,
    "unnecessary_changes": [],
    "ready_to_submit": True,
    "blocking_issues": [],
}


NIL_POINTER_BUG: dict[str, Any] = {
    "name": "nil-pointer-go-controller",
    "issue_url": "https://github.com/konflux-ci/build-service/issues/42",
    "issue_title": "Panic: nil pointer in Reconcile when owner is nil",
    "files": {
        "pkg/controller/reconciler.go": (
            "package controller\n\n"
            "func Reconcile(obj *Object) error {\n"
            "\tresult := obj.Process()\n"
            "\treturn result\n"
            "}\n"
        ),
        "pkg/controller/reconciler_test.go": (
            "package controller\n\n"
            'import "testing"\n\n'
            "func TestReconcile(t *testing.T) {\n"
            "\terr := Reconcile(nil)\n"
            "\tif err != nil {\n"
            "\t\tt.Fatal(err)\n"
            "\t}\n"
            "}\n"
        ),
    },
    "triage_response": {
        **_TRIAGE_BASE,
        "classification": "bug",
        "confidence": 0.92,
        "severity": "high",
        "affected_components": ["pkg/controller/reconciler.go"],
        "reproduction": {
            "existing_tests": ["pkg/controller/reconciler_test.go"],
            "can_reproduce": True,
            "reproduction_steps": "Run go test ./pkg/controller/... — panics on nil",
        },
        "reasoning": "Nil pointer dereference when obj parameter is nil in Reconcile()",
    },
    "implement_response": {
        "root_cause": "Missing nil check for obj parameter in Reconcile()",
        "fix_description": "Added nil guard at function entry",
        "files_changed": ["pkg/controller/reconciler.go"],
        "file_changes": [
            {
                "path": "pkg/controller/reconciler.go",
                "content": (
                    "package controller\n\n"
                    "func Reconcile(obj *Object) error {\n"
                    "\tif obj == nil {\n"
                    "\t\treturn nil\n"
                    "\t}\n"
                    "\tresult := obj.Process()\n"
                    "\treturn result\n"
                    "}\n"
                ),
            }
        ],
        "test_added": "",
        "tests_passing": True,
        "linters_passing": True,
        "confidence": 0.95,
        "diff_summary": "+3 lines (nil check guard)",
    },
    "review_response": {
        **_REVIEW_APPROVE,
        "confidence": 0.93,
        "summary": "Minimal nil check guard. Correct fix for nil pointer dereference.",
    },
    "validate_response": {
        **_VALIDATE_READY,
        "test_summary": "All tests pass including nil check path",
        "pr_description": "## Fix: Nil pointer in Reconcile\n\nAdded nil guard.",
        "confidence": 0.95,
    },
    "human_diff": (
        "diff --git a/pkg/controller/reconciler.go b/pkg/controller/reconciler.go\n"
        "--- a/pkg/controller/reconciler.go\n"
        "+++ b/pkg/controller/reconciler.go\n"
        "@@ -2,4 +2,7 @@\n"
        " func Reconcile(obj *Object) error {\n"
        "+\tif obj == nil {\n"
        "+\t\treturn nil\n"
        "+\t}\n"
        " \tresult := obj.Process()\n"
    ),
}

PYTHON_IMPORT_BUG: dict[str, Any] = {
    "name": "python-import-error",
    "issue_url": "https://github.com/konflux-ci/mintmaker/issues/17",
    "issue_title": "NameError: name 'yaml' is not defined in config loader",
    "files": {
        "src/config.py": (
            '"""Config loader."""\n\n'
            "def load_config(path: str) -> dict:\n"
            "    with open(path) as f:\n"
            "        return yaml.safe_load(f)\n"
        ),
        "tests/test_config.py": (
            "import pytest\n"
            "from src.config import load_config\n\n"
            "def test_load_config(tmp_path):\n"
            "    p = tmp_path / 'cfg.yaml'\n"
            "    p.write_text('key: val')\n"
            "    assert load_config(str(p)) == {'key': 'val'}\n"
        ),
    },
    "triage_response": {
        **_TRIAGE_BASE,
        "classification": "bug",
        "confidence": 0.95,
        "severity": "medium",
        "affected_components": ["src/config.py"],
        "reproduction": {
            "existing_tests": ["tests/test_config.py"],
            "can_reproduce": True,
            "reproduction_steps": "Import fails because yaml is not imported",
        },
        "reasoning": "Missing import statement for yaml module in config.py",
    },
    "implement_response": {
        "root_cause": "Missing `import yaml` statement in src/config.py",
        "fix_description": "Added `import yaml` at the top of the file",
        "files_changed": ["src/config.py"],
        "file_changes": [
            {
                "path": "src/config.py",
                "content": (
                    '"""Config loader."""\n\n'
                    "import yaml\n\n\n"
                    "def load_config(path: str) -> dict:\n"
                    "    with open(path) as f:\n"
                    "        return yaml.safe_load(f)\n"
                ),
            }
        ],
        "test_added": "",
        "tests_passing": True,
        "linters_passing": True,
        "confidence": 0.98,
        "diff_summary": "+1 line (import yaml)",
    },
    "review_response": {
        **_REVIEW_APPROVE,
        "confidence": 0.97,
        "summary": "One-line import fix. Correct and minimal.",
    },
    "validate_response": {
        **_VALIDATE_READY,
        "test_summary": "All tests pass after adding import",
        "pr_description": "## Fix: Missing yaml import\n\nAdded `import yaml` to config.py.",
        "confidence": 0.98,
    },
    "human_diff": (
        "diff --git a/src/config.py b/src/config.py\n"
        "--- a/src/config.py\n"
        "+++ b/src/config.py\n"
        "@@ -1,3 +1,5 @@\n"
        ' """Config loader."""\n'
        "+\n"
        "+import yaml\n"
        " \n"
        " def load_config(path: str) -> dict:\n"
    ),
}

YAML_CONFIG_BUG: dict[str, Any] = {
    "name": "yaml-config-parse-error",
    "issue_url": "https://github.com/konflux-ci/release-service/issues/88",
    "issue_title": "KeyError: 'spec' when reading pipeline config",
    "files": {
        "config/pipeline.yaml": (
            "apiVersion: v1\n"
            "kind: Pipeline\n"
            "metadata:\n"
            "  name: release\n"
            "sepc:\n"
            "  stages:\n"
            "    - name: build\n"
        ),
        "pkg/loader.go": (
            "package pkg\n\n"
            'import "fmt"\n\n'
            "func LoadPipeline(data map[string]interface{}) error {\n"
            '\tspec := data["spec"].(map[string]interface{})\n'
            "\tfmt.Println(spec)\n"
            "\treturn nil\n"
            "}\n"
        ),
        "pkg/loader_test.go": (
            "package pkg\n\n"
            'import "testing"\n\n'
            "func TestLoadPipeline(t *testing.T) {\n"
            '\tdata := map[string]interface{}{"sepc": nil}\n'
            "\terr := LoadPipeline(data)\n"
            "\tif err == nil {\n"
            '\t\tt.Fatal("expected error")\n'
            "\t}\n"
            "}\n"
        ),
    },
    "triage_response": {
        **_TRIAGE_BASE,
        "classification": "bug",
        "confidence": 0.88,
        "severity": "high",
        "affected_components": ["config/pipeline.yaml", "pkg/loader.go"],
        "reproduction": {
            "existing_tests": ["pkg/loader_test.go"],
            "can_reproduce": True,
            "reproduction_steps": "YAML has 'sepc' typo instead of 'spec'",
        },
        "reasoning": "Typo in pipeline.yaml: 'sepc' should be 'spec'",
    },
    "implement_response": {
        "root_cause": "Typo in pipeline.yaml — key 'sepc' should be 'spec'",
        "fix_description": "Fixed typo from 'sepc' to 'spec' in pipeline.yaml",
        "files_changed": ["config/pipeline.yaml"],
        "file_changes": [
            {
                "path": "config/pipeline.yaml",
                "content": (
                    "apiVersion: v1\n"
                    "kind: Pipeline\n"
                    "metadata:\n"
                    "  name: release\n"
                    "spec:\n"
                    "  stages:\n"
                    "    - name: build\n"
                ),
            }
        ],
        "test_added": "",
        "tests_passing": True,
        "linters_passing": True,
        "confidence": 0.99,
        "diff_summary": "1 character fix: sepc → spec",
    },
    "review_response": {
        **_REVIEW_APPROVE,
        "confidence": 0.99,
        "summary": "Single-character typo fix. Correct and minimal.",
    },
    "validate_response": {
        **_VALIDATE_READY,
        "test_summary": "All tests pass after fixing YAML key typo",
        "pr_description": "## Fix: Typo in pipeline.yaml\n\nFixed 'sepc' → 'spec'.",
        "confidence": 0.99,
    },
    "human_diff": (
        "diff --git a/config/pipeline.yaml b/config/pipeline.yaml\n"
        "--- a/config/pipeline.yaml\n"
        "+++ b/config/pipeline.yaml\n"
        "@@ -4,4 +4,4 @@\n"
        "   name: release\n"
        "-sepc:\n"
        "+spec:\n"
        "   stages:\n"
    ),
}

ALL_BUGS = [NIL_POINTER_BUG, PYTHON_IMPORT_BUG, YAML_CONFIG_BUG]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


# ====================================================================
# Helpers
# ====================================================================


def _init_repo(tmp_path: Path, bug: dict[str, Any]) -> Path:
    """Create a git repo populated with the bug scenario's files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel_path, content in bug["files"].items():
        fpath = repo / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content)
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env=_GIT_ENV,
    )
    return repo


def _make_config() -> EngineConfig:
    """Engine config with test/lint execution disabled — safe for test repos."""
    cfg = EngineConfig()
    cfg.phases.implement.run_tests_after_each_edit = False
    cfg.phases.implement.run_linters = False
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    return cfg


def _mock_responses(bug: dict[str, Any]) -> list[str]:
    """Build the ordered list of LLM responses for a full pipeline run."""
    return [
        json.dumps(bug["triage_response"]),
        json.dumps(bug["implement_response"]),
        json.dumps(bug["review_response"]),
        json.dumps(bug["validate_response"]),
    ]


def _make_loop(
    repo: Path,
    output_dir: Path,
    bug: dict[str, Any],
    *,
    config: EngineConfig | None = None,
    comparison_ref: str = "",
) -> RalphLoop:
    """Create a fully-wired RalphLoop with real phases and MockProvider."""
    cfg = config or _make_config()
    loop = RalphLoop(
        config=cfg,
        llm=MockProvider(responses=_mock_responses(bug)),
        issue_url=bug["issue_url"],
        repo_path=str(repo),
        output_dir=str(output_dir),
        comparison_ref=comparison_ref,
    )
    loop.register_phase("triage", TriagePhase)
    loop.register_phase("implement", ImplementPhase)
    loop.register_phase("review", ReviewPhase)
    loop.register_phase("validate", ValidatePhase)
    return loop


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Prevent real sleeping during backoff so e2e tests stay fast."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())


# ====================================================================
# 1. FULL PIPELINE — all phases run end-to-end against each bug
# ====================================================================


class TestEndToEndPipeline:
    """Full pipeline tests — loop runs through all phases against simulated bugs."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bug", ALL_BUGS, ids=lambda b: b["name"])
    async def test_full_pipeline_succeeds(self, bug, tmp_path):
        """Loop completes with status=success for each simulated bug scenario."""
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)

        execution = await loop.run()

        assert execution.result["status"] == "success"
        assert execution.result["total_iterations"] == 5

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bug", ALL_BUGS, ids=lambda b: b["name"])
    async def test_all_phases_run_in_order(self, bug, tmp_path):
        """All five phases execute in the correct sequence."""
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)

        execution = await loop.run()

        phases_run = [it["phase"] for it in execution.iterations]
        assert phases_run == PHASE_ORDER

    @pytest.mark.asyncio
    async def test_execution_json_written_with_complete_structure(self, tmp_path):
        """execution.json contains all required fields."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        await loop.run()

        data = json.loads((out / "execution.json").read_text())
        exec_data = data["execution"]

        assert "id" in exec_data
        assert "started_at" in exec_data
        assert "completed_at" in exec_data
        assert exec_data["completed_at"] != ""
        assert exec_data["trigger"]["type"] == "github_issue"
        assert exec_data["trigger"]["source_url"] == bug["issue_url"]
        assert exec_data["target"]["repo_path"] == str(repo)
        assert len(exec_data["iterations"]) == 5
        assert exec_data["result"]["status"] == "success"
        assert "metrics" in exec_data
        assert "actions" in exec_data

    @pytest.mark.asyncio
    async def test_status_txt_matches_result(self, tmp_path):
        """status.txt file contains the result status string."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        await loop.run()

        assert (out / "status.txt").read_text() == "success"

    @pytest.mark.asyncio
    async def test_iteration_records_have_timing(self, tmp_path):
        """Every iteration record includes timing metadata."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        for it in execution.iterations:
            assert "number" in it
            assert "started_at" in it
            assert "completed_at" in it
            assert "duration_ms" in it
            assert it["duration_ms"] >= 0
            assert "result" in it
            assert "success" in it["result"]

    @pytest.mark.asyncio
    async def test_phase_results_accumulate_correctly(self, tmp_path):
        """Each phase result is recorded in the execution record."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        phase_results = execution.result["phase_results"]
        assert len(phase_results) == 5
        phase_names = [pr["phase"] for pr in phase_results]
        assert phase_names == PHASE_ORDER
        for pr in phase_results[:4]:
            assert pr["success"] is True
            assert pr["escalate"] is False

    @pytest.mark.asyncio
    async def test_implement_phase_modifies_files(self, tmp_path):
        """The implement phase actually writes files to the test repo."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        await loop.run()

        fixed_file = repo / "pkg" / "controller" / "reconciler.go"
        content = fixed_file.read_text()
        assert "nil" in content
        assert content != bug["files"]["pkg/controller/reconciler.go"]


# ====================================================================
# 2. COMPARISON MODE — agent fix vs human fix
# ====================================================================


class TestEndToEndComparisonMode:
    """Tests comparison mode with a known human fix reference."""

    @pytest.mark.asyncio
    async def test_comparison_ref_recorded_in_execution(self, tmp_path):
        """Setting comparison_ref records it in the execution record."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug, comparison_ref="abc123def")
        execution = await loop.run()

        assert execution.target["comparison_ref"] == "abc123def"

    @pytest.mark.asyncio
    async def test_comparison_ref_persisted_in_execution_json(self, tmp_path):
        """comparison_ref appears in the persisted execution.json file."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug, comparison_ref="abc123def")
        await loop.run()

        data = json.loads((out / "execution.json").read_text())
        assert data["execution"]["target"]["comparison_ref"] == "abc123def"

    @pytest.mark.asyncio
    async def test_comparison_data_enabled_when_ref_set(self, tmp_path):
        """build_comparison returns enabled=True when comparison_ref is set."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug, comparison_ref="abc123def")
        execution = await loop.run()

        comparison = build_comparison(execution.to_dict())
        assert comparison.enabled is True
        assert comparison.comparison_ref == "abc123def"

    @pytest.mark.asyncio
    async def test_comparison_data_disabled_when_no_ref(self, tmp_path):
        """build_comparison returns enabled=False when no comparison_ref."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        comparison = build_comparison(execution.to_dict())
        assert comparison.enabled is False

    @pytest.mark.asyncio
    async def test_comparison_with_injected_diffs(self, tmp_path):
        """build_comparison computes metrics when both agent and human diffs exist."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug, comparison_ref="abc123def")
        execution = await loop.run()

        agent_diff = (
            "diff --git a/pkg/controller/reconciler.go"
            " b/pkg/controller/reconciler.go\n"
            "--- a/pkg/controller/reconciler.go\n"
            "+++ b/pkg/controller/reconciler.go\n"
            "@@ -2,4 +2,7 @@\n"
            " func Reconcile(obj *Object) error {\n"
            "+\tif obj == nil {\n"
            "+\t\treturn nil\n"
            "+\t}\n"
            " \tresult := obj.Process()\n"
        )
        execution.result["comparison"] = {
            "agent_diff": agent_diff,
            "human_diff": bug["human_diff"],
            "similarity_score": 0.92,
            "analysis": "Both fixes add a nil check guard with identical logic.",
        }

        comparison = build_comparison(execution.to_dict())
        assert comparison.enabled is True
        assert comparison.metrics.similarity_score == 0.92
        assert comparison.analysis == "Both fixes add a nil check guard with identical logic."
        assert comparison.agent_summary.total_files > 0
        assert comparison.human_summary.total_files > 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bug", ALL_BUGS, ids=lambda b: b["name"])
    async def test_comparison_metrics_computable_for_all_bugs(self, bug, tmp_path):
        """Each bug scenario's human_diff is parseable and produces valid metrics."""
        bug_with_comparison = dict(bug)
        repo = _init_repo(tmp_path, bug_with_comparison)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug_with_comparison, comparison_ref="human-fix-ref")
        execution = await loop.run()

        execution.result["comparison"] = {
            "agent_diff": "",
            "human_diff": bug["human_diff"],
        }

        comparison = build_comparison(execution.to_dict())
        assert comparison.enabled is True
        assert comparison.human_summary.total_files > 0


# ====================================================================
# 3. METRICS AND OBSERVABILITY
# ====================================================================


class TestEndToEndMetrics:
    """Tests metrics collection, action recording, and execution time."""

    @pytest.mark.asyncio
    async def test_metrics_populated_for_all_phases(self, tmp_path):
        """Phase iteration counts and timing are recorded for every phase."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        metrics = execution.metrics
        assert metrics["total_iterations"] == 5
        for phase in PHASE_ORDER:
            assert phase in metrics["phase_iteration_counts"]
            assert metrics["phase_iteration_counts"][phase] >= 1
            assert phase in metrics["time_per_phase_ms"]
            assert metrics["time_per_phase_ms"][phase] > 0

    @pytest.mark.asyncio
    async def test_llm_metrics_counters_nonzero(self, tmp_path):
        """LLM call counters must be non-zero after a full run (D2 fix)."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        metrics = execution.metrics
        llm_actions = [a for a in execution.actions if a["action_type"] == "llm_query"]
        assert metrics["total_llm_calls"] == len(llm_actions), (
            "total_llm_calls must match the number of llm_query actions in the trace"
        )
        assert metrics["total_llm_calls"] >= 4, (
            "Expect at least 4 LLM calls (triage, implement, review, validate)"
        )
        assert metrics["total_tokens_in"] > 0
        assert metrics["total_tokens_out"] > 0

    @pytest.mark.asyncio
    async def test_actions_recorded_across_phases(self, tmp_path):
        """Actions from all phases appear in the execution record."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        assert len(execution.actions) > 0
        action_types = {a["action_type"] for a in execution.actions}
        assert "llm_query" in action_types

    @pytest.mark.asyncio
    async def test_llm_calls_recorded_with_provenance(self, tmp_path):
        """LLM call actions include model provenance information."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        llm_actions = [a for a in execution.actions if a["action_type"] == "llm_query"]
        assert len(llm_actions) >= 4
        for action in llm_actions:
            llm_ctx = action.get("llm_context", {})
            assert "model" in llm_ctx
            assert "provider" in llm_ctx
            assert "tokens_in" in llm_ctx
            assert "tokens_out" in llm_ctx

    @pytest.mark.asyncio
    async def test_execution_completes_within_time_budget(self, tmp_path):
        """Full pipeline completes well within the default 30-minute budget."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)

        start = time.monotonic()
        execution = await loop.run()
        elapsed_s = time.monotonic() - start

        assert execution.result["status"] == "success"
        assert elapsed_s < 60, "E2E test with MockProvider should complete in under 60s"

    @pytest.mark.asyncio
    async def test_tool_actions_recorded(self, tmp_path):
        """Tool executions (file_read, shell_run, etc.) appear in action log."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        tool_actions = [a for a in execution.actions if a["action_type"].startswith("tool:")]
        assert len(tool_actions) > 0

    @pytest.mark.asyncio
    async def test_metrics_persisted_in_execution_json(self, tmp_path):
        """Metrics from the execution record match those in execution.json."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        data = json.loads((out / "execution.json").read_text())
        persisted_metrics = data["execution"]["metrics"]
        assert persisted_metrics["total_iterations"] == execution.metrics["total_iterations"]


# ====================================================================
# 4. REPORT GENERATION (visualization integration)
# ====================================================================


class TestEndToEndReports:
    """Tests that the visualization layer produces output from E2E execution data."""

    @pytest.mark.asyncio
    async def test_report_generator_processes_execution_data(self, tmp_path):
        """ReportGenerator can extract report data from a real execution record."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        report_data = extract_report_data(execution.to_dict())
        assert report_data.status == "success"
        assert report_data.total_iterations == 5
        assert len(report_data.phases_summary) > 0
        assert len(report_data.iterations) == 5

    @pytest.mark.asyncio
    async def test_report_html_generated(self, tmp_path):
        """ReportGenerator produces a valid HTML report from execution data."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        generator = ReportGenerator()
        html = generator.generate(execution.to_dict())
        assert "<html" in html
        assert "success" in html.lower()

    @pytest.mark.asyncio
    async def test_decision_tree_data_present(self, tmp_path):
        """Report data includes a decision tree structure."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        report_data = extract_report_data(execution.to_dict())
        tree = report_data.decision_tree
        assert tree is not None
        assert "label" in tree
        assert "children" in tree

    @pytest.mark.asyncio
    async def test_action_map_data_present(self, tmp_path):
        """Report data includes an action map structure."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        report_data = extract_report_data(execution.to_dict())
        action_map = report_data.action_map
        assert action_map is not None
        assert "layers" in action_map
        assert len(action_map["layers"]) > 0

    @pytest.mark.asyncio
    async def test_reports_directory_created_by_loop(self, tmp_path):
        """The loop's _publish_reports creates a reports subdirectory."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        await loop.run()

        reports_dir = out / "reports"
        assert reports_dir.exists()
        report_files = list(reports_dir.iterdir())
        assert len(report_files) > 0


# ====================================================================
# 5. ROBUSTNESS — error handling, edge cases
# ====================================================================


class TestEndToEndRobustness:
    """Tests for edge cases and error handling in the full pipeline."""

    @pytest.mark.asyncio
    async def test_no_github_token_does_not_crash(self, tmp_path):
        """Pipeline succeeds even without a GitHub token (PR creation skipped)."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)

        with patch.dict("os.environ", {"GH_PAT": "", "GITHUB_TOKEN": ""}, clear=False):
            execution = await loop.run()

        assert execution.result["status"] == "success"

    @pytest.mark.asyncio
    async def test_triage_escalation_ends_loop(self, tmp_path):
        """If triage classifies as feature, the loop escalates immediately."""
        bug = dict(NIL_POINTER_BUG)
        triage = dict(bug["triage_response"])
        triage["classification"] = "feature"
        triage["recommendation"] = "escalate"
        bug["triage_response"] = triage

        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        phases_run = [it["phase"] for it in execution.iterations]
        assert phases_run == ["triage"]

    @pytest.mark.asyncio
    async def test_review_rejection_backtracks_to_implement(self, tmp_path):
        """Review with request_changes verdict sends the loop back to implement."""
        bug = NIL_POINTER_BUG
        reject_review = {
            "verdict": "request_changes",
            "findings": [
                {"file": "pkg/controller/reconciler.go", "severity": "medium", "issue": "x"}
            ],
            "scope_assessment": "bug_fix",
            "injection_detected": False,
            "confidence": 0.7,
            "summary": "Needs improvement.",
        }
        approve_review = {
            **_REVIEW_APPROVE,
            "confidence": 0.93,
            "summary": "Fixed. Looks good now.",
        }

        responses = [
            json.dumps(bug["triage_response"]),
            json.dumps(bug["implement_response"]),
            json.dumps(reject_review),
            json.dumps(bug["implement_response"]),
            json.dumps(approve_review),
            json.dumps(bug["validate_response"]),
        ]

        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        cfg = _make_config()
        cfg.loop.max_iterations = 15

        loop = RalphLoop(
            config=cfg,
            llm=MockProvider(responses=responses),
            issue_url=bug["issue_url"],
            repo_path=str(repo),
            output_dir=str(out),
        )
        loop.register_phase("triage", TriagePhase)
        loop.register_phase("implement", ImplementPhase)
        loop.register_phase("review", ReviewPhase)
        loop.register_phase("validate", ValidatePhase)

        execution = await loop.run()

        assert execution.result["status"] == "success"
        phases_run = [it["phase"] for it in execution.iterations]
        assert phases_run.count("implement") == 2
        assert phases_run.count("review") == 2

    @pytest.mark.asyncio
    async def test_iteration_cap_stops_before_completion(self, tmp_path):
        """With a low iteration cap, the loop escalates before finishing all phases."""
        bug = NIL_POINTER_BUG
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        cfg = _make_config()
        cfg.loop = LoopConfig(max_iterations=2)

        loop = _make_loop(repo, out, bug, config=cfg)
        execution = await loop.run()

        assert execution.result["status"] == "escalated"
        assert execution.result["total_iterations"] == 2


# ====================================================================
# 6. CROSS-SCENARIO QUALITY — aggregated checks across all bugs
# ====================================================================


class TestCrossScenarioQuality:
    """Aggregated checks verifying quality properties across all bug scenarios."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bug", ALL_BUGS, ids=lambda b: b["name"])
    async def test_no_escalation_in_successful_run(self, bug, tmp_path):
        """Successful runs should have zero escalation actions."""
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        escalation_actions = [a for a in execution.actions if a["action_type"] == "escalation"]
        assert len(escalation_actions) == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bug", ALL_BUGS, ids=lambda b: b["name"])
    async def test_execution_record_serializable(self, bug, tmp_path):
        """The full execution record can be serialized to JSON without errors."""
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        serialized = json.dumps(execution.to_dict(), indent=2)
        roundtripped = json.loads(serialized)
        assert roundtripped["execution"]["result"]["status"] == "success"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bug", ALL_BUGS, ids=lambda b: b["name"])
    async def test_report_data_extractable_for_all_bugs(self, bug, tmp_path):
        """extract_report_data succeeds for all bug scenarios."""
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        report_data = extract_report_data(execution.to_dict())
        assert report_data.status == "success"
        assert report_data.total_iterations == 5

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bug", ALL_BUGS, ids=lambda b: b["name"])
    async def test_issue_url_appears_in_execution_trigger(self, bug, tmp_path):
        """Each bug's issue URL is correctly recorded in the execution trigger."""
        repo = _init_repo(tmp_path, bug)
        out = tmp_path / "output"
        out.mkdir()
        loop = _make_loop(repo, out, bug)
        execution = await loop.run()

        assert execution.trigger["source_url"] == bug["issue_url"]
