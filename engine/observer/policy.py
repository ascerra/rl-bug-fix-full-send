"""Policy evaluator — evaluates attestations against configurable rules.

Loads a YAML policy definition and checks a signed attestation against
each enabled rule.  Produces a ``PolicyResult`` with per-rule pass/fail,
a list of violations, and optional warnings.

Built-in rules (see SPEC.md §5.6 and IMPLEMENTATION-PLAN.md §8.4):

- **model_allowlist** — model IDs in the attestation must be on the
  configured allowlist.
- **prompt_integrity** — prompt template digests in the attestation must
  match known-good digests from the policy (when configured).
- **scope_compliance** — files modified by the agent are related to the
  issue (heuristic check against triage / issue references).
- **cross_checks_passed** — all cross-checks must have ``passed: true``.
- **iteration_limits** — iteration count did not exceed the configured
  maximum.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from engine.observer.signer import SignedAttestation


@dataclass
class RuleResult:
    """Outcome of evaluating a single policy rule."""

    rule_name: str = ""
    passed: bool = False
    details: str = ""
    severity: str = "violation"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_name": self.rule_name,
            "passed": self.passed,
            "details": self.details,
            "severity": self.severity,
        }


@dataclass
class PolicyResult:
    """Aggregated policy evaluation result."""

    passed: bool = False
    rule_results: list[RuleResult] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "rule_results": [r.to_dict() for r in self.rule_results],
            "violations": list(self.violations),
            "warnings": list(self.warnings),
        }


def load_policy(policy_file: str | Path) -> dict[str, Any]:
    """Read a YAML policy file and return the parsed policy dict.

    Returns an empty ``policy`` structure if the file does not exist or
    is empty.
    """
    path = Path(policy_file)
    if not path.exists():
        return {"policy": {"version": "1", "rules": {}}}
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return raw if "policy" in raw else {"policy": {"version": "1", "rules": raw}}


class PolicyEvaluator:
    """Evaluates a signed attestation against a set of policy rules.

    Usage::

        policy = load_policy("templates/policies/default.yaml")
        evaluator = PolicyEvaluator()
        result = evaluator.evaluate(signed_attestation, policy)
    """

    def evaluate(
        self,
        signed_attestation: SignedAttestation,
        policy: dict[str, Any],
        *,
        triage_components: list[str] | None = None,
        issue_body: str = "",
    ) -> PolicyResult:
        """Evaluate *signed_attestation* against *policy* rules.

        Args:
            signed_attestation: The attestation (signed or unsigned) to check.
            policy: Parsed policy dict (from :func:`load_policy`).
            triage_components: File paths mentioned in triage output, used
                by the ``scope_compliance`` rule.
            issue_body: Raw issue text, used by ``scope_compliance`` for
                keyword matching.

        Returns:
            A :class:`PolicyResult` with per-rule outcomes.
        """
        attestation = _parse_attestation(signed_attestation)
        rules_cfg = policy.get("policy", {}).get("rules", {})
        result = PolicyResult()

        rule_runners = [
            ("model_allowlist", self._check_model_allowlist),
            ("prompt_integrity", self._check_prompt_integrity),
            ("scope_compliance", self._check_scope_compliance),
            ("cross_checks", self._check_cross_checks),
            ("iteration_limits", self._check_iteration_limits),
        ]

        for rule_name, runner in rule_runners:
            rule_cfg = rules_cfg.get(rule_name, {})
            if not rule_cfg.get("enabled", False):
                continue
            rr = runner(
                attestation,
                rule_cfg,
                triage_components=triage_components or [],
                issue_body=issue_body,
            )
            result.rule_results.append(rr)
            if rr.severity == "warning":
                result.warnings.append(f"{rr.rule_name}: {rr.details}")
            elif not rr.passed:
                result.violations.append(f"{rr.rule_name}: {rr.details}")

        result.passed = len(result.violations) == 0
        return result

    # ------------------------------------------------------------------
    # Rule implementations
    # ------------------------------------------------------------------

    def _check_model_allowlist(
        self,
        attestation: dict[str, Any],
        rule_cfg: dict[str, Any],
        **_: Any,
    ) -> RuleResult:
        """Model IDs in the attestation must be in the configured allowlist."""
        allowlist = rule_cfg.get("models", [])
        if not allowlist:
            return RuleResult(
                rule_name="model_allowlist",
                passed=True,
                details="No model allowlist configured — skipping",
                severity="warning",
            )

        models = _extract_models(attestation)
        if not models:
            return RuleResult(
                rule_name="model_allowlist",
                passed=True,
                details="No models recorded in attestation",
            )

        disallowed = [m for m in models if m not in allowlist]
        if disallowed:
            return RuleResult(
                rule_name="model_allowlist",
                passed=False,
                details=f"Disallowed models: {', '.join(sorted(disallowed))}",
            )
        return RuleResult(
            rule_name="model_allowlist",
            passed=True,
            details=f"All {len(models)} model(s) on allowlist",
        )

    def _check_prompt_integrity(
        self,
        attestation: dict[str, Any],
        rule_cfg: dict[str, Any],
        **_: Any,
    ) -> RuleResult:
        """Prompt template digests must match known-good digests."""
        known_digests: dict[str, str] = rule_cfg.get("known_digests", {})
        if not known_digests:
            return RuleResult(
                rule_name="prompt_integrity",
                passed=True,
                details="No known-good digests configured — skipping",
                severity="warning",
            )

        resolved_deps = _extract_resolved_deps(attestation)
        prompt_deps = {
            d["uri"]: d.get("digest", {}).get("sha256", "")
            for d in resolved_deps
            if d.get("uri", "").startswith("prompt://")
        }

        mismatches: list[str] = []
        for uri, expected_digest in known_digests.items():
            actual = prompt_deps.get(uri, "")
            if not actual:
                mismatches.append(f"{uri}: not found in attestation")
            elif actual != expected_digest:
                mismatches.append(f"{uri}: digest mismatch")

        if mismatches:
            return RuleResult(
                rule_name="prompt_integrity",
                passed=False,
                details=f"Prompt integrity failures: {'; '.join(mismatches)}",
            )
        return RuleResult(
            rule_name="prompt_integrity",
            passed=True,
            details=f"All {len(known_digests)} prompt template digest(s) match",
        )

    def _check_scope_compliance(
        self,
        attestation: dict[str, Any],
        rule_cfg: dict[str, Any],
        *,
        triage_components: list[str] | None = None,
        issue_body: str = "",
        **_: Any,
    ) -> RuleResult:
        """Files modified by the agent must be related to the issue.

        Heuristic: files mentioned in triage ``affected_components`` or
        referenced in the issue body are considered related.  Files not
        matching either are flagged as unrelated.  The rule has a
        configurable threshold (``max_unrelated_files``, default 0).
        """
        max_unrelated = rule_cfg.get("max_unrelated_files", 0)
        triage = triage_components or []

        cross_checks = _extract_cross_check_results(attestation)
        diff_evidence = cross_checks.get("diff_consistency", {})
        git_files: list[str] = diff_evidence.get("git_files", [])

        if not git_files:
            resolved_deps = _extract_resolved_deps(attestation)
            for dep in resolved_deps:
                uri = dep.get("uri", "")
                if uri and not uri.startswith("prompt://") and not uri.startswith("git+"):
                    git_files.append(uri)

        if not git_files:
            return RuleResult(
                rule_name="scope_compliance",
                passed=True,
                details="No files modified — nothing to check",
            )

        related_names = set()
        for comp in triage:
            related_names.add(comp)
            if "/" in comp:
                related_names.add(comp.rsplit("/", 1)[-1])

        issue_lower = issue_body.lower()

        unrelated: list[str] = []
        for f in git_files:
            basename = f.rsplit("/", 1)[-1] if "/" in f else f
            if f in related_names or basename in related_names:
                continue
            if issue_lower and (f.lower() in issue_lower or basename.lower() in issue_lower):
                continue
            unrelated.append(f)

        if len(unrelated) > max_unrelated:
            return RuleResult(
                rule_name="scope_compliance",
                passed=False,
                details=(
                    f"{len(unrelated)} unrelated file(s) modified "
                    f"(max {max_unrelated}): {', '.join(sorted(unrelated)[:5])}"
                ),
            )
        return RuleResult(
            rule_name="scope_compliance",
            passed=True,
            details=f"All {len(git_files)} file(s) within scope",
        )

    def _check_cross_checks(
        self,
        attestation: dict[str, Any],
        rule_cfg: dict[str, Any],
        **_: Any,
    ) -> RuleResult:
        """All cross-checks in the attestation must have ``passed: true``."""
        required = rule_cfg.get(
            "required_checks",
            ["diff_consistency", "action_completeness", "phase_ordering"],
        )

        cross_results = _extract_cross_check_results(attestation)
        if not cross_results:
            return RuleResult(
                rule_name="cross_checks",
                passed=True,
                details="No cross-check results in attestation — skipping",
                severity="warning",
            )

        failures: list[str] = []
        for check_name in required:
            check_data = cross_results.get(check_name)
            if check_data is None:
                failures.append(f"{check_name}: missing from attestation")
            elif not check_data.get("passed", False):
                detail = check_data.get("details", "failed")
                failures.append(f"{check_name}: {detail}")

        if failures:
            return RuleResult(
                rule_name="cross_checks",
                passed=False,
                details=f"Cross-check failures: {'; '.join(failures)}",
            )
        return RuleResult(
            rule_name="cross_checks",
            passed=True,
            details=f"All {len(required)} required cross-check(s) passed",
        )

    def _check_iteration_limits(
        self,
        attestation: dict[str, Any],
        rule_cfg: dict[str, Any],
        **_: Any,
    ) -> RuleResult:
        """Iteration count must not exceed the configured maximum."""
        max_iterations = rule_cfg.get("max_iterations", 10)

        meta = attestation.get("predicate", {}).get("runDetails", {}).get("metadata", {})
        iteration_count = meta.get("iteration_count", 0)

        if not iteration_count:
            build_def = attestation.get("predicate", {}).get("buildDefinition", {})
            internal = build_def.get("internalParameters", {})
            iteration_count = internal.get("iteration_count", 0)

        if iteration_count > max_iterations:
            return RuleResult(
                rule_name="iteration_limits",
                passed=False,
                details=(f"Iteration count ({iteration_count}) exceeds maximum ({max_iterations})"),
            )
        return RuleResult(
            rule_name="iteration_limits",
            passed=True,
            details=f"Iteration count ({iteration_count}) within limit ({max_iterations})",
        )

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_pr_comment(policy_result: PolicyResult) -> str:
        """Produce a markdown summary suitable for a PR comment."""
        status = "PASSED" if policy_result.passed else "FAILED"
        icon = "\u2705" if policy_result.passed else "\u274c"

        lines = [
            f"## {icon} Observer Policy: {status}",
            "",
        ]

        for rr in policy_result.rule_results:
            rule_icon = "\u2705" if rr.passed else "\u274c"
            lines.append(f"- {rule_icon} **{rr.rule_name}**: {rr.details}")

        if policy_result.violations:
            lines.append("")
            lines.append("### Violations")
            for v in policy_result.violations:
                lines.append(f"- {v}")

        if policy_result.warnings:
            lines.append("")
            lines.append("### Warnings")
            for w in policy_result.warnings:
                lines.append(f"- {w}")

        lines.append("")
        lines.append(
            "*Generated by the neutral observer — the agent cannot modify this assessment.*"
        )
        return "\n".join(lines)

    @staticmethod
    def format_summary(policy_result: PolicyResult) -> str:
        """Produce a concise text summary for ``$GITHUB_STEP_SUMMARY``."""
        status = "PASSED" if policy_result.passed else "FAILED"
        total = len(policy_result.rule_results)
        passed = sum(1 for r in policy_result.rule_results if r.passed)
        parts = [f"Observer policy: {status} ({passed}/{total} rules passed)"]

        if policy_result.violations:
            parts.append(f"Violations: {'; '.join(policy_result.violations)}")
        if policy_result.warnings:
            parts.append(f"Warnings: {'; '.join(policy_result.warnings)}")

        return " | ".join(parts)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _parse_attestation(signed: SignedAttestation) -> dict[str, Any]:
    """Extract the attestation dict from a SignedAttestation."""
    if not signed.payload:
        return {}
    try:
        return json.loads(signed.payload)
    except (json.JSONDecodeError, TypeError):
        return {}


def _extract_models(attestation: dict[str, Any]) -> list[str]:
    """Return model IDs from the attestation's runDetails.models."""
    models_list = attestation.get("predicate", {}).get("runDetails", {}).get("models", [])
    return [m.get("id", "") for m in models_list if m.get("id")]


def _extract_resolved_deps(attestation: dict[str, Any]) -> list[dict[str, Any]]:
    """Return resolvedDependencies from the attestation."""
    return (
        attestation.get("predicate", {}).get("buildDefinition", {}).get("resolvedDependencies", [])
    )


def _extract_cross_check_results(attestation: dict[str, Any]) -> dict[str, Any]:
    """Return crossCheckResults from the attestation."""
    return attestation.get("predicate", {}).get("runDetails", {}).get("crossCheckResults", {})
