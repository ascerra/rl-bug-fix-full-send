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
    max_tokens: int = 65536
    fallback_provider: str = "anthropic"
    fallback_model: str = "claude-sonnet-4-20250514"


@dataclass
class LoopConfig:
    max_iterations: int = 10
    time_budget_minutes: int = 30
    escalation_on_iteration_cap: str = "human"
    escalation_on_time_budget: str = "human"
    escalation_on_review_block_after: int = 3
    retry_backoff_base_seconds: float = 1.0
    retry_backoff_max_seconds: float = 4.0


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
class TriagePhaseConfig:
    enabled: bool = True
    classify_bug_vs_feature: bool = True
    attempt_reproduction: bool = True
    write_failing_test: bool = True


@dataclass
class ImplementPhaseConfig:
    enabled: bool = True
    max_inner_iterations: int = 5
    run_tests_after_each_edit: bool = True
    run_linters: bool = True
    max_parse_retries: int = 3
    test_command: str = ""
    lint_command: str = ""


@dataclass
class ReviewPhaseConfig:
    enabled: bool = True
    correctness: bool = True
    intent_alignment: bool = True
    security: bool = True
    style: bool = True
    scope_check: bool = True


@dataclass
class ValidatePhaseConfig:
    enabled: bool = True
    full_test_suite: bool = True
    ci_equivalent: bool = True
    minimal_diff_check: bool = True
    test_command: str = ""
    lint_command: str = ""


@dataclass
class ReportPhaseConfig:
    enabled: bool = True


@dataclass
class GitHubIntegrationConfig:
    enabled: bool = True
    commit_signing: bool = True
    signing_method: str = "gitsign"


@dataclass
class SlackIntegrationConfig:
    enabled: bool = False
    channel: str = ""


@dataclass
class JiraIntegrationConfig:
    enabled: bool = False
    project: str = ""
    server_url: str = ""


@dataclass
class IntegrationsConfig:
    github: GitHubIntegrationConfig = field(default_factory=GitHubIntegrationConfig)
    slack: SlackIntegrationConfig = field(default_factory=SlackIntegrationConfig)
    jira: JiraIntegrationConfig = field(default_factory=JiraIntegrationConfig)


@dataclass
class PhasesConfig:
    triage: TriagePhaseConfig = field(default_factory=TriagePhaseConfig)
    implement: ImplementPhaseConfig = field(default_factory=ImplementPhaseConfig)
    review: ReviewPhaseConfig = field(default_factory=ReviewPhaseConfig)
    validate: ValidatePhaseConfig = field(default_factory=ValidatePhaseConfig)
    report: ReportPhaseConfig = field(default_factory=ReportPhaseConfig)


@dataclass
class EngineConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    loop: LoopConfig = field(default_factory=LoopConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    phases: PhasesConfig = field(default_factory=PhasesConfig)
    integrations: IntegrationsConfig = field(default_factory=IntegrationsConfig)


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
    if "phases" in raw:
        _apply_phases_config(config.phases, raw["phases"])
    if "integrations" in raw:
        _apply_integrations_config(config.integrations, raw["integrations"])
    return config


_PHASE_CONFIG_MAP: dict[str, str] = {
    "triage": "triage",
    "implement": "implement",
    "review": "review",
    "validate": "validate",
    "report": "report",
}


def _apply_phases_config(phases: PhasesConfig, raw_phases: dict[str, Any]) -> None:
    """Apply per-phase raw YAML into PhasesConfig sub-dataclasses."""
    for phase_key, attr_name in _PHASE_CONFIG_MAP.items():
        if phase_key in raw_phases:
            phase_cfg = getattr(phases, attr_name)
            for k, v in raw_phases[phase_key].items():
                if hasattr(phase_cfg, k):
                    setattr(phase_cfg, k, v)


_INTEGRATION_CONFIG_MAP: dict[str, str] = {
    "github": "github",
    "slack": "slack",
    "jira": "jira",
}


def _apply_integrations_config(
    integrations: IntegrationsConfig, raw_integrations: dict[str, Any]
) -> None:
    """Apply per-integration raw YAML into IntegrationsConfig sub-dataclasses."""
    for integration_key, attr_name in _INTEGRATION_CONFIG_MAP.items():
        if integration_key in raw_integrations:
            integration_cfg = getattr(integrations, attr_name)
            for k, v in raw_integrations[integration_key].items():
                if hasattr(integration_cfg, k):
                    setattr(integration_cfg, k, v)
