"""Configuration loading and validation for the Ralph Loop engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LLMConfig:
    provider: str = "gemini"
    model: str = "gemini-2.5-pro"
    temperature: float = 0.2
    max_tokens: int = 8192
    fallback_provider: str = "anthropic"
    fallback_model: str = "claude-sonnet-4-20250514"


@dataclass
class LoopConfig:
    max_iterations: int = 10
    time_budget_minutes: int = 30
    escalation_on_iteration_cap: str = "human"
    escalation_on_time_budget: str = "human"
    escalation_on_review_block_after: int = 3


@dataclass
class SecurityConfig:
    commit_signing: bool = True
    signing_method: str = "gitsign"
    provenance_recording: bool = True
    untrusted_content_delimiter: str = "--- UNTRUSTED CONTENT BELOW ---"


@dataclass
class ReportingConfig:
    decision_tree: bool = True
    action_map: bool = True
    comparison_mode: bool = False
    publish_to_pages: bool = False
    artifact_retention_days: int = 30


@dataclass
class EngineConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)


def load_config(
    config_path: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> EngineConfig:
    """Load configuration from YAML file with optional overrides."""
    config = EngineConfig()

    if config_path:
        path = Path(config_path)
        if path.exists():
            with path.open() as f:
                raw = yaml.safe_load(f) or {}
            config = _apply_raw_config(config, raw)

    if overrides:
        config = _apply_raw_config(config, overrides)

    return config


def _apply_raw_config(config: EngineConfig, raw: dict[str, Any]) -> EngineConfig:
    """Apply raw YAML dict to config dataclass. Shallow merge per section."""
    if "llm" in raw:
        for k, v in raw["llm"].items():
            if hasattr(config.llm, k):
                setattr(config.llm, k, v)
    if "ralph_loop" in raw:
        for k, v in raw["ralph_loop"].items():
            if hasattr(config.loop, k):
                setattr(config.loop, k, v)
    if "security" in raw:
        for k, v in raw["security"].items():
            if hasattr(config.security, k):
                setattr(config.security, k, v)
    if "reporting" in raw:
        for k, v in raw["reporting"].items():
            if hasattr(config.reporting, k):
                setattr(config.reporting, k, v)
    return config
