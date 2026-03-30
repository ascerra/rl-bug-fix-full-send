"""Tests for secret management — redaction, validation, and integration with observability."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.secrets import (
    KNOWN_SECRET_ENV_VARS,
    MIN_SECRET_LENGTH,
    PROVIDER_REQUIRED_SECRETS,
    REDACTED_PLACEHOLDER,
    SecretManager,
    SecretRedactor,
    noop_redactor,
)

# ------------------------------------------------------------------
# SecretRedactor — core redaction logic
# ------------------------------------------------------------------


class TestSecretRedactor:
    def test_redact_single_secret(self):
        r = SecretRedactor({"API_KEY": "super-secret-key-12345"})
        text = "Authorization: Bearer super-secret-key-12345"
        result = r.redact(text)
        assert "super-secret-key-12345" not in result
        assert "***REDACTED:API_KEY***" in result

    def test_redact_multiple_secrets(self):
        r = SecretRedactor(
            {
                "GEMINI_API_KEY": "gem-abc-123",
                "GH_PAT": "ghp_XyZ789Token",
            }
        )
        text = "Keys: gem-abc-123 and ghp_XyZ789Token end"
        result = r.redact(text)
        assert "gem-abc-123" not in result
        assert "ghp_XyZ789Token" not in result
        assert "***REDACTED:GEMINI_API_KEY***" in result
        assert "***REDACTED:GH_PAT***" in result

    def test_redact_preserves_non_secret_text(self):
        r = SecretRedactor({"KEY": "secretvalue"})
        text = "This is safe text without secrets"
        assert r.redact(text) == text

    def test_redact_multiple_occurrences(self):
        r = SecretRedactor({"KEY": "token123"})
        text = "first token123 and second token123"
        result = r.redact(text)
        assert result.count("***REDACTED:KEY***") == 2
        assert "token123" not in result

    def test_redact_ignores_short_secrets(self):
        r = SecretRedactor({"KEY": "ab"})
        text = "ab is short and ab should not be redacted"
        assert r.redact(text) == text

    def test_redact_boundary_length(self):
        short = "x" * (MIN_SECRET_LENGTH - 1)
        r_short = SecretRedactor({"KEY": short})
        assert r_short.redact(f"test {short} end") == f"test {short} end"

        exact = "x" * MIN_SECRET_LENGTH
        r_exact = SecretRedactor({"KEY": exact})
        result = r_exact.redact(f"test {exact} end")
        assert exact not in result
        assert "***REDACTED:KEY***" in result

    def test_redact_empty_value_ignored(self):
        r = SecretRedactor({"KEY": ""})
        text = "nothing to redact"
        assert r.redact(text) == text

    def test_redact_special_regex_chars(self):
        r = SecretRedactor({"KEY": "secret+value.with*chars"})
        text = "has secret+value.with*chars inside"
        result = r.redact(text)
        assert "secret+value.with*chars" not in result
        assert "***REDACTED:KEY***" in result

    def test_redact_value_string(self):
        r = SecretRedactor({"KEY": "mysecret"})
        assert "***REDACTED:KEY***" in r.redact_value("contains mysecret")

    def test_redact_value_non_string(self):
        r = SecretRedactor({"KEY": "mysecret"})
        assert r.redact_value(42) == 42
        assert r.redact_value(None) is None
        assert r.redact_value(True) is True

    def test_redact_dict_shallow(self):
        r = SecretRedactor({"KEY": "secret123"})
        data = {"output": "got secret123 here", "count": 5}
        result = r.redact_dict(data)
        assert "secret123" not in result["output"]
        assert "***REDACTED:KEY***" in result["output"]
        assert result["count"] == 5

    def test_redact_dict_nested(self):
        r = SecretRedactor({"KEY": "secret123"})
        data = {"outer": {"inner": "has secret123"}, "safe": "ok"}
        result = r.redact_dict(data)
        assert "secret123" not in result["outer"]["inner"]
        assert result["safe"] == "ok"

    def test_redact_dict_list_values(self):
        r = SecretRedactor({"KEY": "secret123"})
        data = {"items": ["safe", "has secret123", "also safe"]}
        result = r.redact_dict(data)
        assert "secret123" not in result["items"][1]
        assert result["items"][0] == "safe"
        assert result["items"][2] == "also safe"

    def test_redact_dict_preserves_non_string_list_items(self):
        r = SecretRedactor({"KEY": "secret123"})
        data = {"items": [1, True, None, "has secret123"]}
        result = r.redact_dict(data)
        assert result["items"][0] == 1
        assert result["items"][1] is True
        assert result["items"][2] is None
        assert "***REDACTED:KEY***" in result["items"][3]

    def test_empty_redactor(self):
        r = SecretRedactor({})
        text = "anything goes here"
        assert r.redact(text) == text
        assert r.redact_dict({"a": "b"}) == {"a": "b"}


class TestNoopRedactor:
    def test_noop_does_nothing(self):
        r = noop_redactor()
        assert r.redact("any text here") == "any text here"

    def test_noop_singleton(self):
        assert noop_redactor() is noop_redactor()


# ------------------------------------------------------------------
# SecretManager — environment loading and validation
# ------------------------------------------------------------------


class TestSecretManagerFromEnvironment:
    def test_loads_from_env(self):
        env = {"GEMINI_API_KEY": "gem-key-abc", "GH_PAT": "ghp_token123"}
        with patch.dict(os.environ, env, clear=False):
            mgr = SecretManager.from_environment()
        assert mgr.is_available("GEMINI_API_KEY")
        assert mgr.is_available("GH_PAT")
        assert mgr.get("GEMINI_API_KEY") == "gem-key-abc"
        assert mgr.get("GH_PAT") == "ghp_token123"

    def test_empty_env(self):
        env = {k: "" for k in KNOWN_SECRET_ENV_VARS}
        with patch.dict(os.environ, env, clear=False):
            for k in KNOWN_SECRET_ENV_VARS:
                os.environ.pop(k, None)
            mgr = SecretManager.from_environment()
        assert mgr.available() == []

    def test_partial_env(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "key"}, clear=False):
            for k in KNOWN_SECRET_ENV_VARS:
                if k != "GEMINI_API_KEY":
                    os.environ.pop(k, None)
            mgr = SecretManager.from_environment()
        assert "GEMINI_API_KEY" in mgr.available()
        assert mgr.get("ANTHROPIC_API_KEY") is None

    def test_ignores_unknown_env_vars(self):
        with patch.dict(os.environ, {"RANDOM_SECRET": "val"}, clear=False):
            for k in KNOWN_SECRET_ENV_VARS:
                os.environ.pop(k, None)
            mgr = SecretManager.from_environment()
        assert "RANDOM_SECRET" not in mgr.available()


class TestSecretManagerGet:
    def test_get_existing(self):
        mgr = SecretManager(_secrets={"KEY": "val"})
        assert mgr.get("KEY") == "val"

    def test_get_missing(self):
        mgr = SecretManager(_secrets={})
        assert mgr.get("KEY") is None

    def test_get_empty_string_is_none(self):
        mgr = SecretManager(_secrets={"KEY": ""})
        assert mgr.get("KEY") is None


class TestSecretManagerAvailable:
    def test_available_sorted(self):
        mgr = SecretManager(_secrets={"C": "1", "A": "2", "B": "3"})
        assert mgr.available() == ["A", "B", "C"]

    def test_available_empty(self):
        mgr = SecretManager(_secrets={})
        assert mgr.available() == []

    def test_is_available_true(self):
        mgr = SecretManager(_secrets={"KEY": "val"})
        assert mgr.is_available("KEY") is True

    def test_is_available_false(self):
        mgr = SecretManager(_secrets={})
        assert mgr.is_available("KEY") is False

    def test_is_available_empty_string(self):
        mgr = SecretManager(_secrets={"KEY": ""})
        assert mgr.is_available("KEY") is False


class TestSecretManagerValidation:
    def test_validate_gemini_missing(self):
        mgr = SecretManager(_secrets={})
        missing = mgr.validate_for_provider("gemini")
        assert "GEMINI_API_KEY" in missing

    def test_validate_gemini_present(self):
        mgr = SecretManager(_secrets={"GEMINI_API_KEY": "key"})
        assert mgr.validate_for_provider("gemini") == []

    def test_validate_anthropic_missing(self):
        mgr = SecretManager(_secrets={})
        missing = mgr.validate_for_provider("anthropic")
        assert "ANTHROPIC_API_KEY" in missing

    def test_validate_anthropic_present(self):
        mgr = SecretManager(_secrets={"ANTHROPIC_API_KEY": "key"})
        assert mgr.validate_for_provider("anthropic") == []

    def test_validate_mock_always_passes(self):
        mgr = SecretManager(_secrets={})
        assert mgr.validate_for_provider("mock") == []

    def test_validate_unknown_provider_passes(self):
        mgr = SecretManager(_secrets={})
        assert mgr.validate_for_provider("unknown_provider") == []

    def test_require_raises_on_missing(self):
        mgr = SecretManager(_secrets={})
        with pytest.raises(RuntimeError, match="Missing required secrets"):
            mgr.require_for_provider("gemini")

    def test_require_includes_description(self):
        mgr = SecretManager(_secrets={})
        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            mgr.require_for_provider("gemini")

    def test_require_passes_when_present(self):
        mgr = SecretManager(_secrets={"GEMINI_API_KEY": "key"})
        mgr.require_for_provider("gemini")

    def test_require_mock_always_passes(self):
        mgr = SecretManager(_secrets={})
        mgr.require_for_provider("mock")


class TestSecretManagerRedactor:
    def test_redactor_property(self):
        mgr = SecretManager(_secrets={"KEY": "mysecret"})
        r = mgr.redactor
        assert isinstance(r, SecretRedactor)
        assert "***REDACTED:KEY***" in r.redact("has mysecret here")

    def test_redactor_cached(self):
        mgr = SecretManager(_secrets={"KEY": "val"})
        assert mgr.redactor is mgr.redactor

    def test_redactor_empty_secrets(self):
        mgr = SecretManager(_secrets={})
        r = mgr.redactor
        assert r.redact("anything") == "anything"


# ------------------------------------------------------------------
# Logger integration — secrets redacted from log output
# ------------------------------------------------------------------


class TestLoggerRedaction:
    def test_log_message_redacted(self):
        from engine.observability.logger import StructuredLogger

        r = SecretRedactor({"KEY": "secret123"})
        logger = StructuredLogger(execution_id="test", redactor=r)
        logger.info("Connecting with secret123 token")

        entries = logger.get_entries()
        assert len(entries) == 1
        assert "secret123" not in entries[0]["message"]
        assert "***REDACTED:KEY***" in entries[0]["message"]

    def test_log_extra_kwargs_redacted(self):
        from engine.observability.logger import StructuredLogger

        r = SecretRedactor({"TOKEN": "ghp_abc123"})
        logger = StructuredLogger(execution_id="test", redactor=r)
        logger.info("API call", auth="Bearer ghp_abc123", url="https://api.github.com")

        entries = logger.get_entries()
        assert "ghp_abc123" not in entries[0]["auth"]
        assert entries[0]["url"] == "https://api.github.com"

    def test_log_without_redactor(self):
        from engine.observability.logger import StructuredLogger

        logger = StructuredLogger(execution_id="test")
        logger.info("message with secret123")

        entries = logger.get_entries()
        assert entries[0]["message"] == "message with secret123"

    def test_log_file_output_redacted(self, tmp_path):
        import json

        from engine.observability.logger import StructuredLogger

        r = SecretRedactor({"KEY": "secret123"})
        log_file = tmp_path / "log.json"
        logger = StructuredLogger(execution_id="test", output_path=log_file, redactor=r)
        logger.info("token secret123 used")
        logger.flush()

        data = json.loads(log_file.read_text())
        assert "secret123" not in data[0]["message"]


# ------------------------------------------------------------------
# Tracer integration — secrets redacted from action records
# ------------------------------------------------------------------


class TestTracerRedaction:
    def test_action_description_redacted(self):
        from engine.observability.tracer import Tracer

        r = SecretRedactor({"KEY": "token456"})
        tracer = Tracer(redactor=r)
        tracer.record_action(
            action_type="api_call",
            description="Called with token456",
        )
        actions = tracer.get_actions()
        assert "token456" not in actions[0].input_description
        assert "***REDACTED:KEY***" in actions[0].input_description

    def test_input_context_redacted(self):
        from engine.observability.tracer import Tracer

        r = SecretRedactor({"KEY": "token456"})
        tracer = Tracer(redactor=r)
        tracer.record_action(
            action_type="api_call",
            description="safe",
            input_context={"header": "Bearer token456"},
        )
        actions = tracer.get_actions()
        assert "token456" not in actions[0].input_context["header"]

    def test_output_data_redacted(self):
        from engine.observability.tracer import Tracer

        r = SecretRedactor({"KEY": "token456"})
        tracer = Tracer(redactor=r)
        tracer.record_action(
            action_type="api_call",
            description="safe",
            output_data={"body": "response had token456"},
        )
        actions = tracer.get_actions()
        assert "token456" not in actions[0].output_data["body"]

    def test_tracer_without_redactor(self):
        from engine.observability.tracer import Tracer

        tracer = Tracer()
        tracer.record_action(
            action_type="test",
            description="has token456",
            input_context={"key": "token456"},
        )
        actions = tracer.get_actions()
        assert actions[0].input_description == "has token456"
        assert actions[0].input_context["key"] == "token456"


# ------------------------------------------------------------------
# ToolExecutor integration — secrets redacted from tool output
# ------------------------------------------------------------------


class TestToolExecutorRedaction:
    @pytest.fixture()
    def tmp_repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "test.txt").write_text("content here\n")
        os.system(f"cd {repo} && git init -b main && git add -A && git commit -m 'init' --quiet")
        return repo

    @pytest.fixture()
    def redactor(self) -> SecretRedactor:
        return SecretRedactor({"TOKEN": "ghp_SuperSecretToken"})

    def _make_executor(self, repo: Path, redactor: SecretRedactor | None = None):
        from engine.observability.logger import StructuredLogger
        from engine.observability.metrics import LoopMetrics
        from engine.observability.tracer import Tracer
        from engine.tools.executor import ToolExecutor

        return ToolExecutor(
            repo_path=repo,
            logger=StructuredLogger(execution_id="test"),
            tracer=Tracer(),
            metrics=LoopMetrics(),
            shell_timeout=10,
            redactor=redactor,
        )

    @pytest.mark.asyncio()
    async def test_shell_output_redacted(self, tmp_repo, redactor):
        executor = self._make_executor(tmp_repo, redactor)
        with patch.dict(os.environ, {"TOKEN": "ghp_SuperSecretToken"}):
            result = await executor.execute("shell_run", command="echo ghp_SuperSecretToken")
        assert "ghp_SuperSecretToken" not in result.get("stdout", "")
        assert "***REDACTED:TOKEN***" in result.get("stdout", "")

    @pytest.mark.asyncio()
    async def test_shell_stderr_redacted(self, tmp_repo, redactor):
        executor = self._make_executor(tmp_repo, redactor)
        result = await executor.execute("shell_run", command="echo ghp_SuperSecretToken >&2")
        assert "ghp_SuperSecretToken" not in result.get("stderr", "")

    @pytest.mark.asyncio()
    async def test_file_content_redacted(self, tmp_repo, redactor):
        (tmp_repo / "secret.txt").write_text("token is ghp_SuperSecretToken\n")
        executor = self._make_executor(tmp_repo, redactor)
        result = await executor.execute("file_read", path="secret.txt")
        assert "ghp_SuperSecretToken" not in result.get("content", "")
        assert "***REDACTED:TOKEN***" in result.get("content", "")

    @pytest.mark.asyncio()
    async def test_no_redaction_without_redactor(self, tmp_repo):
        (tmp_repo / "secret.txt").write_text("token is ghp_SuperSecretToken\n")
        executor = self._make_executor(tmp_repo, redactor=None)
        result = await executor.execute("file_read", path="secret.txt")
        assert "ghp_SuperSecretToken" in result.get("content", "")


# ------------------------------------------------------------------
# CLI integration — secret validation on startup
# ------------------------------------------------------------------


class TestCLISecretValidation:
    def test_mock_provider_no_validation_needed(self, tmp_path):
        from engine.__main__ import main

        repo = tmp_path / "repo"
        repo.mkdir()

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_instance = mock_loop_cls.return_value

            async def _run():
                from engine.loop import ExecutionRecord

                rec = ExecutionRecord()
                rec.result = {"status": "success", "total_iterations": 1}
                return rec

            mock_instance.run = _run
            mock_instance.register_phase = lambda name, cls: None

            exit_code = main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(tmp_path / "out"),
                    "--provider",
                    "mock",
                ]
            )
        assert exit_code == 0

    def test_gemini_provider_fails_without_key(self, tmp_path):
        from engine.__main__ import main

        repo = tmp_path / "repo"
        repo.mkdir()

        env = {k: "" for k in KNOWN_SECRET_ENV_VARS}
        with patch.dict(os.environ, env, clear=False):
            for k in KNOWN_SECRET_ENV_VARS:
                os.environ.pop(k, None)
            with pytest.raises(RuntimeError, match="Missing required secrets"):
                main(
                    [
                        "--issue-url",
                        "https://github.com/org/repo/issues/1",
                        "--target-repo",
                        str(repo),
                        "--output-dir",
                        str(tmp_path / "out"),
                        "--provider",
                        "gemini",
                    ]
                )

    def test_redactor_passed_to_loop(self, tmp_path):
        from engine.__main__ import main

        repo = tmp_path / "repo"
        repo.mkdir()

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_instance = mock_loop_cls.return_value

            async def _run():
                from engine.loop import ExecutionRecord

                rec = ExecutionRecord()
                rec.result = {"status": "success", "total_iterations": 1}
                return rec

            mock_instance.run = _run
            mock_instance.register_phase = lambda name, cls: None

            main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(tmp_path / "out"),
                    "--provider",
                    "mock",
                ]
            )

        call_kwargs = mock_loop_cls.call_args[1]
        assert "redactor" in call_kwargs
        assert isinstance(call_kwargs["redactor"], SecretRedactor)


# ------------------------------------------------------------------
# Loop integration — redactor wired through to observability
# ------------------------------------------------------------------


class TestLoopRedactorWiring:
    def test_loop_creates_logger_with_redactor(self):
        from engine.config import EngineConfig
        from engine.integrations.llm import MockProvider
        from engine.loop import PipelineEngine

        r = SecretRedactor({"KEY": "val"})
        loop = PipelineEngine(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/org/repo/issues/1",
            repo_path="/tmp/fake",
            redactor=r,
        )
        assert loop.logger._redactor is r

    def test_loop_creates_tracer_with_redactor(self):
        from engine.config import EngineConfig
        from engine.integrations.llm import MockProvider
        from engine.loop import PipelineEngine

        r = SecretRedactor({"KEY": "val"})
        loop = PipelineEngine(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/org/repo/issues/1",
            repo_path="/tmp/fake",
            redactor=r,
        )
        assert loop.tracer._redactor is r

    def test_loop_without_redactor(self):
        from engine.config import EngineConfig
        from engine.integrations.llm import MockProvider
        from engine.loop import PipelineEngine

        loop = PipelineEngine(
            config=EngineConfig(),
            llm=MockProvider(),
            issue_url="https://github.com/org/repo/issues/1",
            repo_path="/tmp/fake",
        )
        assert loop.logger._redactor is None
        assert loop.tracer._redactor is None


# ------------------------------------------------------------------
# Constants and module-level checks
# ------------------------------------------------------------------


class TestConstants:
    def test_known_env_vars_include_all_providers(self):
        for provider, required in PROVIDER_REQUIRED_SECRETS.items():
            for secret_name in required:
                assert secret_name in KNOWN_SECRET_ENV_VARS, (
                    f"Provider {provider} requires {secret_name} "
                    f"but it's not in KNOWN_SECRET_ENV_VARS"
                )

    def test_placeholder_format(self):
        result = REDACTED_PLACEHOLDER.format(name="FOO")
        assert result == "***REDACTED:FOO***"

    def test_min_secret_length_positive(self):
        assert MIN_SECRET_LENGTH > 0
