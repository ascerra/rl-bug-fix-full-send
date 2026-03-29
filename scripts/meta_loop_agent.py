#!/usr/bin/env python3
"""Meta Ralph Loop Agent — implements planned work and diagnoses production failures.

Two modes of operation:

1. **Task mode** (--task): Reads a task description + IMPLEMENTATION-PLAN.md + relevant
   codebase files, generates and applies code changes to implement the task. Use this to
   drive implementation of planned work before running production jobs.

2. **Diagnose mode** (--execution-json): Reads execution.json from a failed production
   run, diagnoses the root cause, and generates targeted fixes. Use this to iterate on
   failures after production runs.

This is the "brain" of the meta Ralph Loop: the component that closes the gap between
"here's what needs to happen" and "here are the code changes."

Usage:
    # Implement a task from the plan
    python scripts/meta_loop_agent.py --task "Implement review phase leniency" [OPTIONS]

    # Diagnose and fix a failed production run
    python scripts/meta_loop_agent.py --execution-json <PATH> [OPTIONS]

Options:
    --task DESCRIPTION      Task to implement (reads IMPLEMENTATION-PLAN.md for context)
    --execution-json PATH   Path to execution.json from a failed run
    --files FILE [FILE ...] Additional files to include as context (beyond the defaults)
    --project-dir DIR       Project root (default: auto-detect from script location)
    --provider NAME         LLM provider: gemini or anthropic (default: gemini)
    --dry-run               Show what would change without modifying files
    --verbose               Print full LLM diagnosis
    --max-context CHARS     Max chars of context to include (default: 60000)

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
    "engine/phases/report.py",
    "engine/phases/prompt_loader.py",
    "engine/__init__.py",
    "engine/__main__.py",
    "engine/integrations/llm.py",
    "engine/integrations/github.py",
    "engine/tools/executor.py",
    "engine/tools/test_runner.py",
    "engine/observability/logger.py",
    "engine/observability/tracer.py",
    "engine/visualization/publisher.py",
    "templates/prompts/review.md",
    "templates/prompts/implement.md",
    "templates/prompts/triage.md",
    "templates/prompts/validate.md",
    "templates/prompts/report.md",
]

PLAN_FILES = [
    "IMPLEMENTATION-PLAN.md",
    "SPEC.md",
    "ARCHITECTURE.md",
    "README.md",
]

DIAGNOSE_PROMPT = """\
You are an expert debugger for the RL Bug Fix engine — a phased OODA pipeline
that automatically fixes bugs in code repositories by running through
Triage → Implement → Review → Validate → Report phases.

You are part of the META RALPH LOOP that develops and maintains this engine.

Given:
1. The execution trace from a FAILED production run (execution.json)
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

TASK_PROMPT = """\
You are a senior engineer working on the RL Bug Fix engine — a phased OODA pipeline
that automatically fixes bugs in code repositories.

You are part of the META RALPH LOOP that develops and maintains this engine.

Given:
1. A TASK DESCRIPTION telling you what to implement
2. The IMPLEMENTATION PLAN with the project's status and roadmap
3. The engine source code and documentation

Your job:
1. Understand the task and its context within the implementation plan.
2. Read the relevant source files carefully.
3. Generate SPECIFIC code edits that implement the task correctly.
4. Update documentation (README.md, IMPLEMENTATION-PLAN.md) to reflect the changes.

## Output Format

Return ONLY a JSON object (no markdown fences, no commentary) with this shape:

{
  "diagnosis": "What the task requires and your approach to implementing it",
  "root_cause": "Single phrase summarizing the task: e.g. 'add retry backoff to implement phase'",
  "changes": [
    {
      "file": "relative/path/to/file.py",
      "search": "exact text to find in the file (multi-line is fine)",
      "replace": "exact replacement text",
      "reason": "why this change is needed for the task"
    }
  ],
  "summary": "One-line summary of all changes made",
  "confidence": "high|medium|low"
}

## Rules

- "search" must EXACTLY match existing text in the file (whitespace, indentation,
  newlines must all match). Read the file contents provided carefully.
- You can create NEW files by setting "search" to "" (empty string) and "file" to
  the new file path. The "replace" field becomes the full file content.
- Make focused, correct changes. Include all necessary imports and updates.
- If the task requires changes across multiple files, list them all.
- Update tests if you're changing behavior that existing tests cover.
- Update IMPLEMENTATION-PLAN.md to mark completed items with ✅.
- Set confidence to "low" if the task is ambiguous and explain in diagnosis.
- Do NOT add unnecessary comments. Code should be self-documenting.
"""


def _read_files_as_context(
    project_dir: Path,
    file_list: list[str],
    extra_files: list[str] | None = None,
) -> str:
    """Read a list of project-relative file paths and format as LLM context."""
    parts: list[str] = []
    seen: set[str] = set()
    for rel_path in [*file_list, *(extra_files or [])]:
        if rel_path in seen:
            continue
        seen.add(rel_path)
        full_path = project_dir / rel_path
        if not full_path.exists():
            continue
        content = full_path.read_text()
        parts.append(f"\n### {rel_path}\n")
        parts.append(f"```\n{content}\n```\n")
    return "\n".join(parts)


