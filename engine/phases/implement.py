"""Implementation Phase — read triage output, analyze code, generate fix, test, lint.

Implements SPEC §5.2:
1. Re-read the issue AND triage output independently (zero trust)
2. Analyze affected code
3. Generate a minimal fix via LLM
4. Run tests after each edit (inner iteration loop)
5. Run linters
6. Produce a structured implementation report
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from engine.phases.base import Phase, PhaseResult
from engine.tools.test_runner import RepoStack, detect_repo_stack


class ImplementPhase(Phase):
    """Generate and validate a minimal bug fix.

    Uses ``file_read``, ``file_write``, ``file_search``, ``shell_run``,
    ``git_diff``, and ``git_commit``. The inner iteration loop re-invokes
    the LLM with test/lint failure output until the fix passes or the
    inner iteration limit is reached.
    """

    name = "implement"
    allowed_tools: ClassVar[list[str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._detected_stack: RepoStack | None = None

    async def observe(self) -> dict[str, Any]:
        """Gather triage output, review feedback, retry context, and read affected files."""
        self.logger.info("Observing: gathering triage context and affected code")

        triage_report = self._extract_triage_report()
        review_feedback = self._extract_review_feedback()
        retry_context = self._extract_retry_context()
        retry_count = len(retry_context)

        if retry_context:
            self.logger.info(f"Retry context: {retry_count} prior failed attempt(s)")

        if review_feedback:
            self.logger.info(
                f"Review feedback present (verdict={review_feedback.get('verdict')}, "
                f"findings={len(review_feedback.get('findings', []))})"
            )

        affected_components = [
            c if isinstance(c, str) else c.get("path", "")
            for c in triage_report.get("affected_components", [])
        ]

        previously_tried_files = _collect_previously_tried_files(retry_context)

        file_contents: dict[str, str] = {}
        if self.tool_executor and affected_components:
            for component in affected_components[:10]:
                if not component:
                    continue
                result = await self.tool_executor.execute("file_read", path=component)
                if result.get("success"):
                    file_contents[component] = result.get("content", "")

        if not file_contents and self.tool_executor:
            file_contents = await self._search_relevant_files(
                retry_count=retry_count,
                exclude_files=previously_tried_files,
            )

        repo_structure = ""
        if self.tool_executor:
            tree = await self.tool_executor.execute(
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
            if tree.get("success"):
                repo_structure = tree["stdout"]

        triage_stack = self._extract_triage_stack()
        if triage_stack is not None:
            self._detected_stack = triage_stack
            self.logger.info(
                f"Inherited repo stack from triage: {self._detected_stack.language} "
                f"(from {self._detected_stack.detected_from}, "
                f"confidence={self._detected_stack.confidence:.2f})"
            )
        else:
            self._detected_stack = detect_repo_stack(
                repo_structure,
                test_command_override=self.config.phases.implement.test_command,
                lint_command_override=self.config.phases.implement.lint_command,
            )
            self.logger.info(
                f"Detected repo stack (independent): {self._detected_stack.language} "
                f"(from {self._detected_stack.detected_from}, "
                f"confidence={self._detected_stack.confidence:.2f})"
            )

        n_files = len(file_contents)
        retry_count_val = retry_count
        review_present = "present" if review_feedback else "absent"
        self.logger.narrate(
            f"Gathered context: {n_files} files read. "
            f"Retry #{retry_count_val}. Review feedback: {review_present}."
        )

        return {
            "issue": dict(self.issue_data),
            "triage_report": triage_report,
            "review_feedback": review_feedback,
            "retry_context": retry_context,
            "retry_count": retry_count,
            "affected_components": affected_components,
            "file_contents": file_contents,
            "repo_structure": repo_structure,
        }

    async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Call LLM with the implementation prompt to generate a fix strategy."""
        self.logger.info("Planning: generating fix strategy via LLM")

        system_prompt = self.load_system_prompt()

        issue_title = self.issue_data.get("title", "N/A")
        issue_body = self.issue_data.get("body", issue_title)
        triage_report = observation.get("triage_report", {})
        review_feedback = observation.get("review_feedback", {})
        retry_context = observation.get("retry_context", [])

        file_context_parts: list[str] = []
        for path, content in observation.get("file_contents", {}).items():
            truncated = content[:5000] if len(content) > 5000 else content
            file_context_parts.append(f"--- {path} ---\n{truncated}")
        file_context = "\n\n".join(file_context_parts) if file_context_parts else "N/A"

        trusted_context = (
            f"Triage summary (verify independently):\n"
            f"  Classification: {triage_report.get('classification', 'N/A')}\n"
            f"  Severity: {triage_report.get('severity', 'N/A')}\n"
            f"  Affected components: {triage_report.get('affected_components', [])}\n"
            f"  Root cause hint: {triage_report.get('reasoning', 'N/A')}\n\n"
            f"Repository structure:\n{observation.get('repo_structure', 'N/A')}\n\n"
            f"Affected file contents:\n{file_context}"
        )

        if review_feedback and review_feedback.get("verdict"):
            trusted_context += "\n\n" + _format_review_feedback(review_feedback)

        if retry_context:
            trusted_context += "\n\n" + _format_retry_context(retry_context)

        untrusted = self._wrap_untrusted_content(
            f"Issue title: {issue_title}\n\nIssue body:\n{issue_body}"
        )
        user_message = f"{trusted_context}\n\n{untrusted}"

        llm_response = await self.llm.complete(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            temperature=self.config.llm.temperature,
            max_tokens=self.config.llm.max_tokens,
        )

        self.record_llm_call(
            description="Implementation fix planning",
            model=llm_response.model,
            provider=llm_response.provider,
            tokens_in=llm_response.tokens_in,
            tokens_out=llm_response.tokens_out,
            latency_ms=llm_response.latency_ms,
            prompt_summary="Implement system prompt + triage context + code + issue",
            response_summary=llm_response.content[:500],
        )

        impl_plan = parse_implement_response(llm_response.content)

        impl_plan, raw_content = await self._parse_with_retry(
            impl_plan,
            llm_response.content,
            system_prompt,
            user_message,
            "Implementation fix planning",
        )

        fix_desc = impl_plan.get("fix_description", "N/A")[:80]
        n_changes = len(impl_plan.get("file_changes", []))
        self.logger.narrate(f"Fix strategy: {fix_desc}. {n_changes} file change(s) proposed.")

        return {
            "impl_plan": impl_plan,
            "raw_llm_response": raw_content,
            "observation": observation,
        }

    async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Apply the fix with an inner iteration loop: write → test → lint → repeat."""
        self.logger.info("Acting: applying fix and running inner iteration loop")

        impl_plan = plan.get("impl_plan", {})
        max_inner = self.config.phases.implement.max_inner_iterations
        test_exec_mode = self.config.phases.implement.test_execution_mode
        run_linters = self.config.phases.implement.run_linters
        actions: list[dict[str, Any]] = []

        should_run_tests = test_exec_mode in ("opportunistic", "required")
        tests_gate = test_exec_mode == "required"

        files_written = await self._apply_fix(impl_plan, actions)

        test_result: dict[str, Any] = {"passed": False, "output": ""}
        lint_result: dict[str, Any] = {"passed": False, "output": ""}

        if should_run_tests:
            test_result = await self._run_tests(actions)
        else:
            test_result = {"passed": True, "output": "Tests skipped (disabled mode)"}

        if run_linters:
            lint_result = await self._run_linters(actions)
        else:
            lint_result = {"passed": True, "output": "Linters skipped by config"}

        inner_iterations = 0
        if tests_gate:
            all_pass = test_result["passed"] and lint_result["passed"]
        else:
            all_pass = lint_result["passed"]
        while inner_iterations < max_inner and not all_pass:
            inner_iterations += 1
            self.logger.info(
                f"Inner iteration {inner_iterations}/{max_inner}: "
                f"tests={'pass' if test_result['passed'] else 'FAIL'}, "
                f"lint={'pass' if lint_result['passed'] else 'FAIL'}"
            )

            refinement = await self._request_refinement(
                impl_plan, test_result, lint_result, plan, inner_iterations
            )
            files_written = await self._apply_fix(refinement, actions)

            if should_run_tests:
                test_result = await self._run_tests(actions)
            if run_linters:
                lint_result = await self._run_linters(actions)
            if tests_gate:
                all_pass = test_result["passed"] and lint_result["passed"]
            else:
                all_pass = lint_result["passed"]

        diff_output = ""
        if self.tool_executor:
            diff_result = await self.tool_executor.execute("git_diff", ref="HEAD")
            if diff_result.get("success"):
                diff_output = diff_result.get("stdout", "")

        n_written = len(files_written)
        t_pass = "PASS" if test_result.get("passed") else "FAIL"
        l_pass = "PASS" if lint_result.get("passed") else "FAIL"
        self.logger.narrate(
            f"Wrote {n_written} file(s). Tests: {t_pass}. Lint: {l_pass}. "
            f"Inner iterations: {inner_iterations}/{max_inner}."
        )

        return {
            "impl_plan": impl_plan,
            "files_written": files_written,
            "test_result": test_result,
            "lint_result": lint_result,
            "inner_iterations": inner_iterations,
            "max_inner_iterations": max_inner,
            "diff": diff_output,
            "actions": actions,
        }

    async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
        """Verify the fix: tests pass, linters pass, files were changed."""
        self.logger.info("Validating: checking fix correctness")

        issues: list[str] = []
        test_result = action_result.get("test_result", {})
        lint_result = action_result.get("lint_result", {})
        files_written = action_result.get("files_written", [])

        if not test_result.get("passed", False):
            issues.append(f"Tests failing: {test_result.get('output', 'unknown')[:200]}")

        if not lint_result.get("passed", False):
            issues.append(f"Linters failing: {lint_result.get('output', 'unknown')[:200]}")

        if not files_written:
            issues.append("No files were modified — fix may not have been applied")

        diff = action_result.get("diff", "")
        if not diff and files_written:
            issues.append("No git diff detected despite file writes")

        n_issues = len(issues)
        if n_issues == 0:
            self.logger.narrate("Implementation validated — all checks pass.")
        else:
            self.logger.narrate(f"Validation found {n_issues} issue(s).")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "tests_passing": test_result.get("passed", False),
            "linters_passing": lint_result.get("passed", False),
            "files_changed": files_written,
            "inner_iterations_used": action_result.get("inner_iterations", 0),
            "diff": diff,
            "impl_plan": action_result.get("impl_plan", {}),
        }

    async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
        """Decide next step: advance to review, retry, or escalate."""
        self.logger.info("Reflecting: deciding implementation outcome")

        impl_plan = validation.get("impl_plan", {})

        if validation.get("valid"):
            self.logger.narrate(
                f"Implementation succeeded. {len(validation.get('files_changed', []))} "
                "file(s) changed. Moving to review."
            )
            return PhaseResult(
                phase=self.name,
                success=True,
                should_continue=True,
                next_phase="review",
                findings=impl_plan,
                artifacts={
                    "files_changed": validation.get("files_changed", []),
                    "diff": validation.get("diff", ""),
                    "inner_iterations_used": validation.get("inner_iterations_used", 0),
                },
            )

        issues = validation.get("issues", [])
        self.logger.warn(f"Implementation validation issues: {issues}")

        failure_findings: dict[str, Any] = {
            "validation_issues": issues,
            "impl_plan": impl_plan,
        }
        failure_artifacts: dict[str, Any] = {
            "files_changed": validation.get("files_changed", []),
        }

        self.logger.narrate(f"Implementation has issues: {'; '.join(issues[:2])}. Will retry.")
        return PhaseResult(
            phase=self.name,
            success=False,
            should_continue=True,
            findings=failure_findings,
            artifacts=failure_artifacts,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _parse_with_retry(
        self,
        initial_plan: dict[str, Any],
        raw_content: str,
        system_prompt: str,
        user_message: str,
        description: str,
    ) -> tuple[dict[str, Any], str]:
        """Validate an impl plan and retry the LLM call on parse/validation failure.

        Returns ``(plan, raw_content)`` — either the original on success or the
        retry result. If both attempts fail, returns whichever has more usable
        data (prefers a successfully-parsed response with empty file_changes over
        a total parse failure).
        """
        issues = validate_impl_plan(initial_plan)
        if not issues:
            return initial_plan, raw_content

        max_retries = self.config.phases.implement.max_parse_retries

        self.logger.warn(
            f"LLM response validation failed ({len(issues)} issue(s)): "
            f"{'; '.join(issues[:3])}. "
            f"Raw response (truncated): {raw_content[:300]}"
        )

        best_plan = initial_plan
        best_content = raw_content

        for attempt in range(max_retries):
            self.logger.info(
                f"Retrying LLM call for valid JSON (attempt {attempt + 1}/{max_retries})"
            )

            retry_message = (
                f"{user_message}\n\n"
                "IMPORTANT: Your previous response was not valid or was incomplete. "
                f"Issues: {'; '.join(issues[:5])}.\n\n"
                "You MUST respond with ONLY a valid JSON object — no markdown, "
                "no explanation, no code fences. The JSON must include a non-empty "
                "`file_changes` array where each entry has a non-empty `path` string "
                "and a non-empty `content` string containing the COMPLETE file content."
            )

            llm_response = await self.llm.complete(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": retry_message}],
                temperature=self.config.llm.temperature,
                max_tokens=self.config.llm.max_tokens,
                json_mode=True,
            )

            self.record_llm_call(
                description=f"{description} (parse retry {attempt + 1})",
                model=llm_response.model,
                provider=llm_response.provider,
                tokens_in=llm_response.tokens_in,
                tokens_out=llm_response.tokens_out,
                latency_ms=llm_response.latency_ms,
                prompt_summary="JSON-only retry prompt",
                response_summary=llm_response.content[:500],
            )

            retry_plan = parse_implement_response(llm_response.content)
            retry_issues = validate_impl_plan(retry_plan)

            if not retry_issues:
                self.logger.info("Parse retry succeeded — got valid JSON with file_changes")
                return retry_plan, llm_response.content

            self.logger.warn(
                f"Parse retry {attempt + 1} also failed: {'; '.join(retry_issues[:3])}"
            )

            if not is_parse_failure(retry_plan) and is_parse_failure(best_plan):
                best_plan = retry_plan
                best_content = llm_response.content

        return best_plan, best_content

    def _extract_triage_stack(self) -> RepoStack | None:
        """Inherit the detected repo stack from the triage phase.

        Triage serializes its ``RepoStack`` into ``PhaseResult.artifacts["detected_stack"]``.
        Using the triage stack prevents re-detection errors caused by truncated file
        listings (D17).  Config overrides for test/lint commands are applied on top.
        """
        for result in reversed(self.prior_results):
            if result.phase != "triage" or not result.success:
                continue
            stack_dict = result.artifacts.get("detected_stack")
            if not isinstance(stack_dict, dict) or "language" not in stack_dict:
                continue
            test_override = self.config.phases.implement.test_command
            lint_override = self.config.phases.implement.lint_command
            return RepoStack(
                language=stack_dict["language"],
                test_command=test_override or stack_dict.get("test_command", ""),
                lint_command=lint_override or stack_dict.get("lint_command", ""),
                detected_from=f"triage_handoff+{stack_dict.get('detected_from', 'unknown')}",
                confidence=float(stack_dict.get("confidence", 0.0)),
            )
        return None

    def _extract_triage_report(self) -> dict[str, Any]:
        """Extract the triage report from prior phase results (if available)."""
        for result in reversed(self.prior_results):
            if result.phase == "triage" and result.success:
                report = result.artifacts.get("triage_report")
                if report:
                    return report
                if result.findings:
                    return result.findings
        return {}

    def _extract_review_feedback(self) -> dict[str, Any]:
        """Extract the most recent review feedback from prior phase results.

        When the review phase returns ``request_changes`` and backtracks to
        implement, its ``PhaseResult`` contains the review report in
        ``findings`` and/or ``artifacts.review_report``.  This method reads
        that data so the implementer can see what the reviewer flagged and
        adapt its approach.
        """
        for result in reversed(self.prior_results):
            if result.phase != "review":
                continue
            review = result.artifacts.get("review_report") or result.findings
            if not review:
                continue
            return {
                "verdict": review.get("verdict", ""),
                "findings": review.get("findings", []),
                "summary": review.get("summary", ""),
                "scope_assessment": review.get("scope_assessment", ""),
            }
        return {}

    def _extract_retry_context(self) -> list[dict[str, Any]]:
        """Extract context from prior failed implement attempts.

        Scans ``self.prior_results`` for prior ``implement`` failures and
        extracts what was tried and why it failed, so the next attempt can
        adapt its strategy.
        """
        retries: list[dict[str, Any]] = []
        for result in self.prior_results:
            if result.phase != "implement" or result.success:
                continue
            impl_plan = result.findings.get("impl_plan", {})
            retry: dict[str, Any] = {
                "attempt": len(retries) + 1,
                "validation_issues": result.findings.get("validation_issues", []),
                "approach": impl_plan.get("fix_description", ""),
                "root_cause_guess": impl_plan.get("root_cause", ""),
                "files_attempted": (
                    result.artifacts.get("files_changed", [])
                    or result.findings.get("impl_plan", {}).get("files_changed", [])
                ),
            }
            retries.append(retry)
        return retries

    async def _search_relevant_files(
        self,
        retry_count: int = 0,
        exclude_files: set[str] | None = None,
    ) -> dict[str, str]:
        """Fallback file search with escalating strategy based on retry count.

        - **Retry 0**: keyword search (> 4 chars) from issue title/body
        - **Retry 1**: broader keywords (> 3 chars), more results
        - **Retry 2+**: broad file listing of all source files
        """
        assert self.tool_executor is not None
        exclude = exclude_files or set()

        if retry_count >= 2:
            return await self._broad_file_scan(exclude)

        title = self.issue_data.get("title", "")
        body = self.issue_data.get("body", "")
        min_len = 4 if retry_count == 0 else 3
        max_keywords = 5 if retry_count == 0 else 8
        max_files = 5 if retry_count == 0 else 8

        keywords = _extract_keywords(title, body, min_len=min_len, max_keywords=max_keywords)

        if not keywords:
            return await self._broad_file_scan(exclude)

        file_contents: dict[str, str] = {}
        seen_files: set[str] = set(exclude)
        for kw in keywords:
            result = await self.tool_executor.execute(
                "shell_run",
                command=(
                    f"grep -rl '{kw}' --include='*.yaml' --include='*.yml'"
                    " --include='*.py' --include='*.go' --include='*.ts'"
                    " . 2>/dev/null | head -5"
                ),
                timeout=15,
            )
            if result.get("success"):
                for path in result.get("stdout", "").strip().splitlines():
                    path = path.strip()
                    if path and path not in seen_files and len(file_contents) < max_files:
                        seen_files.add(path)
                        read_result = await self.tool_executor.execute("file_read", path=path)
                        if read_result.get("success"):
                            file_contents[path] = read_result.get("content", "")

        if file_contents:
            self.logger.info(
                f"Fallback file search (strategy={retry_count}) found "
                f"{len(file_contents)} files: {list(file_contents.keys())}"
            )
        elif retry_count < 2:
            self.logger.info("Keyword search found nothing — escalating to broad file scan")
            return await self._broad_file_scan(exclude)
        return file_contents

    async def _broad_file_scan(self, exclude: set[str] | None = None) -> dict[str, str]:
        """List all source files and read the most likely candidates.

        Used as a last-resort strategy when keyword search fails. Reads the
        shortest source files first (shorter files are more likely to be
        focused modules with clear purpose).
        """
        assert self.tool_executor is not None
        exclude = exclude or set()

        result = await self.tool_executor.execute(
            "shell_run",
            command=(
                "find . -type f "
                "\\( -name '*.py' -o -name '*.go' -o -name '*.js' -o -name '*.ts' "
                "-o -name '*.yaml' -o -name '*.yml' -o -name '*.rs' \\) "
                "| grep -v node_modules | grep -v __pycache__ | grep -v vendor "
                "| grep -v .git | head -30"
            ),
            timeout=15,
        )
        if not result.get("success"):
            return {}

        candidates = [
            p.strip()
            for p in result.get("stdout", "").strip().splitlines()
            if p.strip() and p.strip() not in exclude
        ]

        file_contents: dict[str, str] = {}
        for path in candidates[:8]:
            read_result = await self.tool_executor.execute("file_read", path=path)
            if read_result.get("success"):
                file_contents[path] = read_result.get("content", "")

        if file_contents:
            self.logger.info(
                f"Broad file scan found {len(file_contents)} files: {list(file_contents.keys())}"
            )
        return file_contents

    async def _apply_fix(
        self,
        impl_plan: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> list[str]:
        """Write files specified in the implementation plan."""
        files_written: list[str] = []
        if not self.tool_executor:
            return files_written

        file_changes = impl_plan.get("file_changes", [])
        for change in file_changes:
            path = change.get("path", "")
            content = change.get("content", "")
            if not path or not content:
                continue
            result = await self.tool_executor.execute("file_write", path=path, content=content)
            success = result.get("success", False)
            files_written.append(path)
            actions.append(
                {
                    "action": "file_write",
                    "path": path,
                    "success": success,
                }
            )

        return files_written

    async def _run_tests(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """Run tests using the detected repo stack's test command."""
        if not self.tool_executor:
            return {"passed": False, "output": "No tool executor available"}

        cmd = (
            self._detected_stack.test_command
            if self._detected_stack
            else "echo 'No test runner detected'"
        )

        result = await self.tool_executor.execute("shell_run", command=cmd, timeout=120)
        output = result.get("stdout", "") + result.get("stderr", "")
        passed = result.get("success", False)
        actions.append({"action": "run_tests", "success": passed, "command": cmd})
        return {"passed": passed, "output": output[:3000]}

    async def _run_linters(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """Run linters using the detected repo stack's lint command."""
        if not self.tool_executor:
            return {"passed": False, "output": "No tool executor available"}

        cmd = (
            self._detected_stack.lint_command
            if self._detected_stack
            else "echo 'No linter detected'"
        )

        result = await self.tool_executor.execute("shell_run", command=cmd, timeout=60)
        output = result.get("stdout", "") + result.get("stderr", "")
        passed = result.get("success", False)
        actions.append({"action": "run_linters", "success": passed, "command": cmd})
        return {"passed": passed, "output": output[:3000]}

    async def _request_refinement(
        self,
        original_plan: dict[str, Any],
        test_result: dict[str, Any],
        lint_result: dict[str, Any],
        plan_context: dict[str, Any],
        iteration: int,
    ) -> dict[str, Any]:
        """Re-invoke the LLM with failure output to refine the fix."""
        self.logger.info(f"Requesting LLM refinement (inner iteration {iteration})")

        system_prompt = self.load_system_prompt()

        failure_context = "Your previous fix attempt failed. Here is the feedback:\n\n"
        if not test_result.get("passed", False):
            failure_context += f"TEST FAILURES:\n{test_result.get('output', 'N/A')}\n\n"
        if not lint_result.get("passed", False):
            failure_context += f"LINT ERRORS:\n{lint_result.get('output', 'N/A')}\n\n"
        failure_context += (
            f"Previous fix plan:\n{json.dumps(original_plan, indent=2)[:3000]}\n\n"
            "Please provide a corrected fix that addresses these failures."
        )

        issue_title = self.issue_data.get("title", "N/A")
        issue_body = self.issue_data.get("body", issue_title)
        untrusted = self._wrap_untrusted_content(
            f"Issue title: {issue_title}\n\nIssue body:\n{issue_body}"
        )

        user_message = f"{failure_context}\n\n{untrusted}"

        llm_response = await self.llm.complete(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_message}],
            temperature=self.config.llm.temperature,
            max_tokens=self.config.llm.max_tokens,
        )

        self.record_llm_call(
            description=f"Implementation refinement (iteration {iteration})",
            model=llm_response.model,
            provider=llm_response.provider,
            tokens_in=llm_response.tokens_in,
            tokens_out=llm_response.tokens_out,
            latency_ms=llm_response.latency_ms,
            prompt_summary=f"Refinement prompt with test/lint failures (iter {iteration})",
            response_summary=llm_response.content[:500],
        )

        impl_plan = parse_implement_response(llm_response.content)

        impl_plan, _ = await self._parse_with_retry(
            impl_plan,
            llm_response.content,
            system_prompt,
            user_message,
            f"Implementation refinement retry (iteration {iteration})",
        )

        return impl_plan


