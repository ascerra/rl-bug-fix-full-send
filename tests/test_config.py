"""Tests for configuration loading and validation."""

from engine.config import EngineConfig, load_config


def test_default_config():
    config = EngineConfig()
    assert config.llm.provider == "gemini"
    assert config.loop.max_iterations == 10
    assert config.loop.time_budget_minutes == 30
    assert config.security.commit_signing is False


def test_load_config_with_overrides():
    overrides = {
        "llm": {"provider": "anthropic", "model": "claude-sonnet-4-20250514"},
        "ralph_loop": {"max_iterations": 5},
    }
    config = load_config(overrides=overrides)
    assert config.llm.provider == "anthropic"
    assert config.llm.model == "claude-sonnet-4-20250514"
    assert config.loop.max_iterations == 5
    assert config.loop.time_budget_minutes == 30  # unchanged default


def test_load_config_nonexistent_file():
    config = load_config(config_path="/nonexistent/path.yaml")
    assert config.llm.provider == "gemini"  # falls back to defaults
