#!/usr/bin/env python3
"""Meta Loop Agent — analyzes failed Ralph Loop runs and generates engine fixes.

Reads execution.json from a failed run, feeds it along with engine source code
to an LLM, and applies the suggested code changes to the local codebase.

This is the "brain" of the meta loop: the component that closes the gap between
"the run failed" and "here are the engine code changes to try next."

Usage:
    python scripts/meta_loop_agent.py --execution-json <PATH> [OPTIONS]

Options:
    --execution-json PATH   Path to execution.json from a failed run (required)
    --project-dir DIR       Project root (default: auto-detect from script location)
    --provider NAME         LLM provider: gemini or anthropic (default: gemini)
    --dry-run               Show what would change without modifying files
    --verbose               Print full LLM diagnosis
    --max-context CHARS     Max chars of execution.json to include (default: 60000)

Exit codes:
    0 = changes were applied successfully
    1 = no changes needed or agent could not determine a fix
    2 = error (missing file, LLM failure, etc.)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from engine.integrations.llm import create_provider  # noqa: E402

ENGINE_FILES = [
    "engine/loop.py",
    "engine/config.py",
    "engine/phases/review.py",
    "engine/phases/implement.py",
    "engine/phases/triage.py",
    "engine/phases/validate.py",
    "engine/phases/base.py",
    "templates/prompts/review.md",
    "templates/prompts/implement.md",
    "templates/prompts/triage.md",
    "templates/prompts/validate.md",
]

SYSTEM_PROMPT = """\
You are an expert debugger for the Ralph Loop — an AI agent that automatically
fixes bugs in code repositories by iterating through Triage → Implement →
Review → Validate → Report phases.

You are part of a META LOOP that improves the Ralph Loop engine itself.

Given:
1. The execution trace from a FAILED Ralph Loop run (execution.json)
2. The engine source code

Your job:
1. Diagnose WHY the run failed — look at iteration traces, phase results,
   escalation reasons, review findings, and error messages.
2. Identify the ROOT CAUSE in the engine code.
3. Generate SPECIFIC, minimal code edits to fix the root cause.

## Common Failure Patterns

- **Review escalation**: Review phase rejected the fix too many times.
  → Relax review criteria, improve progressive leniency, or improve implement
    phase prompts so it produces better code.
- **Implementation loops**: Implement phase generates the same wrong code.
  → Improve triage context gathering, add better examples to prompts,
    or adjust how review feedback is relayed back to implement.
- **Parse errors**: LLM response doesn't match expected format.
  → Add stricter format instructions, improve JSON extraction, add retries.
- **Timeout**: Ran out of time budget.
  → Reduce unnecessary iterations, increase budget, or optimize phase logic.
- **Triage failure**: Bug not properly understood.
  → Improve triage prompts, add better file discovery heuristics.

## Output Format

Return ONLY a JSON object (no markdown fences, no commentary) with this shape:

{
  "diagnosis": "Clear multi-sentence description of what went wrong and why",
  "root_cause": "Single phrase: e.g. 'review prompt too strict on style'",
  "changes": [
    {
      "file": "relative/path/to/file.py",
      "search": "exact text to find in the file (multi-line is fine)",
      "replace": "exact replacement text",
      "reason": "why this change fixes the root cause"
    }
  ],
  "summary": "One-line summary of all changes made",
  "confidence": "high|medium|low"
}

## Rules

- "search" must EXACTLY match existing text in the file (whitespace, indentation,
  newlines must all match).
- Make MINIMAL, targeted changes. Don't rewrite whole files.
- Focus on the ROOT CAUSE. Don't just bump retry counters.
- If the run failed due to an external issue (LLM outage, GitHub API error),
  say so in the diagnosis and return an empty changes array.