_STOPWORDS: set[str] = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "when",
    "have",
    "been",
    "does",
    "not",
    "are",
    "was",
    "were",
    "but",
    "they",
    "will",
    "can",
    "should",
    "would",
    "could",
    "about",
    "into",
    "more",
    "some",
    "than",
    "then",
    "also",
    "just",
    "only",
    "each",
    "other",
    "after",
    "before",
    "between",
    "through",
    "error",
    "issue",
    "bug",
    "fix",
    "none",
    "null",
    "true",
    "false",
}


def _extract_keywords(
    title: str,
    body: str,
    min_len: int = 5,
    max_keywords: int = 5,
) -> list[str]:
    """Extract meaningful search keywords from issue title and body.

    Filters out stopwords and trivially short/meaningless tokens.
    """
    keywords: list[str] = []
    seen: set[str] = set()

    for word in title.split():
        if len(keywords) >= max_keywords:
            break
        cleaned = word.strip(".,;:!?()[]{}\"'`")
        lower = cleaned.lower()
        if (
            len(cleaned) >= min_len
            and cleaned.replace("-", "").replace("_", "").isalnum()
            and lower not in _STOPWORDS
            and lower not in seen
            and lower != "n/a"
        ):
            seen.add(lower)
            keywords.append(cleaned)

    if len(keywords) < max_keywords:
        for word in body.split()[:200]:
            cleaned = word.strip(".,;:!?()[]{}\"'`")
            lower = cleaned.lower()
            if (
                len(cleaned) >= max(min_len, 4)
                and cleaned.replace("-", "").replace("_", "").isalnum()
                and lower not in _STOPWORDS
                and lower not in seen
                and lower != "n/a"
            ):
                seen.add(lower)
                keywords.append(cleaned)
                if len(keywords) >= max_keywords:
                    break

    return keywords


