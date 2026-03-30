"""Tests for the CLI entry point — arg parsing, config overrides, main() integration."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from engine.__main__ import build_overrides, main, parse_args, parse_config_override
from engine.config import EngineConfig

# ------------------------------------------------------------------
# parse_config_override
# ------------------------------------------------------------------


class TestParseConfigOverride:
    def test_empty_string(self):
        assert parse_config_override("") == {}

    def test_whitespace_only(self):
        assert parse_config_override("   ") == {}

    def test_none_like_empty(self):
        assert parse_config_override("") == {}

    def test_valid_yaml_dict(self):
        result = parse_config_override("{llm: {provider: anthropic}}")
        assert result == {"llm": {"provider": "anthropic"}}

    def test_valid_yaml_multiline(self):
        override = "llm:\n  provider: anthropic\n  model: claude-sonnet-4-20250514"
        result = parse_config_override(override)
        assert result["llm"]["provider"] == "anthropic"
        assert result["llm"]["model"] == "claude-sonnet-4-20250514"

    def test_valid_yaml_nested(self):
        override = "{phases: {triage: {enabled: false}}}"
        result = parse_config_override(override)
        assert result["phases"]["triage"]["enabled"] is False

    def test_valid_yaml_loop_config_primary_key(self):
        override = "{loop: {max_iterations: 5, time_budget_minutes: 15}}"
        result = parse_config_override(override)
        assert result["loop"]["max_iterations"] == 5
        assert result["loop"]["time_budget_minutes"] == 15

    def test_valid_yaml_loop_config_ralph_loop_backward_compat(self):
        override = "{ralph_loop: {max_iterations: 5, time_budget_minutes: 15}}"
        result = parse_config_override(override)
        assert result["ralph_loop"]["max_iterations"] == 5
        assert result["ralph_loop"]["time_budget_minutes"] == 15

    def test_invalid_yaml_returns_empty(self, capsys):
        result = parse_config_override("{bad: yaml: [}")
        assert result == {}
        captured = capsys.readouterr()
        assert "Warning" in captured.out

    def test_non_dict_yaml_returns_empty(self):
        assert parse_config_override("just a string") == {}

    def test_yaml_list_returns_empty(self):
        assert parse_config_override("[1, 2, 3]") == {}

    def test_yaml_number_returns_empty(self):
        assert parse_config_override("42") == {}

    def test_yaml_boolean_returns_empty(self):
        assert parse_config_override("true") == {}


# ------------------------------------------------------------------
# parse_args
# ------------------------------------------------------------------


class TestParseArgs:
    def test_required_args_only(self):
        args = parse_args(
            [
                "--issue-url",
                "https://github.com/org/repo/issues/1",
                "--target-repo",
                "/tmp/repo",
            ]
        )
        assert args.issue_url == "https://github.com/org/repo/issues/1"
        assert args.target_repo == "/tmp/repo"
        assert args.comparison_ref == ""
        assert args.provider == ""
        assert args.output_dir == "./output"
        assert args.config == ""
        assert args.config_override == ""

    def test_all_args(self):
        args = parse_args(
            [
                "--issue-url",
                "https://github.com/org/repo/issues/42",
                "--target-repo",
                "/tmp/repo",
                "--comparison-ref",
                "abc123",
                "--provider",
                "anthropic",
                "--output-dir",
                "/tmp/out",
                "--config",
                "/tmp/config.yaml",
                "--config-override",
                "{llm: {provider: gemini}}",
            ]
        )
        assert args.issue_url == "https://github.com/org/repo/issues/42"
        assert args.target_repo == "/tmp/repo"
        assert args.comparison_ref == "abc123"
        assert args.provider == "anthropic"
        assert args.output_dir == "/tmp/out"
        assert args.config == "/tmp/config.yaml"
        assert args.config_override == "{llm: {provider: gemini}}"

    def test_missing_required_issue_url(self):
        with pytest.raises(SystemExit):
            parse_args(["--target-repo", "/tmp/repo"])

    def test_missing_required_target_repo(self):
        with pytest.raises(SystemExit):
            parse_args(["--issue-url", "https://github.com/org/repo/issues/1"])


# ------------------------------------------------------------------
# build_overrides
# ------------------------------------------------------------------


class TestBuildOverrides:
    def _make_args(self, **kwargs) -> Any:
        import argparse

        defaults = {
            "issue_url": "https://github.com/org/repo/issues/1",
            "target_repo": "/tmp/repo",
            "comparison_ref": "",
            "provider": "",
            "output_dir": "./output",
            "config": "",
            "config_override": "",
        }
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    def test_no_overrides(self):
        args = self._make_args()
        assert build_overrides(args) is None

    def test_provider_only(self):
        args = self._make_args(provider="anthropic")
        result = build_overrides(args)
        assert result == {"llm": {"provider": "anthropic"}}

    def test_config_override_only(self):
        args = self._make_args(config_override="{loop: {max_iterations: 3}}")
        result = build_overrides(args)
        assert result == {"loop": {"max_iterations": 3}}

    def test_provider_overrides_config_override_llm(self):
        """--provider flag takes precedence over llm.provider in --config-override."""
        args = self._make_args(
            provider="anthropic",
            config_override="{llm: {provider: gemini, model: gemini-2.5-pro}}",
        )
        result = build_overrides(args)
        assert result is not None
        assert result["llm"]["provider"] == "anthropic"
        assert result["llm"]["model"] == "gemini-2.5-pro"

    def test_config_override_and_provider_merge(self):
        args = self._make_args(
            provider="gemini",
            config_override="{loop: {max_iterations: 5}}",
        )
        result = build_overrides(args)
        assert result is not None
        assert result["llm"]["provider"] == "gemini"
        assert result["loop"]["max_iterations"] == 5

    def test_invalid_config_override_with_provider(self):
        args = self._make_args(provider="gemini", config_override="not valid yaml: [}")
        result = build_overrides(args)
        assert result == {"llm": {"provider": "gemini"}}


# ------------------------------------------------------------------
# Helpers for main() integration tests
# ------------------------------------------------------------------


def _make_fake_execution(status: str = "success", **extra_target):
    """Build a minimal ExecutionRecord for mock returns."""
    from engine.loop import ExecutionRecord

    rec = ExecutionRecord(
        trigger={"type": "github_issue", "source_url": "https://example.com"},
        target={"repo_path": "/tmp/test", **extra_target},
    )
    rec.result = {"status": status, "total_iterations": 5}
    return rec


def _async_return(value):
    """Create an async callable that returns a fixed value."""

    async def _coro():
        return value

    return _coro


# ------------------------------------------------------------------
# main() integration tests
#
# We mock PipelineEngine to avoid running real phases with MockProvider
# (MockProvider's canned responses cause triage escalation).
# This isolates CLI wiring from phase behavior.
# ------------------------------------------------------------------


class TestMain:
    def test_main_success(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        output = tmp_path / "output"

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_instance = mock_loop_cls.return_value
            mock_instance.run = _async_return(_make_fake_execution("success"))
            mock_instance.register_phase = lambda name, cls: None

            exit_code = main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(output),
                    "--provider",
                    "mock",
                ]
            )

        assert exit_code == 0
        mock_loop_cls.assert_called_once()
        call_kwargs = mock_loop_cls.call_args[1]
        assert call_kwargs["issue_url"] == "https://github.com/org/repo/issues/1"
        assert call_kwargs["repo_path"] == str(repo)
        assert call_kwargs["output_dir"] == str(output)

    def test_main_with_config_override(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        output = tmp_path / "output"

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
            patch("engine.__main__.load_config") as mock_load,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_load.return_value = EngineConfig()
            mock_instance = mock_loop_cls.return_value
            mock_instance.run = _async_return(_make_fake_execution("success"))
            mock_instance.register_phase = lambda name, cls: None

            exit_code = main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(output),
                    "--provider",
                    "mock",
                    "--config-override",
                    "{loop: {max_iterations: 20}}",
                ]
            )

        assert exit_code == 0
        load_kwargs = mock_load.call_args[1]
        overrides = load_kwargs.get("overrides")
        assert overrides is not None
        assert overrides["loop"]["max_iterations"] == 20
        assert overrides["llm"]["provider"] == "mock"

    def test_main_with_config_file(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        output = tmp_path / "output"
        config_file = tmp_path / "config.yaml"
        config_file.write_text("llm:\n  temperature: 0.5\n")

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
            patch("engine.__main__.load_config") as mock_load,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_load.return_value = EngineConfig()
            mock_instance = mock_loop_cls.return_value
            mock_instance.run = _async_return(_make_fake_execution("success"))
            mock_instance.register_phase = lambda name, cls: None

            exit_code = main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(output),
                    "--provider",
                    "mock",
                    "--config",
                    str(config_file),
                ]
            )

        assert exit_code == 0
        load_kwargs = mock_load.call_args[1]
        assert load_kwargs["config_path"] == str(config_file)

    def test_main_comparison_ref_passed(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        output = tmp_path / "output"

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_instance = mock_loop_cls.return_value
            mock_instance.run = _async_return(_make_fake_execution("success"))
            mock_instance.register_phase = lambda name, cls: None

            exit_code = main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(output),
                    "--provider",
                    "mock",
                    "--comparison-ref",
                    "abc123",
                ]
            )

        assert exit_code == 0
        call_kwargs = mock_loop_cls.call_args[1]
        assert call_kwargs["comparison_ref"] == "abc123"

    def test_main_failure_returns_nonzero(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        output = tmp_path / "output"

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_instance = mock_loop_cls.return_value
            mock_instance.run = _async_return(_make_fake_execution("escalated"))
            mock_instance.register_phase = lambda name, cls: None

            exit_code = main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(output),
                    "--provider",
                    "mock",
                ]
            )

        assert exit_code == 1

    def test_main_registers_all_four_phases(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        output = tmp_path / "output"
        registered: list[str] = []

        with (
            patch("engine.__main__.create_provider") as mock_create,
            patch("engine.__main__.PipelineEngine") as mock_loop_cls,
        ):
            from engine.integrations.llm import MockProvider

            mock_create.return_value = MockProvider()
            mock_instance = mock_loop_cls.return_value
            mock_instance.run = _async_return(_make_fake_execution("success"))
            mock_instance.register_phase = lambda name, cls: registered.append(name)

            main(
                [
                    "--issue-url",
                    "https://github.com/org/repo/issues/1",
                    "--target-repo",
                    str(repo),
                    "--output-dir",
                    str(output),
                    "--provider",
                    "mock",
                ]
            )

        assert "triage" in registered
        assert "implement" in registered
        assert "review" in registered
        assert "validate" in registered


# ------------------------------------------------------------------
# Config override integration with load_config
# ------------------------------------------------------------------


class TestConfigOverrideIntegration:
    def test_override_changes_loop_config_primary_key(self):
        from engine.config import load_config

        config = load_config(overrides={"loop": {"max_iterations": 3}})
        assert config.loop.max_iterations == 3

    def test_override_changes_loop_config_ralph_loop_backward_compat(self):
        from engine.config import load_config

        config = load_config(overrides={"ralph_loop": {"max_iterations": 3}})
        assert config.loop.max_iterations == 3

    def test_override_changes_llm_config(self):
        from engine.config import load_config

        config = load_config(overrides={"llm": {"provider": "anthropic", "temperature": 0.8}})
        assert config.llm.provider == "anthropic"
        assert config.llm.temperature == 0.8

    def test_override_changes_phase_config(self):
        from engine.config import load_config

        config = load_config(
            overrides={"phases": {"triage": {"enabled": False, "attempt_reproduction": False}}}
        )
        assert config.phases.triage.enabled is False
        assert config.phases.triage.attempt_reproduction is False

    def test_override_from_yaml_string(self):
        from engine.config import load_config

        yaml_str = "{loop: {max_iterations: 7}, llm: {temperature: 0.9}}"
        overrides = parse_config_override(yaml_str)
        config = load_config(overrides=overrides)
        assert config.loop.max_iterations == 7
        assert config.llm.temperature == 0.9

    def test_file_plus_override(self, tmp_path):
        from engine.config import load_config

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "llm:\n  provider: gemini\n  temperature: 0.1\nloop:\n  max_iterations: 15\n"
        )

        config = load_config(
            config_path=str(config_file),
            overrides={"llm": {"temperature": 0.5}},
        )
        assert config.llm.provider == "gemini"
        assert config.llm.temperature == 0.5
        assert config.loop.max_iterations == 15
