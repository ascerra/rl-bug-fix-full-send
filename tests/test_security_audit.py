"""Security Audit — Phase 5.4.

Verifies the four security audit sub-items:
1. Commit signing works (gitsign and GPG configuration)
2. Provenance recording (model name, provider, tokens in every LLM action)
3. No secrets in logs or artifacts (full redaction pipeline)
4. Untrusted content separation in all LLM calls (every phase wraps issue content)

See SPEC §7 (Golden Principles), ARCHITECTURE ADR-006, and IMPLEMENTATION-PLAN Phase 5.4.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from engine.config import EngineConfig, SecurityConfig
from engine.integrations.github import GitHubAdapter
from engine.integrations.llm import LLMResponse, MockProvider
from engine.loop import PipelineEngine
from engine.observability.logger import StructuredLogger
from engine.observability.metrics import LoopMetrics
from engine.observability.tracer import Tracer
from engine.phases.base import PHASE_TOOL_SETS, Phase, PhaseResult
from engine.phases.implement import ImplementPhase
from engine.phases.review import ReviewPhase
from engine.phases.triage import TriagePhase
from engine.phases.validate import ValidatePhase
from engine.secrets import (
    KNOWN_SECRET_ENV_VARS,
    SecretManager,
    SecretRedactor,
)
from engine.tools.executor import ToolExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TRIAGE_JSON = json.dumps(
    {
        "classification": "bug",
        "confidence": 0.9,
        "severity": "high",
        "affected_components": ["pkg/server.go"],
        "reproduction": {"existing_tests": [], "can_reproduce": False},
        "injection_detected": False,
        "recommendation": "proceed",
        "reasoning": "Nil pointer dereference in server handler.",
    }
)

_IMPLEMENT_JSON = json.dumps(
    {
        "root_cause": "Missing nil check",
        "fix_description": "Added nil guard",
        "files_changed": ["pkg/server.go"],
        "file_changes": [{"path": "pkg/server.go", "content": "package main\n"}],
        "test_added": "",
        "tests_passing": True,
        "linters_passing": True,
        "confidence": 0.85,
        "diff_summary": "+nil check",
    }
)

_REVIEW_APPROVE_JSON = json.dumps(
    {
        "verdict": "approve",
        "findings": [],
        "scope_assessment": "bug_fix",
        "injection_detected": False,
        "confidence": 0.9,
        "summary": "Fix looks correct and minimal.",
    }
)

_VALIDATE_READY_JSON = json.dumps(
    {
        "tests_passing": True,
        "test_summary": "10 passed",
        "linters_passing": True,
        "lint_issues": [],
        "diff_is_minimal": True,
        "unnecessary_changes": [],
        "pr_description": "Fixes nil pointer.",
        "ready_to_submit": True,
        "blocking_issues": [],
        "confidence": 0.9,
    }
)


@pytest.fixture()
def tmp_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "server.go").write_text("package main\n")
    os.system(
        f"cd {repo} && git init -b main --quiet && git add -A && git commit -m 'init' --quiet"
    )
    return repo


@pytest.fixture()
def config() -> EngineConfig:
    cfg = EngineConfig()
    cfg.phases.implement.run_tests_after_each_edit = False
    cfg.phases.implement.run_linters = False
    cfg.phases.validate.full_test_suite = False
    cfg.phases.validate.ci_equivalent = False
    cfg.loop.max_iterations = 10
    return cfg


# =========================================================================
# 1. COMMIT SIGNING
# =========================================================================


class TestCommitSigning:
    """Verify gitsign and GPG commit-signing configuration works."""

    @pytest.mark.asyncio()
    async def test_gitsign_sets_correct_git_config(self, tmp_repo: Path):
        adapter = GitHubAdapter(owner="o", repo="r", token="t")
        adapter.config.signing_method = "gitsign"
        result = await adapter.configure_commit_signing(str(tmp_repo))
        assert result["success"] is True
        assert result["method"] == "gitsign"

        proc = await asyncio.create_subprocess_shell(
            "git config commit.gpgsign",
            cwd=str(tmp_repo),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert stdout.decode().strip() == "true"

        proc2 = await asyncio.create_subprocess_shell(
            "git config gpg.x509.program",
            cwd=str(tmp_repo),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout2, _ = await proc2.communicate()
        assert stdout2.decode().strip() == "gitsign"

        proc3 = await asyncio.create_subprocess_shell(
            "git config gpg.format",
            cwd=str(tmp_repo),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout3, _ = await proc3.communicate()
        assert stdout3.decode().strip() == "x509"

    @pytest.mark.asyncio()
    async def test_gpg_sets_commit_gpgsign(self, tmp_repo: Path):
        adapter = GitHubAdapter(owner="o", repo="r", token="t")
        adapter.config.signing_method = "gpg"
        result = await adapter.configure_commit_signing(str(tmp_repo))
        assert result["success"] is True
        assert result["method"] == "gpg"

        proc = await asyncio.create_subprocess_shell(
            "git config commit.gpgsign",
            cwd=str(tmp_repo),
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        assert stdout.decode().strip() == "true"

    @pytest.mark.asyncio()
    async def test_unknown_signing_method_fails(self, tmp_repo: Path):
        adapter = GitHubAdapter(owner="o", repo="r", token="t")
        adapter.config.signing_method = "unknown"
        result = await adapter.configure_commit_signing(str(tmp_repo))
        assert result["success"] is False
        assert "Unknown signing method" in result["error"]

    def test_signing_config_in_engine_config(self):
        cfg = EngineConfig()
        assert cfg.security.commit_signing is False
        assert cfg.security.signing_method in ("gitsign", "gpg")

    def test_signing_config_in_github_integration(self):
        cfg = EngineConfig()
        assert cfg.integrations.github.commit_signing is True
        assert cfg.integrations.github.signing_method == "gitsign"

    @pytest.mark.asyncio()
    async def test_discover_includes_signing_capability_when_enabled(self):
        adapter = GitHubAdapter(owner="o", repo="r", token="t")
        adapter.config.commit_signing = True
        result = await adapter.discover()
        assert "commit_signing" in result["capabilities"]

    @pytest.mark.asyncio()
    async def test_discover_excludes_signing_capability_when_disabled(self):
        adapter = GitHubAdapter(owner="o", repo="r", token="t")
        adapter.config.commit_signing = False
        result = await adapter.discover()
        assert "commit_signing" not in result["capabilities"]

    def test_signing_method_configurable_via_yaml(self):
        from engine.config import load_config

        cfg = load_config(
            overrides={
                "security": {"signing_method": "gpg", "commit_signing": True},
                "integrations": {"github": {"signing_method": "gpg"}},
            }
        )
        assert cfg.security.signing_method == "gpg"
        assert cfg.integrations.github.signing_method == "gpg"


# =========================================================================
# 2. PROVENANCE RECORDING
# =========================================================================


class TestProvenanceRecording:
    """Verify every LLM call records model name, provider, and token counts."""

    def test_llm_response_contains_provenance_fields(self):
        resp = LLMResponse(
            content="test",
            model="gemini-2.5-pro",
            provider="gemini",
            tokens_in=100,
            tokens_out=50,
            latency_ms=200.0,
        )
        assert resp.model == "gemini-2.5-pro"
        assert resp.provider == "gemini"
        assert resp.tokens_in == 100
        assert resp.tokens_out == 50

    @pytest.mark.asyncio()
    async def test_mock_provider_records_provenance(self):
        provider = MockProvider(responses=["hello"])
        resp = await provider.complete(
            system_prompt="test", messages=[{"role": "user", "content": "q"}]
        )
        assert resp.model == "mock-model"
        assert resp.provider == "mock"
        assert resp.tokens_in > 0
        assert resp.tokens_out > 0

    def test_tracer_records_llm_provenance(self):
        tracer = Tracer()
        tracer.record_llm_call(
            description="Test call",
            model="gemini-2.5-pro",
            provider="gemini",
            tokens_in=500,
            tokens_out=200,
            latency_ms=1500.0,
            prompt_summary="system prompt",
            response_summary="response text",
        )
        actions = tracer.get_actions_as_dicts()
        assert len(actions) == 1
        llm_ctx = actions[0]["llm_context"]
        assert llm_ctx["model"] == "gemini-2.5-pro"
        assert llm_ctx["provider"] == "gemini"
        assert llm_ctx["tokens_in"] == 500
        assert llm_ctx["tokens_out"] == 200

    def test_tracer_llm_call_action_type(self):
        tracer = Tracer()
        record = tracer.record_llm_call(
            description="d",
            model="m",
            provider="p",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1.0,
        )
        assert record.action_type == "llm_query"

    @pytest.mark.asyncio()
    async def test_triage_phase_records_llm_provenance(self, tmp_repo, config):
        provider = MockProvider(responses=[_TRIAGE_JSON])
        tracer = Tracer()
        phase = TriagePhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=tracer,
            repo_path=str(tmp_repo),
            issue_data={"url": "https://github.com/o/r/issues/1", "title": "bug", "body": "crash"},
            config=config,
        )
        await phase.execute()
        llm_actions = [a for a in tracer.get_actions_as_dicts() if a["action_type"] == "llm_query"]
        assert len(llm_actions) >= 1
        for action in llm_actions:
            ctx = action["llm_context"]
            assert ctx["model"], "model must be non-empty"
            assert ctx["provider"], "provider must be non-empty"
            assert isinstance(ctx["tokens_in"], int)
            assert isinstance(ctx["tokens_out"], int)

    @pytest.mark.asyncio()
    async def test_implement_phase_records_llm_provenance(self, tmp_repo, config):
        provider = MockProvider(responses=[_IMPLEMENT_JSON])
        tracer = Tracer()
        triage_result = PhaseResult(
            phase="triage",
            success=True,
            findings={
                "classification": "bug",
                "severity": "high",
                "affected_components": ["pkg/server.go"],
                "reasoning": "nil ptr",
            },
            artifacts={"triage_report": {"affected_components": ["pkg/server.go"]}},
        )
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=tracer,
            metrics=LoopMetrics(),
        )
        phase = ImplementPhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=tracer,
            repo_path=str(tmp_repo),
            issue_data={"url": "https://github.com/o/r/issues/1", "title": "bug", "body": "crash"},
            prior_phase_results=[triage_result],
            tool_executor=executor,
            config=config,
        )
        await phase.execute()
        llm_actions = [a for a in tracer.get_actions_as_dicts() if a["action_type"] == "llm_query"]
        assert len(llm_actions) >= 1
        assert all(a["llm_context"]["model"] for a in llm_actions)
        assert all(a["llm_context"]["provider"] for a in llm_actions)

    @pytest.mark.asyncio()
    async def test_review_phase_records_llm_provenance(self, tmp_repo, config):
        provider = MockProvider(responses=[_REVIEW_APPROVE_JSON])
        tracer = Tracer()
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            findings={"root_cause": "nil"},
            artifacts={"diff": "--- a/f\n+++ b/f\n", "files_changed": ["pkg/server.go"]},
        )
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=tracer,
            metrics=LoopMetrics(),
        )
        phase = ReviewPhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=tracer,
            repo_path=str(tmp_repo),
            issue_data={"url": "https://github.com/o/r/issues/1", "title": "bug", "body": "crash"},
            prior_phase_results=[impl_result],
            tool_executor=executor,
            config=config,
        )
        await phase.execute()
        llm_actions = [a for a in tracer.get_actions_as_dicts() if a["action_type"] == "llm_query"]
        assert len(llm_actions) >= 1
        assert all(a["llm_context"]["model"] for a in llm_actions)

    @pytest.mark.asyncio()
    async def test_validate_phase_records_llm_provenance(self, tmp_repo, config):
        provider = MockProvider(responses=[_VALIDATE_READY_JSON])
        tracer = Tracer()
        review_result = PhaseResult(
            phase="review",
            success=True,
            findings={"verdict": "approve"},
            artifacts={"review_report": {"verdict": "approve", "summary": "ok"}},
        )
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            findings={"root_cause": "nil"},
            artifacts={"diff": "--- a/f\n+++ b/f\n", "files_changed": ["pkg/server.go"]},
        )
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=tracer,
            metrics=LoopMetrics(),
        )
        phase = ValidatePhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=tracer,
            repo_path=str(tmp_repo),
            issue_data={"url": "https://github.com/o/r/issues/1", "title": "bug", "body": "crash"},
            prior_phase_results=[impl_result, review_result],
            tool_executor=executor,
            config=config,
        )
        await phase.execute()
        llm_actions = [a for a in tracer.get_actions_as_dicts() if a["action_type"] == "llm_query"]
        assert len(llm_actions) >= 1

    def test_provenance_recording_config_enabled_by_default(self):
        cfg = EngineConfig()
        assert cfg.security.provenance_recording is True

    @pytest.mark.asyncio()
    async def test_provenance_in_execution_record(self, tmp_repo, config):
        provider = MockProvider(
            responses=[_TRIAGE_JSON, _IMPLEMENT_JSON, _REVIEW_APPROVE_JSON, _VALIDATE_READY_JSON]
        )
        loop = PipelineEngine(
            config=config,
            llm=provider,
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(tmp_repo / "output"),
        )
        loop.register_phase("triage", TriagePhase)
        loop.register_phase("implement", ImplementPhase)
        loop.register_phase("review", ReviewPhase)
        loop.register_phase("validate", ValidatePhase)

        record = await loop.run()
        llm_actions = [a for a in record.actions if a.get("action_type") == "llm_query"]
        assert len(llm_actions) >= 4, "Each phase should produce at least one LLM call"
        for action in llm_actions:
            ctx = action["llm_context"]
            assert ctx["model"], "Model recorded in execution record"
            assert ctx["provider"], "Provider recorded in execution record"


# =========================================================================
# 3. NO SECRETS IN LOGS OR ARTIFACTS
# =========================================================================

SECRET_VALUES = {
    "GEMINI_API_KEY": "FAKE-gemini-key-for-testing-only-00000000",
    "GH_PAT": "FAKE-gh-pat-for-testing-only-000000000000",
    "ANTHROPIC_API_KEY": "FAKE-anthropic-key-for-testing-only-00000",
    "SLACK_BOT_TOKEN": "FAKE-slack-token-for-testing-only-0000000",
    "JIRA_API_TOKEN": "FAKE-jira-token-for-testing-only-00000000",
}


class TestNoSecretsInLogs:
    """Verify secrets never appear in log entries, tracer output, or execution artifacts."""

    def _make_redactor(self) -> SecretRedactor:
        return SecretRedactor(SECRET_VALUES)

    def test_logger_redacts_messages(self):
        r = self._make_redactor()
        logger = StructuredLogger(execution_id="test", redactor=r)
        for _name, value in SECRET_VALUES.items():
            logger.info(f"Using {value} for auth")
        for entry in logger.get_entries():
            for value in SECRET_VALUES.values():
                assert value not in entry["message"], (
                    f"Secret leaked in log message: {value[:10]}..."
                )

    def test_logger_redacts_extra_kwargs(self):
        r = self._make_redactor()
        logger = StructuredLogger(execution_id="test", redactor=r)
        logger.info("call", auth=SECRET_VALUES["GH_PAT"], key=SECRET_VALUES["GEMINI_API_KEY"])
        entry = logger.get_entries()[0]
        assert SECRET_VALUES["GH_PAT"] not in entry["auth"]
        assert SECRET_VALUES["GEMINI_API_KEY"] not in entry["key"]

    def test_tracer_redacts_descriptions(self):
        r = self._make_redactor()
        tracer = Tracer(redactor=r)
        for _name, value in SECRET_VALUES.items():
            tracer.record_action(
                action_type="test",
                description=f"Called API with {value}",
            )
        for action in tracer.get_actions():
            for value in SECRET_VALUES.values():
                assert value not in action.input_description

    def test_tracer_redacts_input_context(self):
        r = self._make_redactor()
        tracer = Tracer(redactor=r)
        tracer.record_action(
            action_type="test",
            description="call",
            input_context={"header": f"Bearer {SECRET_VALUES['GH_PAT']}"},
        )
        action = tracer.get_actions()[0]
        assert SECRET_VALUES["GH_PAT"] not in action.input_context["header"]

    def test_tracer_redacts_output_data(self):
        r = self._make_redactor()
        tracer = Tracer(redactor=r)
        tracer.record_action(
            action_type="test",
            description="call",
            output_data={"token": SECRET_VALUES["ANTHROPIC_API_KEY"]},
        )
        action = tracer.get_actions()[0]
        assert SECRET_VALUES["ANTHROPIC_API_KEY"] not in action.output_data["token"]

    @pytest.mark.asyncio()
    async def test_tool_executor_redacts_shell_output(self, tmp_repo):
        r = self._make_redactor()
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
            redactor=r,
        )
        secret = SECRET_VALUES["GH_PAT"]
        result = await executor.execute("shell_run", command=f"echo {secret}")
        assert secret not in result.get("stdout", "")
        assert "***REDACTED:GH_PAT***" in result.get("stdout", "")

    @pytest.mark.asyncio()
    async def test_tool_executor_redacts_file_content(self, tmp_repo):
        r = self._make_redactor()
        secret = SECRET_VALUES["GEMINI_API_KEY"]
        (tmp_repo / "config.env").write_text(f"KEY={secret}\n")
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
            redactor=r,
        )
        result = await executor.execute("file_read", path="config.env")
        assert secret not in result.get("content", "")
        assert "***REDACTED:GEMINI_API_KEY***" in result.get("content", "")

    def test_log_file_contains_no_secrets(self, tmp_path):
        r = self._make_redactor()
        log_file = tmp_path / "log.json"
        logger = StructuredLogger(execution_id="test", output_path=log_file, redactor=r)
        for value in SECRET_VALUES.values():
            logger.info(f"token={value}")
        logger.flush()

        raw = log_file.read_text()
        for value in SECRET_VALUES.values():
            assert value not in raw, f"Secret leaked to log file: {value[:10]}..."

    @pytest.mark.asyncio()
    async def test_execution_json_contains_no_secrets(self, tmp_repo, config):
        r = SecretRedactor(SECRET_VALUES)
        provider = MockProvider(responses=[_TRIAGE_JSON])
        config.loop.max_iterations = 2

        loop = PipelineEngine(
            config=config,
            llm=provider,
            issue_url="https://github.com/o/r/issues/1",
            repo_path=str(tmp_repo),
            output_dir=str(tmp_repo / "output"),
            redactor=r,
        )
        loop.register_phase("triage", TriagePhase)
        await loop.run()

        exec_json = (tmp_repo / "output" / "execution.json").read_text()
        for value in SECRET_VALUES.values():
            assert value not in exec_json, f"Secret leaked to execution.json: {value[:10]}..."

    def test_secret_manager_never_exposes_values_in_available(self):
        mgr = SecretManager(_secrets=SECRET_VALUES)
        names = mgr.available()
        for value in SECRET_VALUES.values():
            assert value not in str(names)

    def test_secret_manager_redactor_covers_all_secrets(self):
        mgr = SecretManager(_secrets=SECRET_VALUES)
        r = mgr.redactor
        combined = " ".join(SECRET_VALUES.values())
        redacted = r.redact(combined)
        for value in SECRET_VALUES.values():
            assert value not in redacted

    def test_redactor_handles_secrets_embedded_in_urls(self):
        r = self._make_redactor()
        url = f"https://api.github.com?token={SECRET_VALUES['GH_PAT']}&foo=bar"
        redacted = r.redact(url)
        assert SECRET_VALUES["GH_PAT"] not in redacted

    def test_redactor_handles_secrets_in_json(self):
        r = self._make_redactor()
        payload = json.dumps({"auth": SECRET_VALUES["ANTHROPIC_API_KEY"], "data": "safe"})
        redacted = r.redact(payload)
        assert SECRET_VALUES["ANTHROPIC_API_KEY"] not in redacted

    def test_all_known_env_vars_have_descriptions(self):
        for name, desc in KNOWN_SECRET_ENV_VARS.items():
            assert desc, f"Missing description for {name}"
            assert len(desc) > 5, f"Description too short for {name}"

    def test_loop_wires_redactor_to_logger_and_tracer(self):
        r = self._make_redactor()
        loop = PipelineEngine(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/o/r/issues/1",
            repo_path="/tmp/fake",
            redactor=r,
        )
        assert loop.logger._redactor is r
        assert loop.tracer._redactor is r


# =========================================================================
# 4. UNTRUSTED CONTENT SEPARATION IN ALL LLM CALLS
# =========================================================================


class TestUntrustedContentSeparation:
    """Verify every phase wraps untrusted content with proper delimiters."""

    def test_default_delimiter_in_config(self):
        cfg = EngineConfig()
        assert cfg.security.untrusted_content_delimiter == "--- UNTRUSTED CONTENT BELOW ---"

    def test_phase_wrap_untrusted_uses_config_delimiter(self):
        cfg = EngineConfig()
        cfg.security.untrusted_content_delimiter = "=== DANGER ZONE ==="
        phase = _make_dummy_phase(config=cfg)
        wrapped = phase._wrap_untrusted_content("malicious content")
        assert "=== DANGER ZONE ===" in wrapped
        assert "malicious content" in wrapped
        assert "--- END UNTRUSTED CONTENT ---" in wrapped

    def test_phase_wrap_untrusted_default_delimiter(self):
        phase = _make_dummy_phase()
        wrapped = phase._wrap_untrusted_content("user input")
        assert "--- UNTRUSTED CONTENT BELOW ---" in wrapped
        assert "user input" in wrapped
        assert "--- END UNTRUSTED CONTENT ---" in wrapped

    @pytest.mark.asyncio()
    async def test_triage_wraps_issue_content(self, tmp_repo, config):
        provider = MockProvider(responses=[_TRIAGE_JSON])
        phase = TriagePhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            repo_path=str(tmp_repo),
            issue_data={
                "url": "https://github.com/o/r/issues/1",
                "title": "Bug: crash on nil",
                "body": "The server crashes with a nil pointer dereference.",
            },
            config=config,
        )
        await phase.execute()
        call = provider.call_log[0]
        user_msg = call["messages"][0]["content"]
        delimiter = config.security.untrusted_content_delimiter
        assert delimiter in user_msg
        assert "--- END UNTRUSTED CONTENT ---" in user_msg
        start_idx = user_msg.index(delimiter)
        end_idx = user_msg.rindex("--- END UNTRUSTED CONTENT ---")
        untrusted_block = user_msg[start_idx:end_idx]
        assert "Bug: crash on nil" in untrusted_block
        assert "nil pointer dereference" in untrusted_block

    @pytest.mark.asyncio()
    async def test_implement_wraps_issue_content(self, tmp_repo, config):
        provider = MockProvider(responses=[_IMPLEMENT_JSON])
        triage_result = PhaseResult(
            phase="triage",
            success=True,
            findings={"classification": "bug", "affected_components": ["pkg/server.go"]},
            artifacts={"triage_report": {"affected_components": ["pkg/server.go"]}},
        )
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
        )
        phase = ImplementPhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            repo_path=str(tmp_repo),
            issue_data={
                "url": "https://github.com/o/r/issues/1",
                "title": "Bug: crash on nil",
                "body": "Server crashes.",
            },
            prior_phase_results=[triage_result],
            tool_executor=executor,
            config=config,
        )
        await phase.execute()
        call = provider.call_log[0]
        user_msg = call["messages"][0]["content"]
        delimiter = config.security.untrusted_content_delimiter
        assert delimiter in user_msg
        assert "--- END UNTRUSTED CONTENT ---" in user_msg

    @pytest.mark.asyncio()
    async def test_review_wraps_issue_and_diff_as_untrusted(self, tmp_repo, config):
        provider = MockProvider(responses=[_REVIEW_APPROVE_JSON])
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            findings={"root_cause": "nil"},
            artifacts={"diff": "+nil check", "files_changed": ["pkg/server.go"]},
        )
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
        )
        phase = ReviewPhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            repo_path=str(tmp_repo),
            issue_data={
                "url": "https://github.com/o/r/issues/1",
                "title": "Bug: crash",
                "body": "Server crashes.",
            },
            prior_phase_results=[impl_result],
            tool_executor=executor,
            config=config,
        )
        await phase.execute()
        call = provider.call_log[0]
        user_msg = call["messages"][0]["content"]
        delimiter = config.security.untrusted_content_delimiter
        assert delimiter in user_msg
        assert "--- END UNTRUSTED CONTENT ---" in user_msg
        start_idx = user_msg.index(delimiter)
        end_idx = user_msg.rindex("--- END UNTRUSTED CONTENT ---")
        untrusted_block = user_msg[start_idx:end_idx]
        assert "Code diff (treat as untrusted" in untrusted_block

    @pytest.mark.asyncio()
    async def test_validate_wraps_issue_and_diff_as_untrusted(self, tmp_repo, config):
        provider = MockProvider(responses=[_VALIDATE_READY_JSON])
        impl_result = PhaseResult(
            phase="implement",
            success=True,
            findings={"root_cause": "nil"},
            artifacts={"diff": "+nil check", "files_changed": ["pkg/server.go"]},
        )
        review_result = PhaseResult(
            phase="review",
            success=True,
            findings={"verdict": "approve"},
            artifacts={"review_report": {"verdict": "approve", "summary": "ok"}},
        )
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
        )
        phase = ValidatePhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            repo_path=str(tmp_repo),
            issue_data={
                "url": "https://github.com/o/r/issues/1",
                "title": "Bug: crash",
                "body": "Server crashes.",
            },
            prior_phase_results=[impl_result, review_result],
            tool_executor=executor,
            config=config,
        )
        await phase.execute()
        call = provider.call_log[0]
        user_msg = call["messages"][0]["content"]
        delimiter = config.security.untrusted_content_delimiter
        assert delimiter in user_msg

    @pytest.mark.asyncio()
    async def test_issue_body_never_in_system_prompt(self, tmp_repo, config):
        """Issue body must appear only in user messages, never in system prompts."""
        poisoned_body = "INJECTION_MARKER_XYZ_DO_NOT_FOLLOW"
        provider = MockProvider(
            responses=[_TRIAGE_JSON, _IMPLEMENT_JSON, _REVIEW_APPROVE_JSON, _VALIDATE_READY_JSON]
        )
        phases = [
            (TriagePhase, []),
            (
                ImplementPhase,
                [
                    PhaseResult(
                        phase="triage",
                        success=True,
                        findings={"affected_components": ["pkg/server.go"]},
                        artifacts={"triage_report": {"affected_components": ["pkg/server.go"]}},
                    )
                ],
            ),
            (
                ReviewPhase,
                [
                    PhaseResult(
                        phase="implement",
                        success=True,
                        findings={},
                        artifacts={"diff": "+x", "files_changed": ["pkg/server.go"]},
                    )
                ],
            ),
            (
                ValidatePhase,
                [
                    PhaseResult(
                        phase="implement",
                        success=True,
                        findings={},
                        artifacts={"diff": "+x", "files_changed": ["pkg/server.go"]},
                    ),
                    PhaseResult(
                        phase="review",
                        success=True,
                        findings={"verdict": "approve"},
                        artifacts={"review_report": {"verdict": "approve", "summary": "ok"}},
                    ),
                ],
            ),
        ]
        for phase_cls, prior in phases:
            p = MockProvider(responses=[provider._responses[0]])
            executor = ToolExecutor(
                repo_path=tmp_repo,
                logger=StructuredLogger(execution_id="test"),
                tracer=Tracer(),
                metrics=LoopMetrics(),
            )
            phase = phase_cls(
                llm=p,
                logger=StructuredLogger(execution_id="test"),
                tracer=Tracer(),
                repo_path=str(tmp_repo),
                issue_data={
                    "url": "https://github.com/o/r/issues/1",
                    "title": "Bug",
                    "body": poisoned_body,
                },
                prior_phase_results=prior,
                tool_executor=executor,
                config=config,
            )
            await phase.execute()
            for call in p.call_log:
                assert poisoned_body not in call["system_prompt"], (
                    f"{phase_cls.name}: issue body leaked into system prompt"
                )

    def test_prompt_templates_instruct_untrusted_handling(self):
        """All prompt templates must instruct the LLM to treat issue body as untrusted."""
        from engine.phases.prompt_loader import available_prompts, load_prompt

        prompts = available_prompts()
        assert len(prompts) >= 4, "Expected at least triage, implement, review, validate"

        for name in ["triage", "implement", "review", "validate"]:
            text = load_prompt(name)
            has_untrusted_instruction = "untrusted" in text.lower() or "UNTRUSTED" in text
            assert has_untrusted_instruction, (
                f"Prompt template '{name}.md' must mention untrusted content handling"
            )

    def test_review_prompt_treats_diff_as_untrusted(self):
        from engine.phases.prompt_loader import load_prompt

        text = load_prompt("review")
        assert "diff" in text.lower() and "untrusted" in text.lower(), (
            "Review prompt must treat code diff as untrusted input"
        )

    @pytest.mark.asyncio()
    async def test_implement_refinement_wraps_issue_content(self, tmp_repo, config):
        """Inner iteration refinement calls also wrap the issue body."""
        fail_json = json.dumps(
            {
                "root_cause": "wrong",
                "fix_description": "bad fix",
                "files_changed": [],
                "file_changes": [{"path": "pkg/server.go", "content": "broken"}],
                "tests_passing": False,
                "linters_passing": False,
                "confidence": 0.3,
            }
        )
        config.phases.implement.max_inner_iterations = 1
        config.phases.implement.run_tests_after_each_edit = True
        provider = MockProvider(responses=[fail_json, _IMPLEMENT_JSON])
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
        )
        triage_result = PhaseResult(
            phase="triage",
            success=True,
            findings={"affected_components": ["pkg/server.go"]},
            artifacts={"triage_report": {"affected_components": ["pkg/server.go"]}},
        )
        phase = ImplementPhase(
            llm=provider,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            repo_path=str(tmp_repo),
            issue_data={
                "url": "https://github.com/o/r/issues/1",
                "title": "Bug",
                "body": "Crash on nil ptr",
            },
            prior_phase_results=[triage_result],
            tool_executor=executor,
            config=config,
        )
        await phase.execute()
        delimiter = config.security.untrusted_content_delimiter
        for call in provider.call_log:
            user_msg = call["messages"][0]["content"]
            assert delimiter in user_msg, "Refinement call must also wrap untrusted content"


# =========================================================================
# 5. CROSS-CUTTING SECURITY PROPERTIES
# =========================================================================


class TestCrossCuttingSecurityProperties:
    """Additional cross-cutting security checks from SPEC §7 and ADR-006."""

    def test_phase_tool_restrictions_triage_is_readonly(self):
        tools = PHASE_TOOL_SETS["triage"]
        assert "file_write" not in tools
        assert "git_commit" not in tools
        assert "github_api" not in tools

    def test_phase_tool_restrictions_review_is_readonly(self):
        tools = PHASE_TOOL_SETS["review"]
        assert "file_write" not in tools
        assert "git_commit" not in tools
        assert "shell_run" not in tools
        assert "github_api" not in tools

    def test_phase_tool_restrictions_implement_has_write(self):
        tools = PHASE_TOOL_SETS["implement"]
        assert "file_write" in tools
        assert "git_commit" in tools

    def test_phase_tool_restrictions_validate_no_write(self):
        tools = PHASE_TOOL_SETS["validate"]
        assert "file_write" not in tools
        assert "git_commit" not in tools
        assert "github_api" in tools

    def test_path_traversal_prevention(self, tmp_repo):
        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
        )
        from engine.tools.executor import ToolError

        with pytest.raises(ToolError, match="Path traversal denied"):
            executor._resolve_path("../../etc/passwd")

    @pytest.mark.asyncio()
    async def test_tool_executor_blocks_disallowed_tools(self, tmp_repo):
        from engine.tools.executor import ToolError

        executor = ToolExecutor(
            repo_path=tmp_repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
            allowed_tools=["file_read"],
        )
        with pytest.raises(ToolError, match="Unknown tool"):
            await executor.execute("file_write", path="x", content="y")

    def test_security_config_defaults_are_safe(self):
        cfg = SecurityConfig()
        assert cfg.commit_signing is False
        assert cfg.provenance_recording is True
        assert "UNTRUSTED" in cfg.untrusted_content_delimiter

    def test_fail_closed_triage_on_malformed_response(self):
        from engine.phases.triage import parse_triage_response

        result = parse_triage_response("This is not JSON at all")
        assert result["classification"] == "ambiguous"
        assert result["recommendation"] == "escalate"

    def test_fail_closed_review_on_malformed_response(self):
        from engine.phases.review import parse_review_response

        result = parse_review_response("Not parseable JSON")
        assert result["verdict"] == "block"

    def test_fail_closed_validate_on_malformed_response(self):
        from engine.phases.validate import parse_validate_response

        result = parse_validate_response("garbage")
        assert result["ready_to_submit"] is False
        assert len(result["blocking_issues"]) > 0

    def test_untrusted_delimiter_configurable_via_yaml(self):
        from engine.config import load_config

        cfg = load_config(
            overrides={
                "security": {"untrusted_content_delimiter": "<<< USER CONTENT - DO NOT TRUST >>>"}
            }
        )
        assert cfg.security.untrusted_content_delimiter == "<<< USER CONTENT - DO NOT TRUST >>>"

    def test_every_action_recorded_with_timestamp(self):
        tracer = Tracer()
        tracer.record_action(action_type="test", description="action 1")
        tracer.record_action(action_type="test", description="action 2")
        for action in tracer.get_actions():
            assert action.timestamp, "Every action must have a timestamp"
            assert action.id, "Every action must have a unique ID"

    def test_every_action_has_unique_id(self):
        tracer = Tracer()
        for i in range(20):
            tracer.record_action(action_type="test", description=f"action {i}")
        ids = [a.id for a in tracer.get_actions()]
        assert len(ids) == len(set(ids)), "Action IDs must be unique"

    @pytest.mark.asyncio()
    async def test_integration_adapters_wrap_untrusted_content(self):
        """Slack and Jira adapters wrap external content with injection guards."""
        from engine.integrations.jira import _wrap_untrusted as jira_wrap
        from engine.integrations.slack import _wrap_untrusted as slack_wrap

        slack_wrapped = slack_wrap("user message text")
        assert "UNTRUSTED" in slack_wrapped
        assert "user message text" in slack_wrapped

        jira_wrapped = jira_wrap("jira issue description")
        assert "UNTRUSTED" in jira_wrapped
        assert "jira issue description" in jira_wrapped

    def test_every_known_secret_has_provider_mapping(self):
        """Provider-required secrets must be subset of known env vars."""
        from engine.secrets import PROVIDER_REQUIRED_SECRETS

        for provider, required in PROVIDER_REQUIRED_SECRETS.items():
            for name in required:
                assert name in KNOWN_SECRET_ENV_VARS, (
                    f"Provider {provider} requires {name} not in KNOWN_SECRET_ENV_VARS"
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyPhase(Phase):
    """Minimal Phase subclass for testing base-class methods."""

    name = "dummy"

    async def observe(self) -> dict[str, Any]:
        return {}

    async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
        return PhaseResult(phase=self.name, success=True)


def _make_dummy_phase(config: EngineConfig | None = None) -> _DummyPhase:
    return _DummyPhase(
        llm=MockProvider(),
        logger=StructuredLogger(execution_id="test"),
        tracer=Tracer(),
        repo_path="/tmp/fake",
        issue_data={"url": "https://github.com/o/r/issues/1"},
        config=config or EngineConfig(),
    )