def _collect_previously_tried_files(
    retry_context: list[dict[str, Any]],
) -> set[str]:
    """Collect file paths from all prior retry attempts."""
    files: set[str] = set()
    for retry in retry_context:
        for f in retry.get("files_attempted", []):
            if isinstance(f, str) and f:
                files.add(f)
    return files


def _format_retry_context(retries: list[dict[str, Any]]) -> str:
    """Format prior implementation attempts as structured text for the LLM.

    Produces a ``PRIOR IMPLEMENTATION ATTEMPTS`` section that tells the LLM
    what was already tried and why it failed, so it can adapt its strategy.
    """
    if not retries:
        return ""
    lines: list[str] = [
        f"PRIOR IMPLEMENTATION ATTEMPTS ({len(retries)} failed "
        "— you MUST try a different approach):",
    ]
    for r in retries:
        lines.append(f"  Attempt {r['attempt']}:")
        if r.get("approach"):
            lines.append(f"    Approach tried: {r['approach'][:300]}")
        if r.get("root_cause_guess"):
            lines.append(f"    Root cause guess: {r['root_cause_guess'][:300]}")
        files = r.get("files_attempted", [])
        if files:
            lines.append(f"    Files modified: {', '.join(str(f) for f in files[:10])}")
        else:
            lines.append("    Files modified: NONE (no files were changed)")
        issues = r.get("validation_issues", [])
        if issues:
            lines.append(f"    Why it failed: {'; '.join(str(i) for i in issues[:5])}")
    lines.append("")
    lines.append(
        "IMPORTANT: Do NOT repeat any of the above approaches. "
        "Analyze the failure reasons and try a fundamentally different strategy."
    )
    return "\n".join(lines)


