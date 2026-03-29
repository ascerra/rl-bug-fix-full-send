"""CI Remediation Phase — fix CI failures after PR creation.

Implements SPEC §5.7 (Implement-First Workflow Execution):
After the validate phase pushes a branch and creates a PR, the target
repo's CI pipeline may report failures.  This phase:
1. Reads the CI failure details (category, errors, annotations, logs)
2. Sends failure context to the LLM for a targeted fix plan
3. Applies the fix to the working tree
4. Runs local validation (lint)
5. Commits and pushes the fix so CI reruns

The phase is invoked by the CI remediation sub-loop in ``engine/loop.py``,
NOT via the normal ``PHASE_ORDER`` pipeline.  It has its own iteration
cap and time budget, independent of the main loop.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from engine.phases.base import Phase, PhaseResult


class CIRemediatePhase(Phase):
    """Fix CI failures detected on the PR's branch.

    Uses the same tool set as the implement phase plus ``github_api``
    for pushing.  Receives CI failure details via ``issue_data`` keys
    injected by the loop's CI monitoring sub-loop.

    Expected ``issue_data`` keys (set by ``RalphLoop._run_ci_monitoring_loop``):
      - ``url``: original issue URL
      - ``ci_failure_details``: ``FailureDetails.to_dict()`` from ``CIMonitor``
      - ``ci_failure_category``: ``CIFailureCategory`` string value
      - ``branch_name``: the PR branch name
      - ``original_diff``: the original bug fix diff
      - ``original_description``: the original fix description
      - ``remediation_iteration``: current CI remediation attempt (1-based)
    """

    name = "ci_remediate"
    allowed_tools: ClassVar[list[str]] = []

    async def observe(self) -> dict[str, Any]:
        """Gather CI failure context and prior remediation attempts."""
        self.logger.info("CI-Remediate observe: gathering CI failure details")

        failure_details = self.issue_data.get("ci_failure_details", {})
        failure_category = self.issue_data.get("ci_failure_category", "unknown")
        branch_name = self.issue_data.get("branch_name", "")
        original_diff = self.issue_data.get("original_diff", "")
        original_desc = self.issue_data.get("original_description", "")
        remediation_iter = self.issue_data.get("remediation_iteration", 1)

        prior_attempts = self._extract_prior_attempts()

        file_contents: dict[str, str] = {}
        failing_files = _extract_failing_files(failure_details)
        if self.tool_executor and failing_files:
            for path in failing_files[:10]:
                result = await self.tool_executor.execute("file_read", path=path)
                if result.get("success"):
                    file_contents[path] = result.get("content", "")

        n_checks = len(failure_details.get("failing_checks", []))
        n_tests = len(failure_details.get("failing_tests", []))
        self.logger.narrate(
            f"CI failure: {failure_category} — "
            f"{n_checks} failing check(s), {n_tests} failing test(s). "
            f"Remediation attempt #{remediation_iter}."
        )

        return {
            "failure_details": failure_details,
            "failure_category": failure_category,
            "branch_name": branch_name,
            "original_diff": original_diff,
            "original_description": original_desc,
            "remediation_iteration": remediation_iter,
            "prior_attempts": prior_attempts,
            "file_contents": file_contents,
            "failing_files": failing_files,
        }

    async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Send CI failure context to LLM for a targeted fix plan."""
        self.logger.info("CI-Remediate plan: calling LLM with failure context")

        system_prompt = self.load_system_prompt()

        failure_details = observation["failure_details"]
        failure_category = observation["failure_category"]

        trusted_context = _build_trusted_context(observation)
        untrusted = self._wrap_untrusted_content(_build_untrusted_context(failure_details))

        user_message = f"{trusted_context}\n\n{untrusted}"

        llm_response = await self.llm.complete(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            temperature=self.config.llm.temperature,
            max_tokens=self.config.llm.max_tokens,
        )

        self.record_llm_call(
            description=f"CI remediation plan ({failure_category})",
            model=llm_response.model,
            provider=llm_response.provider,
            tokens_in=llm_response.tokens_in,
            tokens_out=llm_response.tokens_out,
            latency_ms=llm_response.latency_ms,
            prompt_summary=(f"CI remediate prompt + {failure_category} failure details"),
            response_summary=llm_response.content[:500],
            system_prompt=system_prompt,
            user_message=user_message,
            response=llm_response.content,
        )

        plan = _parse_remediation_response(llm_response.content)

        is_code_fix = plan.get("is_code_fix", True)
        strategy = plan.get("fix_strategy", "unknown")
        self.logger.narrate(
            f"LLM plan: {'code fix' if is_code_fix else 'no code change'} "
            f"— strategy: {strategy[:100]}"
        )

        return {
            "plan": plan,
            "observation": observation,
            "raw_llm_response": llm_response.content,
        }

    async def act(self, plan_result: dict[str, Any]) -> dict[str, Any]:
        """Apply the remediation fix, commit, and push."""
        self.logger.info("CI-Remediate act: applying fix")

        plan = plan_result.get("plan", {})
        observation = plan_result.get("observation", {})
        branch_name = observation.get("branch_name", "")
        actions: list[dict[str, Any]] = []

        if not plan.get("is_code_fix", True):
            self.logger.narrate("No code change needed (rerun or pre-existing).")
            return {
                "plan": plan,
                "files_changed": [],
                "committed": False,
                "pushed": False,
                "needs_rerun": plan.get("fix_strategy") == "rerun",
                "actions": actions,
            }

        file_changes = plan.get("file_changes", [])
        if not file_changes:
            self.logger.narrate("LLM returned no file changes.")
            return {
                "plan": plan,
                "files_changed": [],
                "committed": False,
                "pushed": False,
                "needs_rerun": False,
                "actions": actions,
            }

        files_written: list[str] = []
        for fc in file_changes:
            path = fc.get("path", "")
            content = fc.get("content", "")
            if not path or not content:
                continue
            if self.tool_executor:
                result = await self.tool_executor.execute("file_write", path=path, content=content)
                success = result.get("success", False)
                actions.append(
                    {
                        "action": "file_write",
                        "path": path,
                        "success": success,
                    }
                )
                if success:
                    files_written.append(path)

        committed = False
        pushed = False

        if files_written and self.tool_executor:
            commit_msg = f"fix(ci): {plan.get('analysis', 'CI remediation fix')[:100]}"

            add_result = await self.tool_executor.execute(
                "shell_run",
                command="git add -A",
                timeout=30,
            )
            actions.append(
                {
                    "action": "git_add",
                    "success": add_result.get("success", False),
                }
            )

            commit_result = await self.tool_executor.execute("git_commit", message=commit_msg)
            committed = commit_result.get("success", False)
            actions.append(
                {
                    "action": "git_commit",
                    "success": committed,
                    "message": commit_msg,
                }
            )

            if committed and branch_name:
                push_result = await self.tool_executor.execute(
                    "shell_run",
                    command=f"git push origin {branch_name}",
                    timeout=60,
                )
                pushed = push_result.get("success", False)
                actions.append(
                    {
                        "action": "git_push",
                        "success": pushed,
                        "branch": branch_name,
                        "output": (push_result.get("stdout", "") + push_result.get("stderr", ""))[
                            :500
                        ],
                    }
                )

        n_files = len(files_written)
        push_status = "pushed" if pushed else ("committed" if committed else "not committed")
        self.logger.narrate(f"Applied {n_files} file change(s), {push_status}.")

        return {
            "plan": plan,
            "files_changed": files_written,
            "committed": committed,
            "pushed": pushed,
            "needs_rerun": False,
            "actions": actions,
        }

    async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
        """Run local lint checks on the remediation fix."""
        self.logger.info("CI-Remediate validate: running local checks")

        if not action_result.get("files_changed"):
            return {
                "valid": True,
                "lint_passed": True,
                "lint_output": "No files changed — skipping lint",
                "action_result": action_result,
            }

        lint_result = await self._run_linters()

        lint_ok = lint_result.get("passed", False)
        self.logger.narrate(f"Local lint: {'PASS' if lint_ok else 'FAIL'}.")

        return {
            "valid": lint_ok or not action_result.get("files_changed"),
            "lint_passed": lint_ok,
            "lint_output": lint_result.get("output", ""),
            "action_result": action_result,
        }

    async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
        """Assess remediation outcome."""
        self.logger.info("CI-Remediate reflect: assessing outcome")

        action_result = validation.get("action_result", {})
        plan = action_result.get("plan", {})
        pushed = action_result.get("pushed", False)
        needs_rerun = action_result.get("needs_rerun", False)
        files_changed = action_result.get("files_changed", [])
        lint_passed = validation.get("lint_passed", True)

        if needs_rerun:
            self.logger.narrate("Recommending CI rerun (infrastructure flake).")
            return PhaseResult(
                phase=self.name,
                success=True,
                should_continue=True,
                findings={
                    "action": "rerun",
                    "analysis": plan.get("analysis", ""),
                },
                artifacts={
                    "needs_rerun": True,
                    "files_changed": [],
                    "pushed": False,
                },
            )

        if not files_changed:
            self.logger.narrate("No files changed — remediation produced no fix.")
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=True,
                findings={
                    "action": "no_fix",
                    "analysis": plan.get("analysis", "No file changes produced"),
                },
                artifacts={
                    "needs_rerun": False,
                    "files_changed": [],
                    "pushed": False,
                },
            )

        if not lint_passed:
            self.logger.narrate("Lint failed on remediation fix — will retry.")
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=True,
                findings={
                    "action": "lint_failed",
                    "lint_output": validation.get("lint_output", "")[:1000],
                    "analysis": plan.get("analysis", ""),
                },
                artifacts={
                    "needs_rerun": False,
                    "files_changed": files_changed,
                    "pushed": False,
                },
            )

        if pushed:
            self.logger.narrate("Fix pushed — CI will rerun on the branch.")
            return PhaseResult(
                phase=self.name,
                success=True,
                should_continue=True,
                findings={
                    "action": "pushed",
                    "analysis": plan.get("analysis", ""),
                    "fix_strategy": plan.get("fix_strategy", ""),
                    "expected_resolution": plan.get("expected_resolution", ""),
                },
                artifacts={
                    "needs_rerun": False,
                    "files_changed": files_changed,
                    "pushed": True,
                },
            )

        self.logger.narrate("Fix applied but push failed — will retry.")
        return PhaseResult(
            phase=self.name,
            success=False,
            should_continue=True,
            findings={
                "action": "push_failed",
                "analysis": plan.get("analysis", ""),
            },
            artifacts={
                "needs_rerun": False,
                "files_changed": files_changed,
                "pushed": False,
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_prior_attempts(self) -> list[dict[str, Any]]:
        """Extract prior CI remediation attempt details from prior_results."""
        attempts: list[dict[str, Any]] = []
        for result in self.prior_results:
            if result.phase != "ci_remediate":
                continue
            attempts.append(
                {
                    "success": result.success,
                    "analysis": result.findings.get("analysis", ""),
                    "fix_strategy": result.findings.get("fix_strategy", ""),
                    "action": result.findings.get("action", ""),
                    "files_changed": result.artifacts.get("files_changed", []),
                    "pushed": result.artifacts.get("pushed", False),
                    "lint_output": result.findings.get("lint_output", ""),
                    "expected_resolution": result.findings.get("expected_resolution", ""),
                }
            )
        return attempts

    async def _run_linters(self) -> dict[str, Any]:
        """Run linters on the remediation fix."""
        if not self.tool_executor:
            return {"passed": True, "output": "No tool executor"}

        from engine.tools.test_runner import detect_repo_stack

        repo_listing = ""
        tree_result = await self.tool_executor.execute(
            "shell_run",
            command=(
                "find . -type f "
                "\\( -name '*.py' -o -name '*.go' -o -name '*.js' -o -name '*.ts' "
                "-o -name '*.yaml' -o -name '*.yml' -o -name '*.rs' "
                "-o -name 'go.mod' -o -name 'Cargo.toml' -o -name 'package.json' "
                "-o -name 'pyproject.toml' -o -name 'Makefile' \\) "
                "| grep -v node_modules | grep -v __pycache__ | sort | head -200"
            ),
        )
        if tree_result.get("success"):
            repo_listing = tree_result.get("stdout", "")

        triage_stack = self._extract_triage_stack()
        stack = triage_stack if triage_stack is not None else detect_repo_stack(repo_listing)

        result = await self.tool_executor.execute(
            "shell_run", command=stack.lint_command, timeout=60
        )
        output = result.get("stdout", "") + result.get("stderr", "")
        return {"passed": result.get("success", False), "output": output[:3000]}

    def _extract_triage_stack(self) -> Any:
        """Inherit the detected repo stack from the triage phase."""
        from engine.tools.test_runner import RepoStack

        for result in reversed(self.prior_results):
            if result.phase != "triage" or not result.success:
                continue
            stack_dict = result.artifacts.get("detected_stack")
            if not isinstance(stack_dict, dict) or "language" not in stack_dict:
                continue
            return RepoStack(
                language=stack_dict["language"],
                test_command=stack_dict.get("test_command", ""),
                lint_command=stack_dict.get("lint_command", ""),
                detected_from=f"triage_handoff+{stack_dict.get('detected_from', 'unknown')}",
                confidence=float(stack_dict.get("confidence", 0.0)),
            )
        return None


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _extract_failing_files(failure_details: dict[str, Any]) -> list[str]:
    """Extract unique file paths from CI failure annotations and error messages."""
    files: list[str] = []
    seen: set[str] = set()

    for ann in failure_details.get("annotations", []):
        path = ann.get("path", "")
        if path and path not in seen:
            seen.add(path)
            files.append(path)

    for msg in failure_details.get("error_messages", []):
        for token in msg.split():
            if "/" in token and "." in token.split("/")[-1]:
                clean = token.rstrip(":,;")
                if clean and clean not in seen:
                    seen.add(clean)
                    files.append(clean)
            if len(files) >= 20:
                break

    return files


def _build_trusted_context(observation: dict[str, Any]) -> str:
    """Build the trusted context block for the LLM."""
    failure_details = observation["failure_details"]
    parts: list[str] = [
        f"CI Failure Category: {observation['failure_category']}",
        f"Remediation Attempt: #{observation['remediation_iteration']}",
        "",
        f"Original fix description: {observation.get('original_description', 'N/A')}",
        "",
        "Failing checks:",
    ]

    for name in failure_details.get("failing_checks", [])[:10]:
        parts.append(f"  - {name}")

    failing_tests = failure_details.get("failing_tests", [])
    if failing_tests:
        parts.append("")
        parts.append("Failing tests:")
        for t in failing_tests[:20]:
            parts.append(f"  - {t}")

    file_contents = observation.get("file_contents", {})
    if file_contents:
        parts.append("")
        parts.append("Current file contents (files referenced in failures):")
        for path, content in list(file_contents.items())[:5]:
            parts.append(f"\n--- {path} ---")
            parts.append(content[:5000])

    prior = observation.get("prior_attempts", [])
    if prior:
        parts.append("")
        parts.append("PRIOR CI REMEDIATION ATTEMPTS (do NOT repeat failed strategies):")
        for i, attempt in enumerate(prior, 1):
            outcome = "succeeded" if attempt.get("success") else "FAILED"
            parts.append(f"  Attempt #{i} ({outcome}):")
            parts.append(f"    Strategy: {attempt.get('fix_strategy', 'unknown')}")

            analysis = attempt.get("analysis", "")
            if analysis:
                parts.append(f"    Analysis: {analysis[:500]}")

            files = attempt.get("files_changed", [])
            if files:
                parts.append(f"    Files changed: {', '.join(files[:10])}")

            lint_output = attempt.get("lint_output", "")
            if lint_output:
                parts.append(f"    Lint output: {lint_output[:500]}")

            expected = attempt.get("expected_resolution", "")
            if expected:
                parts.append(f"    Expected resolution: {expected[:200]}")

    original_diff = observation.get("original_diff", "")
    if original_diff:
        parts.append("")
        parts.append("Original bug fix diff:")
        parts.append(original_diff[:5000])

    return "\n".join(parts)


def _build_untrusted_context(failure_details: dict[str, Any]) -> str:
    """Build the untrusted context block from CI error messages and logs."""
    parts: list[str] = []

    error_messages = failure_details.get("error_messages", [])
    if error_messages:
        parts.append("Error messages from CI:")
        for msg in error_messages[:15]:
            parts.append(f"  {msg[:500]}")

    annotations = failure_details.get("annotations", [])
    if annotations:
        parts.append("")
        parts.append("CI annotations:")
        for ann in annotations[:20]:
            path = ann.get("path", "?")
            line = ann.get("start_line", "?")
            level = ann.get("annotation_level", "?")
            msg = ann.get("message", "")
            parts.append(f"  [{level}] {path}:{line}: {msg[:300]}")

    log_excerpts = failure_details.get("log_excerpts", [])
    if log_excerpts:
        parts.append("")
        parts.append("Log excerpts:")
        for excerpt in log_excerpts[:5]:
            parts.append(excerpt[:3000])

    return "\n".join(parts)


def _parse_remediation_response(content: str) -> dict[str, Any]:
    """Parse the LLM's JSON remediation plan from its response."""
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        pass

    if "```json" in content:
        try:
            start = content.index("```json") + len("```json")
            end = content.index("```", start)
            return json.loads(content[start:end].strip())
        except (json.JSONDecodeError, ValueError):
            pass

    if "```" in content:
        parts = content.split("```")
        for i in range(1, len(parts), 2):
            text = parts[i].strip()
            if text.startswith("json"):
                text = text[4:].strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue

    return {
        "analysis": f"Failed to parse LLM response. Raw: {content[:500]}",
        "fix_strategy": "unknown",
        "is_code_fix": False,
        "file_changes": [],
        "expected_resolution": "",
        "pre_existing_failures": [],
    }
