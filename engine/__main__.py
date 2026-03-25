"""CLI entry point for the Ralph Loop engine."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import yaml

from engine.config import load_config
from engine.integrations.llm import create_provider
from engine.loop import RalphLoop
from engine.phases.implement import ImplementPhase
from engine.phases.review import ReviewPhase
from engine.phases.triage import TriagePhase
from engine.phases.validate import ValidatePhase
from engine.secrets import SecretManager
from engine.workflow.monitor import WorkflowMonitor


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rl-engine",
        description="Ralph Loop Bug Fix Engine — agentic SDLC for GitHub organizations",
    )
    parser.add_argument("--issue-url", required=True, help="GitHub issue URL to process")
    parser.add_argument("--target-repo", required=True, help="Path to cloned target repository")
    parser.add_argument("--comparison-ref", default="", help="Git ref of human fix for comparison")
    parser.add_argument(
        "--provider", default="", help="LLM provider override (gemini|anthropic|mock)"
    )
    parser.add_argument("--output-dir", default="./output", help="Directory for output artifacts")
    parser.add_argument("--config", default="", help="Path to .rl-config.yaml override")
    parser.add_argument(
        "--config-override",
        default="",
        help="Inline YAML config overrides (e.g. '{llm: {provider: anthropic}}')",
    )
    return parser.parse_args(argv)


def parse_config_override(override_str: str) -> dict[str, Any]:
    """Parse an inline YAML string into a config override dict.

    Returns an empty dict on invalid input rather than crashing — fail-safe
    so the engine can still run with defaults.
    """
    if not override_str or not override_str.strip():
        return {}
    try:
        parsed = yaml.safe_load(override_str)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except yaml.YAMLError:
        print(f"Warning: invalid YAML in --config-override, ignoring: {override_str!r}")
        return {}


def build_overrides(args: argparse.Namespace) -> dict[str, Any] | None:
    """Merge CLI flags and inline YAML into a single overrides dict."""
    overrides: dict[str, Any] = {}

    if args.config_override:
        overrides.update(parse_config_override(args.config_override))

    if args.provider:
        overrides.setdefault("llm", {})["provider"] = args.provider

    return overrides or None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    overrides = build_overrides(args)
    config = load_config(
        config_path=args.config or None,
        overrides=overrides,
    )

    secrets = SecretManager.from_environment()
    missing = secrets.validate_for_provider(config.llm.provider)
    if missing:
        print(f"Available secrets: {secrets.available() or ['none']}")
        secrets.require_for_provider(config.llm.provider)

    provider = create_provider(config.llm.provider, config.llm.model)

    monitor = WorkflowMonitor.from_environment()

    loop = RalphLoop(
        config=config,
        llm=provider,
        issue_url=args.issue_url,
        repo_path=args.target_repo,
        output_dir=args.output_dir,
        comparison_ref=args.comparison_ref,
        workflow_monitor=monitor,
        redactor=secrets.redactor,
    )
    loop.register_phase("triage", TriagePhase)
    loop.register_phase("implement", ImplementPhase)
    loop.register_phase("review", ReviewPhase)
    loop.register_phase("validate", ValidatePhase)

    execution = asyncio.run(loop.run())

    status = execution.result.get("status", "unknown")
    iterations = execution.result.get("total_iterations", 0)
    print(f"\nRalph Loop complete: status={status}, iterations={iterations}")
    print(f"Output: {args.output_dir}/execution.json")

    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
