"""Triage Phase — classify the issue, identify affected components, attempt reproduction.

Implements SPEC §5.1:
1. Read the issue body (treat as untrusted input)
2. Classify: bug, feature request, or ambiguous
3. Identify affected files/components from issue + repo structure
4. Attempt reproduction (find and run related tests)
5. Produce a structured triage report
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from engine.phases.base import Phase, PhaseResult


class TriagePhase(Phase):
    """Analyze a GitHub issue to classify, scope, and attempt reproduction.

    The triage phase is read-only: it uses ``file_read``, ``file_search``,
    and ``shell_run`` but never writes files or commits. Classification
    and component identification are driven by the LLM; reproduction
    attempts run existing tests.
    """

    name = "triage"
    allowed_tools: ClassVar[list[str]] = []

    async def observe(self) -> dict[str, Any]:
        """Gather issue data, repo file listing, and existing test files."""
        self.logger.info("Observing: gathering issue data and repo structure")

        observation: dict[str, Any] = {
            "issue": dict(self.issue_data),
            "repo_path": self.repo_path,
            "repo_files": "",
            "test_files": "",
        }

        if not self.tool_executor:
            return observation

        tree = await self.tool_executor.execute(
            "shell_run",
            command=(
                "find . -type f "
                "\\( -name '*.py' -o -name '*.go' -o -name '*.js' -o -name '*.ts' "
                "-o -name '*.yaml' -o -name '*.yml' -o -name '*.rs' \\) "
                "| grep -v node_modules | grep -v __pycache__ | sort | head -200"
            ),
        )
        if tree.get("success"):
            observation["repo_files"] = tree["stdout"]

        tests = await self.tool_executor.execute(
            "shell_run",
            command=(
                "find . -type f "
                "\\( -name '*_test.go' -o -name 'test_*.py' -o -name '*_test.py' "
                "-o -name '*.test.ts' -o -name '*.test.js' -o -name '*.spec.ts' \\) "
                "| sort | head -50"
            ),
        )
        if tests.get("success"):
            observation["test_files"] = tests["stdout"]

        return observation

    async def plan(self, observation: dict[str, Any]) -> dict[str, Any]:
        """Call LLM with the triage prompt, issue content, and repo context."""
        self.logger.info("Planning: classifying issue via LLM")

        system_prompt = self.load_system_prompt()

        issue_url = self.issue_data.get("url", "unknown")
        issue_title = self.issue_data.get("title", "N/A")
        issue_body = self.issue_data.get("body", issue_title)

        trusted_context = (
            f"Issue URL: {issue_url}\n\n"
            f"Repository files:\n{observation.get('repo_files', 'N/A')}\n\n"
            f"Test files found:\n{observation.get('test_files', 'N/A')}"
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
            description="Triage classification",
            model=llm_response.model,
            provider=llm_response.provider,
            tokens_in=llm_response.tokens_in,
            tokens_out=llm_response.tokens_out,
            latency_ms=llm_response.latency_ms,
            prompt_summary="Triage system prompt + repo context + issue content",
            response_summary=llm_response.content[:500],
        )

        triage_result = parse_triage_response(llm_response.content)

        return {
            "triage_result": triage_result,
            "raw_llm_response": llm_response.content,
            "observation": observation,
        }

    async def act(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Verify affected components exist and attempt reproduction."""
        self.logger.info("Acting: verifying components and attempting reproduction")

        triage = plan.get("triage_result", {})
        actions: list[dict[str, Any]] = []

        verified_components = await self._verify_components(
            triage.get("affected_components", []), actions
        )

        reproduction = await self._attempt_reproduction(plan, triage, actions)

        return {
            "triage_result": triage,
            "verified_components": verified_components,
            "reproduction": reproduction,
            "actions": actions,
        }

    async def validate(self, action_result: dict[str, Any]) -> dict[str, Any]:
        """Check that the triage report is structurally valid."""
        self.logger.info("Validating: checking triage report coherence")

        triage = action_result.get("triage_result", {})
        issues: list[str] = []

        classification = triage.get("classification", "")
        if classification not in ("bug", "feature", "ambiguous"):
            issues.append(f"Invalid classification: '{classification}'")

        confidence = triage.get("confidence", -1)
        if not isinstance(confidence, (int, float)) or not (0 <= confidence <= 1):
            issues.append(f"Invalid confidence: {confidence}")

        severity = triage.get("severity", "")
        if severity not in ("critical", "high", "medium", "low"):
            issues.append(f"Invalid severity: '{severity}'")

        if not triage.get("reasoning"):
            issues.append("Missing reasoning in triage report")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "classification": classification,
            "injection_detected": triage.get("injection_detected", False),
            "triage_result": triage,
            "verified_components": action_result.get("verified_components", []),
            "reproduction": action_result.get("reproduction", {}),
        }

    async def reflect(self, validation: dict[str, Any]) -> PhaseResult:
        """Decide next step: proceed to implement, escalate, or retry."""
        self.logger.info("Reflecting: deciding triage outcome")

        triage = validation.get("triage_result", {})
        classification = validation.get("classification", "")

        if validation.get("injection_detected", False):
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=False,
                escalate=True,
                escalation_reason="Prompt injection detected in issue content",
                findings=triage,
            )

        if classification == "feature":
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=False,
                escalate=True,
                escalation_reason=(
                    f"Issue classified as 'feature' — requires human review. "
                    f"Reasoning: {triage.get('reasoning', 'none provided')}"
                ),
                findings=triage,
            )

        if classification == "ambiguous":
            confidence = triage.get("confidence", 0)
            if isinstance(confidence, (int, float)) and confidence >= 0.4:
                self.logger.warn(
                    f"Ambiguous classification with confidence {confidence} — proceeding as bug"
                )
                return PhaseResult(
                    phase=self.name,
                    success=True,
                    should_continue=True,
                    next_phase="implement",
                    findings=triage,
                    artifacts={
                        "classification": "ambiguous_as_bug",
                        "confidence": confidence,
                        "verified_components": validation.get("verified_components", []),
                    },
                )
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=False,
                escalate=True,
                escalation_reason=(
                    f"Issue classified as 'ambiguous' with low confidence — requires human review. "
                    f"Reasoning: {triage.get('reasoning', 'none provided')}"
                ),
                findings=triage,
            )

        if not validation.get("valid", False):
            self.logger.warn(f"Triage validation issues: {validation.get('issues', [])}")
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=True,
                findings={"validation_issues": validation["issues"], "triage": triage},
            )

        if triage.get("recommendation") == "escalate":
            return PhaseResult(
                phase=self.name,
                success=False,
                should_continue=False,
                escalate=True,
                escalation_reason=(
                    f"Triage recommends escalation: {triage.get('reasoning', 'no reason given')}"
                ),
                findings=triage,
            )

        return PhaseResult(
            phase=self.name,
            success=True,
            should_continue=True,
            next_phase="implement",
            findings=triage,
            artifacts={
                "triage_report": triage,
                "verified_components": validation.get("verified_components", []),
                "reproduction": validation.get("reproduction", {}),
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _verify_components(
        self,
        components: list[str],
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Check whether reported affected components exist in the repo."""
        verified: list[dict[str, Any]] = []
        if not self.tool_executor or not components:
            return verified

        for component in components[:10]:
            result = await self.tool_executor.execute("file_read", path=component)
            found = result.get("success", False)
            verified.append({"path": component, "found": found})
            actions.append({"action": "verify_component", "component": component, "found": found})

        return verified

    async def _attempt_reproduction(
        self,
        plan: dict[str, Any],
        triage: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Run existing tests to attempt reproduction if configured."""
        if not self.config.phases.triage.attempt_reproduction or not self.tool_executor:
            return {"attempted": False}

        test_files_raw = plan.get("observation", {}).get("test_files", "")
        test_files = [f.strip() for f in test_files_raw.strip().splitlines() if f.strip()]
        existing_tests = triage.get("reproduction", {}).get("existing_tests", [])
        targets = existing_tests[:3] if existing_tests else test_files[:3]

        if not targets:
            return {"attempted": False, "reason": "No test files found"}

        result = await self.tool_executor.execute(
            "shell_run",
            command=(
                "python -m pytest --tb=short -q 2>&1 "
                "|| go test ./... 2>&1 "
                "|| echo 'No test runner detected'"
            ),
            timeout=120,
        )
        reproduction = {
            "attempted": True,
            "tests_targeted": targets,
            "test_output": result.get("stdout", "")[:2000],
            "test_success": result.get("success", False),
        }
        actions.append({"action": "run_tests", "success": result.get("success", False)})
        return reproduction


def parse_triage_response(content: str) -> dict[str, Any]:
    """Extract structured triage JSON from an LLM response.

    Tries direct JSON parse, then ``json`` code-block extraction, then
    generic code-block extraction. Returns a default escalate-result on
    parse failure.
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
        "classification": "ambiguous",
        "confidence": 0.0,
        "severity": "medium",
        "affected_components": [],
        "reproduction": {
            "existing_tests": [],
            "can_reproduce": False,
            "reproduction_steps": "",
        },
        "injection_detected": False,
        "recommendation": "escalate",
        "reasoning": f"Failed to parse LLM triage response. Raw: {content[:500]}",
    }
