"""Tests for the observer CLI and pipeline integration (Phase 8.5).

Covers:
- CLI arg parsing: required args, defaults, optional flags
- run_observer: full pipeline with fixture artifacts, skip-signing mode
- Pipeline helpers: _extract_triage_components, _extract_issue_body
- main() function: exit codes (OK, policy failed, observer error)
- Workflow YAML validation: observer job present, correct permissions, artifact flow
- End-to-end: fixture artifacts → attestation + policy result + PR comment
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml

from engine.observer.__main__ import (
    EXIT_OBSERVER_ERROR,
    EXIT_OK,
    EXIT_POLICY_FAILED,
    _extract_issue_body,
    _extract_triage_components,
    main,
    run_observer,
)
from engine.observer.cli import parse_args

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

WORKFLOW_PATH = Path(__file__).parent.parent / ".github" / "workflows" / "rl-engine.yml"


_SENTINEL = object()


def _make_execution_data(
    actions: list[dict[str, Any]] | object = _SENTINEL,
    iterations: list[dict[str, Any]] | object = _SENTINEL,
    trigger: dict[str, Any] | object = _SENTINEL,
) -> dict[str, Any]:
    """Build a minimal execution.json structure for tests."""
    default_trigger = {
        "type": "github_issue",
        "source_url": "https://github.com/org/repo/issues/42",
    }
    default_iterations = [
        {
            "number": 1,
            "phase": "triage",
            "started_at": "2026-03-28T10:00:00Z",
            "completed_at": "2026-03-28T10:02:00Z",
            "duration_ms": 120000,
            "result": {"success": True},
            "artifacts": {"affected_components": ["pkg/controller.go"]},
            "observation": {"issue_body": "nil pointer in reconciler"},
        },
        {
            "number": 2,
            "phase": "implement",
            "started_at": "2026-03-28T10:02:00Z",
            "completed_at": "2026-03-28T10:10:00Z",
            "duration_ms": 480000,
            "result": {"success": True},
        },
    ]
    default_actions = [
        {
            "id": "act-1",
            "action_type": "llm_query",
            "phase": "triage",
            "iteration": 1,
            "timestamp": "2026-03-28T10:00:30Z",
            "input": {"description": "Triage LLM call"},
            "output": {"success": True},
            "llm_context": {
                "model": "gemini-2.5-pro",
                "provider": "google",
                "tokens_in": 5000,
                "tokens_out": 1000,
            },
        },
    ]
    return {
        "execution": {
            "id": "test-exec-cli",
            "started_at": "2026-03-28T10:00:00Z",
            "completed_at": "2026-03-28T10:25:00Z",
            "trigger": default_trigger if trigger is _SENTINEL else trigger,
            "target": {"repo": "org/repo", "ref": "abc123"},
            "config": {},
            "iterations": default_iterations if iterations is _SENTINEL else iterations,
            "actions": default_actions if actions is _SENTINEL else actions,
            "result": {"status": "success", "commit_sha": "def456"},
        }
    }


def _write_fixtures(tmp_path: Path, execution_data: dict[str, Any] | None = None) -> Path:
    """Write fixture artifacts to a temp directory and return its path."""
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    data = execution_data or _make_execution_data()
    (artifacts_dir / "execution.json").write_text(json.dumps(data))
    (artifacts_dir / "log.json").write_text("[]")

    transcripts_dir = artifacts_dir / "transcripts"
    transcripts_dir.mkdir()
    (transcripts_dir / "transcript-calls.json").write_text("[]")

    return artifacts_dir


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_required_artifacts_dir(self):
        args = parse_args(["--artifacts-dir", "/tmp/artifacts"])
        assert args.artifacts_dir == "/tmp/artifacts"

    def test_default_output_dir(self):
        args = parse_args(["--artifacts-dir", "/tmp/a"])
        assert args.output_dir == "./attestation"

    def test_custom_output_dir(self):
        args = parse_args(["--artifacts-dir", "/tmp/a", "--output-dir", "/tmp/out"])
        assert args.output_dir == "/tmp/out"

    def test_optional_config(self):
        args = parse_args(["--artifacts-dir", "/tmp/a", "--config", "/tmp/config.yaml"])
        assert args.config == "/tmp/config.yaml"

    def test_optional_branch_dir(self):
        args = parse_args(["--artifacts-dir", "/tmp/a", "--branch-dir", "/tmp/branch"])
        assert args.branch_dir == "/tmp/branch"

    def test_optional_templates_dir(self):
        args = parse_args(["--artifacts-dir", "/tmp/a", "--templates-dir", "/tmp/t"])
        assert args.templates_dir == "/tmp/t"

    def test_skip_signing_default_false(self):
        args = parse_args(["--artifacts-dir", "/tmp/a"])
        assert args.skip_signing is False

    def test_skip_signing_flag(self):
        args = parse_args(["--artifacts-dir", "/tmp/a", "--skip-signing"])
        assert args.skip_signing is True

    def test_missing_required_arg(self):
        with pytest.raises(SystemExit):
            parse_args([])

    def test_all_args(self):
        args = parse_args(
            [
                "--artifacts-dir",
                "/art",
                "--output-dir",
                "/out",
                "--config",
                "/cfg.yaml",
                "--branch-dir",
                "/branch",
                "--templates-dir",
                "/tpl",
                "--skip-signing",
            ]
        )
        assert args.artifacts_dir == "/art"
        assert args.output_dir == "/out"
        assert args.config == "/cfg.yaml"
        assert args.branch_dir == "/branch"
        assert args.templates_dir == "/tpl"
        assert args.skip_signing is True


# ---------------------------------------------------------------------------
# Pipeline Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_extract_triage_components_present(self):
        data = _make_execution_data()
        components = _extract_triage_components(data)
        assert components == ["pkg/controller.go"]

    def test_extract_triage_components_absent(self):
        data = _make_execution_data(iterations=[])
        assert _extract_triage_components(data) == []

    def test_extract_triage_components_empty(self):
        data = _make_execution_data(
            iterations=[
                {
                    "number": 1,
                    "phase": "triage",
                    "artifacts": {"affected_components": []},
                }
            ]
        )
        assert _extract_triage_components(data) == []

    def test_extract_triage_components_no_artifacts(self):
        data = _make_execution_data(iterations=[{"number": 1, "phase": "triage"}])
        assert _extract_triage_components(data) == []

    def test_extract_issue_body_from_observation(self):
        data = _make_execution_data()
        body = _extract_issue_body(data)
        assert body == "nil pointer in reconciler"

    def test_extract_issue_body_from_trigger(self):
        data = _make_execution_data(
            trigger={
                "type": "github_issue",
                "source_url": "https://github.com/o/r/issues/1",
                "issue_body": "trigger body text",
            }
        )
        body = _extract_issue_body(data)
        assert body == "trigger body text"

    def test_extract_issue_body_empty(self):
        data = _make_execution_data(iterations=[])
        body = _extract_issue_body(data)
        assert body == ""

    def test_extract_triage_components_unwrapped(self):
        """Handle execution data without the 'execution' wrapper."""
        inner = _make_execution_data()["execution"]
        components = _extract_triage_components(inner)
        assert components == ["pkg/controller.go"]


# ---------------------------------------------------------------------------
# run_observer Pipeline
# ---------------------------------------------------------------------------


class TestRunObserver:
    def test_full_pipeline_skip_signing(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        assert "policy_passed" in result
        assert "attestation_path" in result
        assert "pr_comment" in result
        assert "summary" in result
        assert "written_files" in result
        assert "cross_check_all_passed" in result

    def test_attestation_file_written(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        att_path = Path(result["attestation_path"])
        assert att_path.exists()
        att = json.loads(att_path.read_text())
        assert att["_type"] == "https://in-toto.io/Statement/v1"

    def test_policy_result_file_written(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        policy_path = output_dir / "policy-result.json"
        assert policy_path.exists()
        result = json.loads(policy_path.read_text())
        assert "passed" in result
        assert "rule_results" in result

    def test_pr_comment_file_written(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        comment_path = output_dir / "pr-comment.md"
        assert comment_path.exists()
        content = comment_path.read_text()
        assert "Observer Policy" in content

    def test_summary_file_written(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        summary_path = output_dir / "summary.txt"
        assert summary_path.exists()
        content = summary_path.read_text()
        assert "Observer policy" in content

    def test_signing_metadata_written(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        meta_path = output_dir / "signing-metadata.json"
        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        assert meta["signing_method"] == "none"
        assert meta["signed"] is False

    def test_schema_violations_empty(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        assert result["schema_violations"] == []

    def test_empty_artifacts_dir(self, tmp_path: Path):
        artifacts_dir = tmp_path / "empty"
        artifacts_dir.mkdir()
        (artifacts_dir / "execution.json").write_text("{}")

        output_dir = tmp_path / "attestation"
        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        assert "policy_passed" in result

    def test_with_config_file(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
            observer:
              enabled: true
              signing_method: none
              fail_on_policy_violation: false
        """)
        )

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            config_path=str(config_path),
            skip_signing=True,
        )

        assert "policy_passed" in result

    def test_with_templates_dir(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        (templates_dir / "triage.md").write_text("# Triage prompt")
        (templates_dir / "implement.md").write_text("# Implement prompt")

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            templates_dir=str(templates_dir),
            skip_signing=True,
        )

        att_path = Path(result["attestation_path"])
        att = json.loads(att_path.read_text())
        resolved_deps = att["predicate"]["buildDefinition"]["resolvedDependencies"]
        prompt_uris = [d["uri"] for d in resolved_deps if d["uri"].startswith("prompt://")]
        assert len(prompt_uris) >= 2

    def test_cross_check_results_in_attestation(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        att_path = Path(result["attestation_path"])
        att = json.loads(att_path.read_text())
        cross_checks = att["predicate"]["runDetails"]["crossCheckResults"]
        assert "phase_ordering" in cross_checks
        assert "token_plausibility" in cross_checks

    def test_model_info_in_attestation(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        att_path = Path(result["attestation_path"])
        att = json.loads(att_path.read_text())
        models = att["predicate"]["runDetails"]["models"]
        assert len(models) >= 1
        assert models[0]["id"] == "gemini-2.5-pro"

    def test_written_files_dict(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        wf = result["written_files"]
        assert "attestation" in wf
        assert "policy_result" in wf
        assert "pr_comment" in wf
        assert "summary" in wf
        assert "metadata" in wf


# ---------------------------------------------------------------------------
# main() Function
# ---------------------------------------------------------------------------


class TestMain:
    def test_exit_ok(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        exit_code = main(
            [
                "--artifacts-dir",
                str(artifacts_dir),
                "--output-dir",
                str(output_dir),
                "--skip-signing",
            ]
        )
        assert exit_code == EXIT_OK

    def test_exit_observer_error_bad_config(self, tmp_path: Path):
        """An invalid config path causes load_config to fail."""
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text(": : : invalid yaml [[[")

        exit_code = main(
            [
                "--artifacts-dir",
                str(artifacts_dir),
                "--output-dir",
                str(output_dir),
                "--config",
                str(bad_config),
                "--skip-signing",
            ]
        )
        assert exit_code == EXIT_OBSERVER_ERROR

    def test_exit_policy_failed_with_fail_on_violation(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            textwrap.dedent("""\
            observer:
              enabled: true
              signing_method: none
              fail_on_policy_violation: true
              policy_file: {policy_path}
        """)
        )

        policy_path = tmp_path / "strict-policy.yaml"
        policy_path.write_text(
            textwrap.dedent("""\
            policy:
              version: "1"
              rules:
                model_allowlist:
                  enabled: true
                  models:
                    - "only-this-model"
        """)
        )

        final_config = config_path.read_text().replace("{policy_path}", str(policy_path))
        config_path.write_text(final_config)

        exit_code = main(
            [
                "--artifacts-dir",
                str(artifacts_dir),
                "--output-dir",
                str(output_dir),
                "--config",
                str(config_path),
                "--skip-signing",
            ]
        )
        assert exit_code == EXIT_POLICY_FAILED

    def test_policy_fail_without_fail_on_violation_exits_ok(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        config_path = tmp_path / "config.yaml"
        policy_path = tmp_path / "strict-policy.yaml"
        policy_path.write_text(
            textwrap.dedent("""\
            policy:
              version: "1"
              rules:
                model_allowlist:
                  enabled: true
                  models:
                    - "only-this-model"
        """)
        )

        config_path.write_text(
            textwrap.dedent(f"""\
            observer:
              enabled: true
              signing_method: none
              fail_on_policy_violation: false
              policy_file: "{policy_path}"
        """)
        )

        exit_code = main(
            [
                "--artifacts-dir",
                str(artifacts_dir),
                "--output-dir",
                str(output_dir),
                "--config",
                str(config_path),
                "--skip-signing",
            ]
        )
        assert exit_code == EXIT_OK


# ---------------------------------------------------------------------------
# Workflow YAML Validation
# ---------------------------------------------------------------------------


class TestWorkflowYAML:
    @pytest.fixture(autouse=True)
    def _load_workflow(self):
        self.workflow = yaml.safe_load(WORKFLOW_PATH.read_text())

    def test_observer_job_exists(self):
        assert "observer" in self.workflow["jobs"]

    def test_observer_needs_agent_job(self):
        observer = self.workflow["jobs"]["observer"]
        assert "run-engine" in observer["needs"]

    def test_observer_runs_always(self):
        observer = self.workflow["jobs"]["observer"]
        assert observer.get("if") == "always()"

    def test_observer_has_id_token_write(self):
        observer = self.workflow["jobs"]["observer"]
        perms = observer.get("permissions", {})
        assert perms.get("id-token") == "write"

    def test_observer_has_contents_read(self):
        observer = self.workflow["jobs"]["observer"]
        perms = observer.get("permissions", {})
        assert perms.get("contents") == "read"

    def test_observer_has_pr_write(self):
        observer = self.workflow["jobs"]["observer"]
        perms = observer.get("permissions", {})
        assert perms.get("pull-requests") == "write"

    def test_observer_installs_cosign(self):
        observer = self.workflow["jobs"]["observer"]
        step_uses = [s.get("uses", "") for s in observer["steps"]]
        assert any("cosign-installer" in u for u in step_uses)

    def test_observer_downloads_agent_artifacts(self):
        observer = self.workflow["jobs"]["observer"]
        step_uses = [s.get("uses", "") for s in observer["steps"]]
        assert any("download-artifact" in u for u in step_uses)

    def test_observer_uploads_attestation(self):
        observer = self.workflow["jobs"]["observer"]
        step_uses = [s.get("uses", "") for s in observer["steps"]]
        assert any("upload-artifact" in u for u in step_uses)

    def test_observer_artifact_name_includes_run_id(self):
        observer = self.workflow["jobs"]["observer"]
        upload_steps = [s for s in observer["steps"] if "upload-artifact" in s.get("uses", "")]
        assert len(upload_steps) >= 1
        artifact_name = upload_steps[0].get("with", {}).get("name", "")
        assert "github.run_id" in artifact_name

    def test_observer_runs_python_module(self):
        observer = self.workflow["jobs"]["observer"]
        run_steps = [s.get("run", "") for s in observer["steps"] if s.get("run")]
        assert any("python -m engine.observer" in r for r in run_steps)

    def test_observer_passes_templates_dir(self):
        observer = self.workflow["jobs"]["observer"]
        run_steps = [s.get("run", "") for s in observer["steps"] if s.get("run")]
        assert any("--templates-dir" in r for r in run_steps)

    def test_observer_passes_artifacts_dir(self):
        observer = self.workflow["jobs"]["observer"]
        run_steps = [s.get("run", "") for s in observer["steps"] if s.get("run")]
        assert any("--artifacts-dir" in r for r in run_steps)

    def test_agent_job_has_outputs(self):
        agent_job = self.workflow["jobs"]["run-engine"]
        assert "outputs" in agent_job

    def test_observer_timeout(self):
        observer = self.workflow["jobs"]["observer"]
        assert observer.get("timeout-minutes", 0) <= 15


# ---------------------------------------------------------------------------
# End-to-End Integration
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_roundtrip_fixtures_to_attestation(self, tmp_path: Path):
        """Full pipeline: fixture artifacts → attestation → policy → outputs."""
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        assert Path(result["attestation_path"]).exists()

        att = json.loads(Path(result["attestation_path"]).read_text())
        assert att["_type"] == "https://in-toto.io/Statement/v1"
        assert att["predicateType"] == "https://rl-engine.dev/provenance/agent/v1"

        assert (output_dir / "policy-result.json").exists()
        assert (output_dir / "pr-comment.md").exists()
        assert (output_dir / "summary.txt").exists()
        assert (output_dir / "signing-metadata.json").exists()

    def test_multiple_models_tracked(self, tmp_path: Path):
        """Two different models in actions → both in attestation."""
        data = _make_execution_data(
            actions=[
                {
                    "id": "a1",
                    "action_type": "llm_query",
                    "phase": "triage",
                    "iteration": 1,
                    "timestamp": "2026-03-28T10:00:30Z",
                    "input": {"description": "Call 1"},
                    "output": {},
                    "llm_context": {
                        "model": "gemini-2.5-pro",
                        "provider": "google",
                        "tokens_in": 3000,
                        "tokens_out": 500,
                    },
                },
                {
                    "id": "a2",
                    "action_type": "llm_query",
                    "phase": "review",
                    "iteration": 3,
                    "timestamp": "2026-03-28T10:15:00Z",
                    "input": {"description": "Call 2"},
                    "output": {},
                    "llm_context": {
                        "model": "claude-sonnet-4-20250514",
                        "provider": "anthropic",
                        "tokens_in": 4000,
                        "tokens_out": 800,
                    },
                },
            ]
        )
        artifacts_dir = _write_fixtures(tmp_path, execution_data=data)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        att = json.loads(Path(result["attestation_path"]).read_text())
        models = att["predicate"]["runDetails"]["models"]
        model_ids = {m["id"] for m in models}
        assert "gemini-2.5-pro" in model_ids
        assert "claude-sonnet-4-20250514" in model_ids

    def test_attestation_subject_from_result(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        att = json.loads(Path(result["attestation_path"]).read_text())
        subject = att["subject"]
        assert len(subject) == 1
        assert subject[0]["digest"]["sha1"] == "def456"

    def test_policy_comment_contains_observer_attribution(self, tmp_path: Path):
        artifacts_dir = _write_fixtures(tmp_path)
        output_dir = tmp_path / "attestation"

        result = run_observer(
            artifacts_dir=str(artifacts_dir),
            output_dir=str(output_dir),
            skip_signing=True,
        )

        assert "neutral observer" in result["pr_comment"].lower()