def build_diagnose_message(
    execution_json_path: str,
    project_dir: Path,
    extra_files: list[str] | None = None,
    max_context: int = 60000,
) -> str:
    """Build the user message for diagnose mode: execution trace + engine source."""
    exec_text = Path(execution_json_path).read_text()

    parts: list[str] = []
    parts.append("## Execution Trace (execution.json)\n")
    parts.append("```json\n")
    parts.append(exec_text[:max_context])
    if len(exec_text) > max_context:
        parts.append("\n... (truncated) ...")
    parts.append("\n```\n")

    parts.append("\n## Engine Source Code\n")
    parts.append(_read_files_as_context(project_dir, ENGINE_FILES, extra_files))

    return "\n".join(parts)


def build_task_message(
    task: str,
    project_dir: Path,
    extra_files: list[str] | None = None,
    max_context: int = 60000,
) -> str:
    """Build the user message for task mode: task description + plan + engine source."""
    parts: list[str] = []

    parts.append("## Task\n")
    parts.append(task)
    parts.append("\n")

    parts.append("\n## Project Documentation\n")
    parts.append(_read_files_as_context(project_dir, PLAN_FILES))

    parts.append("\n## Engine Source Code\n")
    parts.append(_read_files_as_context(project_dir, ENGINE_FILES, extra_files))

    full = "\n".join(parts)
    if len(full) > max_context:
        return full[:max_context] + "\n\n... (truncated) ..."
    return full


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
    *,
    mode: str,
    project_dir: Path,
    provider_name: str = "gemini",
    max_context: int = 60000,
    execution_json: str = "",
    task: str = "",
    extra_files: list[str] | None = None,
) -> dict:
    """Call the LLM to diagnose a failure or implement a task."""
    provider = create_provider(provider_name)

    if mode == "task":
        system_prompt = TASK_PROMPT
        user_msg = build_task_message(task, project_dir, extra_files, max_context)
    else:
        system_prompt = DIAGNOSE_PROMPT
        user_msg = build_diagnose_message(
            execution_json, project_dir, extra_files, max_context
        )

    print(f"  Mode:         {mode}")
    print(f"  Context size: {len(user_msg):,} chars")
    print(f"  LLM provider: {provider_name}")

    response = await provider.complete(
        system_prompt=system_prompt,
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
    """Apply search/replace edits to files. Returns list of modified file paths.

    If "search" is empty, creates a new file with "replace" as its content.
    """
    modified: list[str] = []

    for i, change in enumerate(changes, 1):
        file_path = project_dir / change["file"]
        reason = change.get("reason", "N/A")
        search = change.get("search", "")
        replace = change.get("replace", "")

        if not search:
            if dry_run:
                print(f"  [dry-run {i}] Would create: {change['file']}")
                print(f"               Reason: {reason}")
            else:
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text(replace)
                print(f"  [{i}] Created: {change['file']}")
                print(f"       Reason: {reason}")
                modified.append(change["file"])
            continue

        if not file_path.exists():
            print(f"  WARNING [{i}] File not found: {change['file']}")
            continue

        content = file_path.read_text()

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
        description="Meta Ralph Loop Agent: implement tasks and diagnose failures"
    )

    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--task",
        help="Task description to implement (reads IMPLEMENTATION-PLAN.md for context)",
    )
    mode_group.add_argument(
        "--execution-json",
        help="Path to execution.json from a failed run (diagnose mode)",
    )

    parser.add_argument(
        "--files",
        nargs="+",
        default=[],
        help="Additional files to include as LLM context",
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
        help="Max chars of context to send to LLM (default: 60000)",
    )

    args = parser.parse_args()
    project = Path(args.project_dir)

    if args.task:
        mode = "task"
    else:
        mode = "diagnose"
        exec_path = Path(args.execution_json)
        if not exec_path.exists():
            print(f"ERROR: execution.json not found: {exec_path}", file=sys.stderr)
            return 2

    print()
    print("Meta Ralph Loop Agent")
    print("=" * 50)
    if mode == "task":
        print(f"  Mode:           task")
        print(f"  Task:           {args.task[:120]}")
    else:
        print(f"  Mode:           diagnose")
        print(f"  Execution JSON: {args.execution_json}")
    if args.files:
        print(f"  Extra files:    {', '.join(args.files)}")
    print(f"  Project dir:    {project}")
    print(f"  Provider:       {args.provider}")
    print(f"  Dry run:        {args.dry_run}")
    print()
    print("  Calling LLM...")

    try:
        result = asyncio.run(
            run_agent(
                mode=mode,
                project_dir=project,
                provider_name=args.provider,
                max_context=args.max_context,
                execution_json=args.execution_json or "",
                task=args.task or "",
                extra_files=args.files or None,
            )
        )
    except Exception as exc:
        print(f"ERROR: LLM call failed: {exc}", file=sys.stderr)
        return 2

    print_summary(result, verbose=args.verbose)

    changes = result.get("changes", [])
    if not changes:
        if mode == "diagnose":
            print("  No code changes suggested.")
            print("  The failure may be external (LLM outage, API error, etc.)")
        else:
            print("  No code changes suggested for this task.")
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

        output_dir = Path(args.execution_json).parent if args.execution_json else project
        summary_file = output_dir / "agent-changes.json"
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