- If you need multiple edits across files, list them all.
- Set confidence to "low" if you're uncertain and explain in diagnosis.
"""


def build_user_message(
    execution_json_path: str,
    project_dir: Path,
    max_context: int = 60000,
) -> str:
    """Build the user message with execution data + engine source."""
    exec_text = Path(execution_json_path).read_text()

    parts: list[str] = []
    parts.append("## Execution Trace (execution.json)\n")
    parts.append("```json\n")
    parts.append(exec_text[:max_context])
    if len(exec_text) > max_context:
        parts.append("\n... (truncated) ...")
    parts.append("\n```\n")

    parts.append("\n## Engine Source Code\n")
    for rel_path in ENGINE_FILES:
        full_path = project_dir / rel_path
        if not full_path.exists():
            continue
        content = full_path.read_text()
        parts.append(f"\n### {rel_path}\n")
        parts.append(f"```\n{content}\n```\n")

    return "\n".join(parts)


def extract_json(text: str) -> dict:
    """Extract a JSON object from LLM output, stripping markdown fences."""
    cleaned = text.strip()

    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end != -1:
        cleaned = cleaned[brace_start : brace_end + 1]

    return json.loads(cleaned)


async def run_agent(
    execution_json: str,
    project_dir: Path,
    provider_name: str = "gemini",
    max_context: int = 60000,
) -> dict:
    """Call the LLM to diagnose and suggest fixes for a failed run."""
    provider = create_provider(provider_name)

    user_msg = build_user_message(execution_json, project_dir, max_context)
    print(f"  Context size: {len(user_msg):,} chars")
    print(f"  LLM provider: {provider_name}")

    response = await provider.complete(
        system_prompt=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        temperature=0.3,
        max_tokens=16384,
        json_mode=True,
    )

    print(
        f"  LLM response: {response.tokens_in:,} tokens in, "
        f"{response.tokens_out:,} tokens out, "
        f"{response.latency_ms:.0f}ms"
    )

    return extract_json(response.content)


def apply_changes(
    changes: list[dict],
    project_dir: Path,
    dry_run: bool = False,
) -> list[str]:
    """Apply search/replace edits to files. Returns list of modified file paths."""
    modified: list[str] = []

    for i, change in enumerate(changes, 1):
        file_path = project_dir / change["file"]
        reason = change.get("reason", "N/A")

        if not file_path.exists():
            print(f"  WARNING [{i}] File not found: {change['file']}")
            continue

        content = file_path.read_text()
        search = change["search"]
        replace = change["replace"]

        if search not in content:
            print(f"  WARNING [{i}] Search text not found in {change['file']}")
            preview = search[:120].replace("\n", "\\n")
            print(f"          Search: {preview}...")
            continue

        if dry_run:
            print(f"  [dry-run {i}] Would modify: {change['file']}")
            print(f"               Reason: {reason}")
        else:
            new_content = content.replace(search, replace, 1)
            file_path.write_text(new_content)
            print(f"  [{i}] Modified: {change['file']}")
            print(f"       Reason: {reason}")
            modified.append(change["file"])

    return modified


def print_summary(result: dict, verbose: bool = False) -> None:
    """Print the agent's diagnosis and summary."""
    print()
    print("  Diagnosis")
    print("  ---------")
    diagnosis = result.get("diagnosis", "No diagnosis provided")
    if verbose:
        for line in diagnosis.split("\n"):
            print(f"  {line}")
    else:
        print(f"  {diagnosis[:300]}")
        if len(diagnosis) > 300:
            print("  ... (use --verbose for full diagnosis)")

    print()
    print(f"  Root cause:  {result.get('root_cause', 'unknown')}")
    print(f"  Confidence:  {result.get('confidence', 'unknown')}")
    print(f"  Summary:     {result.get('summary', 'N/A')}")
    print(f"  Changes:     {len(result.get('changes', []))} edit(s)")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Meta Loop Agent: diagnose failed runs and generate engine fixes"
    )
    parser.add_argument(
        "--execution-json",
        required=True,
        help="Path to execution.json from a failed run",
    )
    parser.add_argument(
        "--project-dir",
        default=str(PROJECT_DIR),
        help="Project root directory (default: auto-detect)",
    )
    parser.add_argument(
        "--provider",
        default="gemini",
        choices=["gemini", "anthropic"],
        help="LLM provider (default: gemini)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show proposed changes without applying them",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full LLM diagnosis",
    )
    parser.add_argument(
        "--max-context",
        type=int,
        default=60000,
        help="Max chars of execution.json to send to LLM (default: 60000)",
    )

    args = parser.parse_args()
    project = Path(args.project_dir)

    exec_path = Path(args.execution_json)
    if not exec_path.exists():
        print(f"ERROR: execution.json not found: {exec_path}", file=sys.stderr)
        return 2

    print()
    print("Meta Loop Agent")
    print("=" * 50)
    print(f"  Execution JSON: {exec_path}")
    print(f"  Project dir:    {project}")
    print(f"  Provider:       {args.provider}")
    print(f"  Dry run:        {args.dry_run}")
    print()
    print("  Calling LLM...")

    try:
        result = asyncio.run(
            run_agent(
                str(exec_path),
                project,
                args.provider,
                args.max_context,
            )
        )
    except Exception as exc:
        print(f"ERROR: LLM call failed: {exc}", file=sys.stderr)
        return 2

    print_summary(result, verbose=args.verbose)

    changes = result.get("changes", [])
    if not changes:
        print("  No code changes suggested.")
        print("  The failure may be external (LLM outage, API error, etc.)")
        return 1

    print("  Applying changes..." if not args.dry_run else "  Dry run — proposed changes:")
    print()

    modified = apply_changes(changes, project, dry_run=args.dry_run)

    if args.dry_run:
        print()
        print(f"  Dry run complete. {len(changes)} change(s) would be applied.")
        return 0

    if modified:
        print()
        print(f"  {len(modified)} file(s) modified:")
        for f in modified:
            print(f"    - {f}")
        print()

        summary_file = exec_path.parent / "agent-changes.json"
        summary_file.write_text(json.dumps(result, indent=2))
        print(f"  Agent output saved to: {summary_file}")
        return 0
    else:
        print()
        print("  WARNING: No changes could be applied (search text mismatches).")
        print("  The LLM may have hallucinated file contents.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
