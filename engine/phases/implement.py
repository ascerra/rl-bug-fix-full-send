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


class ImplementPhase(Phase):
    """Generate and validate a minimal bug fix.

    Uses ``file_read``, ``file_write``, ``file_search``, ``shell_run``,
    ``git_diff``, and ``git_commit``. The inner iteration loop re-invokes
    the LLM with test/lint failure output until the fix passes or the
    inner iteration limit is reached.
    """

    name = "implement"
    allowed_tools: ClassVar[list[str]] = []

    async def observe(self) -> dict[str, Any]:
        """Gather triage output, re-read the issue, and read affected files."""
        self.logger.info("Observing: gathering triage context and affected code")

        triage_report = self._extract_triage_report()
        affected_components = [
            c if isinstance(c, str) else c.get("path", "")
            for c in triage_report.get("affected_components", [])
        ]

        file_contents: dict[str, str] = {}
        if self.tool_executor and affected_components:
            for component in affected_components[:10]:
                if not component:
                    continue
                result = await self.tool_executor.execute("file_read", path=component)
                if result.get("success"):
                    file_contents[component] = result.get("content", "")

        repo_structure = ""
        if self.tool_executor:
            tree = await self.tool_executor.execute(
                "shell_run",
                command=(
                    "find . -type f "
                    "\\( -name '*.py' -o -name '*.go' -o -name '*.js' -o -name '*.ts' "
                    "-o -name '*.yaml' -o -name '*.yml' -o -name '*.rs' \\) "
                    "| grep -v node_modules | grep -v __pycache__ | sort | head -100"
                ),
            )
            if tree.get("success"):
                repo_structure = tree["stdout"]

        return {
            "issue": dict(self.issue_data),
            "triage_report": triage_report,
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

        self.tracer.record_llm_call(
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

        return {
            "impl_plan": impl_plan,
            "raw_llm_response": llm_response.content,
            "observation": observation,
        }

    async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Apply the fix with an inner iteration loop: write → test → lint → repeat."""
        self.logger.info("Acting: applying fix and running inner iteration loop")

        impl_plan = plan.get("impl_plan", {})
        max_inner = self.config.phases.implement.max_inner_iterations
        run_tests = self.config.phases.implement.run_tests_after_each_edit
        run_linters = self.config.phases.implement.run_linters
        actions: list[dict[str, Any]] = []

        files_written = await self._apply_fix(impl_plan, actions)

        test_result: dict[str, Any] = {"passed": False, "output": ""}
        lint_result: dict[str, Any] = {"passed": False, "output": ""}

        if run_tests:
            test_result = await self._run_tests(actions)
        else:
            test_result = {"passed": True, "output": "Tests skipped by config"}

        if run_linters:
            lint_result = await self._run_linters(actions)
        else:
            lint_result = {"passed": True, "output": "Linters skipped by config"}

        inner_iterations = 0
        both_pass = test_result["passed"] and lint_result["passed"]
        while inner_iterations < max_inner and not both_pass:
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

            if run_tests:
                test_result = await self._run_tests(actions)
            if run_linters:
                lint_result = await self._run_linters(actions)
            both_pass = test_result["passed"] and lint_result["passed"]

        diff_output = ""
        if self.tool_executor:
            diff_result = await self.tool_executor.execute("git_diff", ref="HEAD")
            if diff_result.get("success"):
                diff_output = diff_result.get("stdout", "")

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

        tests_passing = validation.get("tests_passing", False)
        linters_passing = validation.get("linters_passing", False)

        if not tests_passing and not linters_passing:
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=True,
                findings={
                    "validation_issues": issues,
                    "impl_plan": impl_plan,
                },
            )

        return PhaseResult(
            phase=self.name,
            success=False,
            should_continue=True,
            findings={"validation_issues": issues, "impl_plan": impl_plan},
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
        """Run tests and return structured result."""
        if not self.tool_executor:
            return {"passed": False, "output": "No tool executor available"}

        result = await self.tool_executor.execute(
            "shell_run",
            command=(
                "python -m pytest --tb=short -q 2>&1 "
                "|| go test ./... 2>&1 "
                "|| npm test 2>&1 "
                "|| echo 'No test runner detected'"
            ),
            timeout=120,
        )
        output = result.get("stdout", "") + result.get("stderr", "")
        passed = result.get("success", False)
        actions.append({"action": "run_tests", "success": passed})
        return {"passed": passed, "output": output[:3000]}

    async def _run_linters(self, actions: list[dict[str, Any]]) -> dict[str, Any]:
        """Run linters and return structured result."""
        if not self.tool_executor:
            return {"passed": False, "output": "No tool executor available"}

        result = await self.tool_executor.execute(
            "shell_run",
            command=(
                "ruff check . 2>&1 "
                "|| golangci-lint run ./... 2>&1 "
                "|| npx eslint . 2>&1 "
                "|| echo 'No linter detected'"
            ),
            timeout=60,
        )
        output = result.get("stdout", "") + result.get("stderr", "")
        passed = result.get("success", False)
        actions.append({"action": "run_linters", "success": passed})
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

        self.tracer.record_llm_call(
            description=f"Implementation refinement (iteration {iteration})",
            model=llm_response.model,
            provider=llm_response.provider,
            tokens_in=llm_response.tokens_in,
            tokens_out=llm_response.tokens_out,
            latency_ms=llm_response.latency_ms,
            prompt_summary=f"Refinement prompt with test/lint failures (iter {iteration})",
            response_summary=llm_response.content[:500],
        )

        return parse_implement_response(llm_response.content)


def parse_implement_response(content: str) -> dict[str, Any]:
    """Extract structured implementation JSON from an LLM response.

    Tries direct JSON parse, then ``json`` code-block extraction, then
    generic code-block extraction. Returns a default empty-plan on parse failure.
    """
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
        "root_cause": "unknown",
        "fix_description": f"Failed to parse LLM response. Raw: {content[:500]}",
        "files_changed": [],
        "file_changes": [],
        "test_added": "",
        "tests_passing": False,
        "linters_passing": False,
        "confidence": 0.0,
        "diff_summary": "",
    }
