"""CLI argument parsing for the neutral observer.

Entry point: ``python -m engine.observer``

Arguments mirror the observer workflow job needs:

- ``--artifacts-dir`` — path to downloaded agent artifacts
- ``--output-dir`` — path to write attestation and policy result
- ``--config`` — path to ``.rl-config.yaml``
- ``--branch-dir`` — path to the agent's working branch checkout
- ``--templates-dir`` — path to prompt templates for digest computation
- ``--skip-signing`` — skip attestation signing (for local testing)
"""

from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse observer CLI arguments."""
    parser = argparse.ArgumentParser(
        prog="rl-observer",
        description="Neutral observer — independent verification of agent execution",
    )
    parser.add_argument(
        "--artifacts-dir",
        required=True,
        help="Path to downloaded agent artifacts (execution.json, log.json, transcripts/)",
    )
    parser.add_argument(
        "--output-dir",
        default="./attestation",
        help="Path to write attestation and policy result (default: ./attestation)",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Path to .rl-config.yaml (reads the 'observer' section)",
    )
    parser.add_argument(
        "--branch-dir",
        default="",
        help="Path to the agent's working branch checkout (for diff consistency check)",
    )
    parser.add_argument(
        "--templates-dir",
        default="",
        help="Path to prompt templates directory (for digest computation)",
    )
    parser.add_argument(
        "--skip-signing",
        action="store_true",
        default=False,
        help="Skip attestation signing (for local testing without cosign)",
    )
    return parser.parse_args(argv)
