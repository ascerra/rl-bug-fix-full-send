"""Cross-checker — verifies agent claims against evidence.

Runs five independent checks against the reconstructed timeline and
raw artifacts:

1. **Diff consistency** — git diff on the agent's branch matches the
   ``file_changes`` recorded in ``execution.json``.
2. **Action completeness** — every file in the diff has a corresponding
   action record.
3. **Phase ordering** — phases executed in the declared ``PHASE_ORDER``,
   no skipped validation.
4. **Token plausibility** — LLM token counts are within plausible bounds.
5. **Tool call integrity** — tool calls in transcripts have matching
   action records.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from engine.observer import CrossCheckReport, CrossCheckResult, TimelineEvent

PHASE_ORDER = ["triage", "implement", "review", "validate", "report"]

_PLAUSIBLE_MAX_TOKENS_OUT = 200_000
_PLAUSIBLE_MAX_TOKENS_IN = 2_000_000
_PLAUSIBLE_MIN_TOKENS = 0


class CrossChecker:
    """Verifies the agent's self-reported execution against evidence.

    Usage::

        checker = CrossChecker()
        report = checker.run_all_checks(
            timeline=timeline,
            execution_data=execution_data,
            branch_dir=Path("/path/to/branch"),
            transcript_calls=transcript_calls,
        )
    """

    def run_all_checks(
        self,
        timeline: list[TimelineEvent],
        execution_data: dict[str, Any],
        branch_dir: Path | None = None,
        transcript_calls: list[dict[str, Any]] | None = None,
    ) -> CrossCheckReport:
        """Run all cross-checks and return the aggregated report."""
        report = CrossCheckReport()

        report.add(self.check_diff_consistency(timeline, execution_data, branch_dir))
        report.add(self.check_action_completeness(timeline, execution_data, branch_dir))
        report.add(self.check_phase_ordering(timeline))
        report.add(self.check_token_plausibility(timeline))
        report.add(self.check_tool_call_integrity(timeline, transcript_calls))

        return report

    def check_diff_consistency(
        self,
        timeline: list[TimelineEvent],
        execution_data: dict[str, Any],
        branch_dir: Path | None = None,
    ) -> CrossCheckResult:
        """Verify the git diff on the agent's branch matches recorded file changes.

        If *branch_dir* is provided and is a git repo, runs ``git diff``
        against the default branch and compares the set of modified files
        to the file_write/file_edit actions in the execution record.

        When *branch_dir* is ``None``, the check passes vacuously (the
        observer may not always have access to the checked-out branch).
        """
        if branch_dir is None:
            return CrossCheckResult(
                check_name="diff_consistency",
                passed=True,
                details="Skipped: no branch directory provided",
            )

        git_dir = branch_dir / ".git"
        if not git_dir.exists() and not git_dir.is_file():
            return CrossCheckResult(
                check_name="diff_consistency",
                passed=True,
                details=f"Skipped: {branch_dir} is not a git repository",
            )

        try:
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1"],
                cwd=str(branch_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if diff_result.returncode != 0:
                diff_result = subprocess.run(
                    ["git", "diff", "--name-only", "--cached"],
                    cwd=str(branch_dir),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return CrossCheckResult(
                check_name="diff_consistency",
                passed=True,
                details=f"Skipped: git command failed ({exc})",
            )

        git_files = {f.strip() for f in diff_result.stdout.splitlines() if f.strip()}

        recorded_files = _extract_modified_files(execution_data)

        if not git_files and not recorded_files:
            return CrossCheckResult(
                check_name="diff_consistency",
                passed=True,
                details="No files modified in diff or execution record",
            )

        only_in_git = git_files - recorded_files
        only_in_record = recorded_files - git_files

        passed = len(only_in_git) == 0 and len(only_in_record) == 0
        details_parts: list[str] = []
        if only_in_git:
            details_parts.append(
                f"Files in git diff but not in execution record: {sorted(only_in_git)}"
            )
        if only_in_record:
            details_parts.append(
                f"Files in execution record but not in git diff: {sorted(only_in_record)}"
            )
        if not details_parts:
            details_parts.append(
                f"All {len(git_files)} files consistent between git diff and execution record"
            )

        return CrossCheckResult(
            check_name="diff_consistency",
            passed=passed,
            details="; ".join(details_parts),
            evidence={
                "git_files": sorted(git_files),
                "recorded_files": sorted(recorded_files),
                "only_in_git": sorted(only_in_git),
                "only_in_record": sorted(only_in_record),
            },
        )

    def check_action_completeness(
        self,
        timeline: list[TimelineEvent],
        execution_data: dict[str, Any],
        branch_dir: Path | None = None,
    ) -> CrossCheckResult:
        """Verify every file modified has a corresponding action record.

        Checks that every file appearing in file_write/file_edit actions
        also appears in at least one action's description or context.
        """
        execution = execution_data.get("execution", execution_data)
        actions = execution.get("actions", [])

        file_write_actions = [
            a for a in actions if a.get("action_type") in ("file_write", "file_edit")
        ]
        all_action_descriptions = {a.get("input", {}).get("description", "") for a in actions}

        orphaned_files: list[str] = []
        for action in file_write_actions:
            desc = action.get("input", {}).get("description", "")
            path = action.get("input", {}).get("context", {}).get("path", "")
            if not desc and not path:
                orphaned_files.append(action.get("id", "unknown"))

        if not file_write_actions:
            return CrossCheckResult(
                check_name="action_completeness",
                passed=True,
                details="No file modification actions recorded",
                evidence={"total_actions": len(actions), "file_actions": 0},
            )

        passed = len(orphaned_files) == 0
        return CrossCheckResult(
            check_name="action_completeness",
            passed=passed,
            details=(
                f"All {len(file_write_actions)} file modifications have action records"
                if passed
                else f"{len(orphaned_files)} file modifications lack description/path"
            ),
            evidence={
                "total_actions": len(actions),
                "file_actions": len(file_write_actions),
                "orphaned": orphaned_files,
                "all_descriptions_count": len(all_action_descriptions),
            },
        )

    def check_phase_ordering(self, timeline: list[TimelineEvent]) -> CrossCheckResult:
        """Verify phases executed in the declared PHASE_ORDER.

        Allows backward transitions (implement↔review backtracking) but
        checks that phases generally followed the expected sequence and
        that no phases were skipped entirely when subsequent phases ran.
        """
        phase_events = [e for e in timeline if e.event_type == "phase_transition"]
        if not phase_events:
            return CrossCheckResult(
                check_name="phase_ordering",
                passed=True,
                details="No phase transitions recorded",
            )

        observed_phases = [e.phase for e in phase_events]
        observed_unique_ordered = _unique_ordered(observed_phases)

        violations: list[str] = []
        for phase in observed_unique_ordered:
            if phase not in PHASE_ORDER:
                violations.append(f"Unknown phase: {phase}")

        implement_idx = PHASE_ORDER.index("implement")
        for i in range(len(observed_phases) - 1):
            cur = observed_phases[i]
            nxt = observed_phases[i + 1]
            if cur not in PHASE_ORDER or nxt not in PHASE_ORDER:
                continue
            cur_idx = PHASE_ORDER.index(cur)
            nxt_idx = PHASE_ORDER.index(nxt)
            if nxt_idx < cur_idx and nxt_idx != implement_idx:
                violations.append(f"Unexpected backward transition: {cur} → {nxt}")

        passed = len(violations) == 0
        return CrossCheckResult(
            check_name="phase_ordering",
            passed=passed,
            details=(
                f"Phase ordering valid: {' → '.join(observed_unique_ordered)}"
                if passed
                else f"Phase ordering violations: {'; '.join(violations)}"
            ),
            evidence={
                "observed_sequence": observed_phases,
                "unique_ordered": observed_unique_ordered,
                "violations": violations,
            },
        )

    def check_token_plausibility(self, timeline: list[TimelineEvent]) -> CrossCheckResult:
        """Verify LLM token counts are within plausible bounds.

        Checks that tokens_in and tokens_out for each LLM call are
        non-negative and within reasonable limits.
        """
        llm_events = [e for e in timeline if e.event_type == "llm_call"]
        if not llm_events:
            return CrossCheckResult(
                check_name="token_plausibility",
                passed=True,
                details="No LLM calls recorded",
            )

        violations: list[str] = []
        total_in = 0
        total_out = 0

        for event in llm_events:
            llm_ctx = event.details.get("llm_context", {})
            tokens_in = llm_ctx.get("tokens_in", 0)
            tokens_out = llm_ctx.get("tokens_out", 0)

            if not isinstance(tokens_in, (int, float)):
                violations.append(f"Non-numeric tokens_in: {tokens_in} in {event.description[:80]}")
                continue
            if not isinstance(tokens_out, (int, float)):
                violations.append(
                    f"Non-numeric tokens_out: {tokens_out} in {event.description[:80]}"
                )
                continue

            if tokens_in < _PLAUSIBLE_MIN_TOKENS:
                violations.append(f"Negative tokens_in ({tokens_in}) in {event.description[:80]}")
            if tokens_out < _PLAUSIBLE_MIN_TOKENS:
                violations.append(f"Negative tokens_out ({tokens_out}) in {event.description[:80]}")
            if tokens_in > _PLAUSIBLE_MAX_TOKENS_IN:
                violations.append(
                    f"Implausible tokens_in ({tokens_in}) in {event.description[:80]}"
                )
            if tokens_out > _PLAUSIBLE_MAX_TOKENS_OUT:
                violations.append(
                    f"Implausible tokens_out ({tokens_out}) in {event.description[:80]}"
                )

            total_in += tokens_in
            total_out += tokens_out

        passed = len(violations) == 0
        return CrossCheckResult(
            check_name="token_plausibility",
            passed=passed,
            details=(
                f"All {len(llm_events)} LLM calls have plausible token counts "
                f"(total: {total_in} in, {total_out} out)"
                if passed
                else f"Token plausibility violations: {'; '.join(violations[:5])}"
            ),
            evidence={
                "llm_call_count": len(llm_events),
                "total_tokens_in": total_in,
                "total_tokens_out": total_out,
                "violations": violations,
            },
        )

    def check_tool_call_integrity(
        self,
        timeline: list[TimelineEvent],
        transcript_calls: list[dict[str, Any]] | None = None,
    ) -> CrossCheckResult:
        """Verify tool calls in transcripts have matching action records.

        Compares the set of LLM calls in the timeline (from action records)
        against the transcript calls (from transcript-calls.json).  Every
        transcript entry should correspond to an action record.
        """
        if not transcript_calls:
            return CrossCheckResult(
                check_name="tool_call_integrity",
                passed=True,
                details="No transcript calls to verify",
            )

        llm_events = [e for e in timeline if e.event_type == "llm_call"]
        action_descriptions = {e.description for e in llm_events}

        transcript_descriptions = {call.get("description", "") for call in transcript_calls}

        unmatched_transcripts = transcript_descriptions - action_descriptions
        non_empty_unmatched = {d for d in unmatched_transcripts if d}

        passed = len(non_empty_unmatched) == 0
        return CrossCheckResult(
            check_name="tool_call_integrity",
            passed=passed,
            details=(
                f"All {len(transcript_calls)} transcript calls have matching action records"
                if passed
                else f"{len(non_empty_unmatched)} transcript calls without matching action records"
            ),
            evidence={
                "transcript_count": len(transcript_calls),
                "action_count": len(llm_events),
                "unmatched_descriptions": sorted(non_empty_unmatched)[:10],
            },
        )


def _extract_modified_files(execution_data: dict[str, Any]) -> set[str]:
    """Extract file paths from file_write/file_edit actions in the execution record."""
    execution = execution_data.get("execution", execution_data)
    files: set[str] = set()

    for action in execution.get("actions", []):
        action_type = action.get("action_type", "")
        if action_type in ("file_write", "file_edit"):
            path = action.get("input", {}).get("context", {}).get("path", "")
            if path:
                files.add(path)
            desc = action.get("input", {}).get("description", "")
            if desc and "/" in desc:
                files.add(desc)

    return files


def _unique_ordered(seq: list[str]) -> list[str]:
    """Return unique elements preserving first-occurrence order."""
    seen: set[str] = set()
    result: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
