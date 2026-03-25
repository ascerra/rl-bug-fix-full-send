"""CLI entry point for the Ralph Loop engine."""

from __future__ import annotations

import argparse
import sys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rl-engine",
        description="Ralph Loop Bug Fix Engine — agentic SDLC for GitHub organizations",
    )
    parser.add_argument("--issue-url", required=True, help="GitHub issue URL to process")
    parser.add_argument("--target-repo", required=True, help="Path to cloned target repository")
    parser.add_argument("--comparison-ref", default="", help="Git ref of human fix for comparison")
    parser.add_argument("--provider", default="gemini", help="LLM provider (gemini|anthropic)")
    parser.add_argument("--output-dir", default="./output", help="Directory for output artifacts")
    parser.add_argument("--config", default="", help="Path to .rl-config.yaml override")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    # TODO: Phase 1 — Initialize config, create RalphLoop, run it
    print("RL Engine v0.1.0")
    print(f"Issue: {args.issue_url}")
    print(f"Target repo: {args.target_repo}")
    print(f"Provider: {args.provider}")
    print(f"Output: {args.output_dir}")
    print("Engine not yet implemented — run the meta ralph loop with prompt.md to build it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