def _format_review_feedback(feedback: dict[str, Any]) -> str:
    """Format review feedback as a structured text block for the LLM context.

    Produces a ``PREVIOUS REVIEW FEEDBACK`` section that the implementer
    LLM can read to understand what the reviewer flagged and adapt.
    """
    lines: list[str] = [
        "PREVIOUS REVIEW FEEDBACK (address these issues in your fix):",
        f"  Verdict: {feedback.get('verdict', 'N/A')}",
        f"  Summary: {feedback.get('summary', 'N/A')}",
    ]
    findings = feedback.get("findings", [])
    if findings:
        lines.append(f"  Findings ({len(findings)}):")
        for i, f in enumerate(findings[:10], 1):
            dim = f.get("dimension", "unknown")
            sev = f.get("severity", "unknown")
            desc = f.get("description", "")
            suggestion = f.get("suggestion", "")
            file_path = f.get("file", "")
            line_num = f.get("line", "")
            loc = f"{file_path}:{line_num}" if file_path and line_num else file_path
            lines.append(f"    {i}. [{dim}/{sev}] {desc}")
            if loc:
                lines.append(f"       Location: {loc}")
            if suggestion:
                lines.append(f"       Suggestion: {suggestion}")
    return "\n".join(lines)


def parse_implement_response(content: str) -> dict[str, Any]:
    """Extract structured implementation JSON from an LLM response.

    Tries (in order):
      1. Direct JSON parse
      2. Strip leading/trailing whitespace and non-JSON preamble
      3. ``json`` code-block extraction
      4. Generic code-block extraction
      5. Find first ``{`` to last ``}`` brute-force extraction
    Returns a default empty-plan on parse failure.
    """
    if not content or not content.strip():
        return _EMPTY_PLAN("Empty LLM response")

    stripped = content.strip()

    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        pass

    if "```json" in stripped:
        try:
            start = stripped.index("```json") + len("```json")
            end = stripped.index("```", start)
            return json.loads(stripped[start:end].strip())
        except (json.JSONDecodeError, ValueError):
            pass

    if "```" in stripped:
        parts = stripped.split("```")
        for i in range(1, len(parts), 2):
            text = parts[i].strip()
            if text.startswith("json"):
                text = text[4:].strip()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue

    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = stripped[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return _EMPTY_PLAN(content[:500])


def _EMPTY_PLAN(raw_snippet: str) -> dict[str, Any]:
    return {
        "root_cause": "unknown",
        "fix_description": f"Failed to parse LLM response. Raw: {raw_snippet}",
        "files_changed": [],
        "file_changes": [],
        "test_added": "",
        "tests_passing": False,
        "linters_passing": False,
        "confidence": 0.0,
        "diff_summary": "",
    }


_PARSE_FAILURE_MARKER = "Failed to parse LLM response."


def is_parse_failure(plan: dict[str, Any]) -> bool:
    """Return True if the plan was produced by a failed parse (default dict)."""
    return (
        plan.get("root_cause") == "unknown"
        and plan.get("confidence") == 0.0
        and _PARSE_FAILURE_MARKER in plan.get("fix_description", "")
    )


def validate_impl_plan(plan: dict[str, Any]) -> list[str]:
    """Validate a parsed implementation plan has usable file_changes.

    Returns a list of issue descriptions. Empty list means valid.
    """
    issues: list[str] = []

    if is_parse_failure(plan):
        issues.append("JSON parse failure — response was not valid JSON")
        return issues

    file_changes = plan.get("file_changes")
    if not isinstance(file_changes, list) or len(file_changes) == 0:
        issues.append("file_changes is empty or missing — no files to write")
        return issues

    for i, entry in enumerate(file_changes):
        if not isinstance(entry, dict):
            issues.append(f"file_changes[{i}] is not a dict")
            continue
        path = entry.get("path", "")
        content = entry.get("content", "")
        if not path:
            issues.append(f"file_changes[{i}] has empty or missing 'path'")
        if not content:
            issues.append(f"file_changes[{i}] has empty or missing 'content'")

    return issues
