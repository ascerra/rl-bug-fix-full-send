"""Tests for the observer policy evaluator (Phase 8.4).

Covers:
- RuleResult / PolicyResult dataclasses: to_dict, serialization
- load_policy: from file, missing file, empty file, malformed
- PolicyEvaluator.evaluate: full pipeline, disabled rules
- model_allowlist rule: pass, fail, empty allowlist, no models
- prompt_integrity rule: pass, fail, missing digest, no config
- scope_compliance rule: pass, fail, with triage, with issue body, no files
- cross_checks rule: pass, fail, missing check, no results
- iteration_limits rule: pass, fail, default max, in internalParameters
- format_pr_comment: pass/fail output, violations, warnings
- format_summary: pass/fail output
- ObserverConfig: defaults, YAML loading, wired into EngineConfig
- Integration: AttestationBuilder → serialize → sign_none → evaluate round-trip
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from engine.config import EngineConfig, ObserverConfig, load_config
from engine.observer.attestation import AttestationBuilder
from engine.observer.policy import (
    PolicyEvaluator,
    PolicyResult,
    RuleResult,
    _extract_cross_check_results,
    _extract_models,
    _extract_resolved_deps,
    _parse_attestation,
    load_policy,
)
from engine.observer.signer import AttestationSigner, SignedAttestation

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _build_sample_attestation(**overrides: Any) -> dict[str, Any]:
    """Build a minimal valid attestation dict for testing."""
    att: dict[str, Any] = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "git+https://github.com/org/repo", "digest": {"sha1": "abc123"}}],
        "predicateType": "https://rl-engine.dev/provenance/agent/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://rl-engine.dev/AgentSynthesis/v1",
                "externalParameters": {"issue_url": "https://github.com/org/repo/issues/1"},
                "internalParameters": {"engine_version": "dev", "iteration_count": 3},
                "resolvedDependencies": [
                    {"uri": "prompt://triage.md", "digest": {"sha256": "sha256:aaa"}},
                    {"uri": "prompt://implement.md", "digest": {"sha256": "sha256:bbb"}},
                ],
            },
            "runDetails": {
                "builder": {"id": "workflow"},
                "metadata": {"invocationId": "", "startedOn": "", "finishedOn": ""},
                "models": [
                    {"id": "gemini-2.5-pro", "provider": "google", "total_calls": 5},
                ],
                "toolDefinitions": {"digest": "sha256:tools", "tools": ["file_read"]},
                "crossCheckResults": {
                    "diff_consistency": {"passed": True},
                    "action_completeness": {"passed": True},
                    "phase_ordering": {"passed": True},
                    "token_plausibility": {"passed": True},
                    "tool_call_integrity": {"passed": True},
                },
            },
        },
    }
    for key, val in overrides.items():
        if key == "models":
            att["predicate"]["runDetails"]["models"] = val
        elif key == "cross_check_results":
            att["predicate"]["runDetails"]["crossCheckResults"] = val
        elif key == "resolved_deps":
            att["predicate"]["buildDefinition"]["resolvedDependencies"] = val
        elif key == "iteration_count":
            att["predicate"]["buildDefinition"]["internalParameters"]["iteration_count"] = val
    return att


def _make_signed_from_dict(att: dict[str, Any]) -> SignedAttestation:
    """Wrap an attestation dict in a SignedAttestation."""
    payload = json.dumps(att, sort_keys=True, separators=(",", ":"))
    return SignedAttestation(payload=payload, signing_method="none", signed=False)


def _all_rules_policy(**overrides: Any) -> dict[str, Any]:
    """Return a policy with all rules enabled."""
    policy: dict[str, Any] = {
        "policy": {
            "version": "1",
            "rules": {
                "model_allowlist": {
                    "enabled": True,
                    "models": ["gemini-2.5-pro", "claude-sonnet-4-20250514"],
                },
                "prompt_integrity": {"enabled": False},
                "scope_compliance": {"enabled": True, "max_unrelated_files": 0},
                "cross_checks": {
                    "enabled": True,
                    "required_checks": [
                        "diff_consistency",
                        "action_completeness",
                        "phase_ordering",
                    ],
                },
                "iteration_limits": {"enabled": True, "max_iterations": 10},
            },
        }
    }
    for key, val in overrides.items():
        policy["policy"]["rules"][key] = val
    return policy


# ===========================================================================
# Dataclass tests
# ===========================================================================


class TestRuleResult:
    def test_to_dict(self) -> None:
        rr = RuleResult(rule_name="test", passed=True, details="ok", severity="violation")
        d = rr.to_dict()
        assert d["rule_name"] == "test"
        assert d["passed"] is True
        assert d["details"] == "ok"
        assert d["severity"] == "violation"

    def test_defaults(self) -> None:
        rr = RuleResult()
        assert rr.rule_name == ""
        assert rr.passed is False
        assert rr.severity == "violation"


class TestPolicyResult:
    def test_to_dict(self) -> None:
        pr = PolicyResult(
            passed=True,
            rule_results=[RuleResult(rule_name="a", passed=True, details="ok")],
            violations=[],
            warnings=["w1"],
        )
        d = pr.to_dict()
        assert d["passed"] is True
        assert len(d["rule_results"]) == 1
        assert d["warnings"] == ["w1"]
        assert d["violations"] == []

    def test_defaults(self) -> None:
        pr = PolicyResult()
        assert pr.passed is False
        assert pr.rule_results == []


# ===========================================================================
# load_policy tests
# ===========================================================================


class TestLoadPolicy:
    def test_load_from_file(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "policy.yaml"
        policy_file.write_text(
            textwrap.dedent("""\
            policy:
              version: "1"
              rules:
                model_allowlist:
                  enabled: true
                  models: ["gemini-2.5-pro"]
        """)
        )
        policy = load_policy(policy_file)
        assert policy["policy"]["version"] == "1"
        rules = policy["policy"]["rules"]
        assert rules["model_allowlist"]["enabled"] is True
        assert "gemini-2.5-pro" in rules["model_allowlist"]["models"]

    def test_missing_file(self, tmp_path: Path) -> None:
        policy = load_policy(tmp_path / "nonexistent.yaml")
        assert "policy" in policy
        assert policy["policy"]["rules"] == {}

    def test_empty_file(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "empty.yaml"
        policy_file.write_text("")
        policy = load_policy(policy_file)
        assert "policy" in policy

    def test_no_policy_key(self, tmp_path: Path) -> None:
        policy_file = tmp_path / "bare.yaml"
        policy_file.write_text("model_allowlist:\n  enabled: true\n")
        policy = load_policy(policy_file)
        assert "policy" in policy
        assert policy["policy"]["rules"]["model_allowlist"]["enabled"] is True

    def test_default_policy_file(self) -> None:
        policy = load_policy("templates/policies/default.yaml")
        assert policy["policy"]["version"] == "1"
        rules = policy["policy"]["rules"]
        assert rules["model_allowlist"]["enabled"] is True
        assert rules["cross_checks"]["enabled"] is True
        assert rules["iteration_limits"]["enabled"] is True


# ===========================================================================
# Helper function tests
# ===========================================================================


class TestHelpers:
    def test_parse_attestation_valid(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        parsed = _parse_attestation(signed)
        assert parsed["_type"] == "https://in-toto.io/Statement/v1"

    def test_parse_attestation_empty_payload(self) -> None:
        signed = SignedAttestation(payload="")
        assert _parse_attestation(signed) == {}

    def test_parse_attestation_invalid_json(self) -> None:
        signed = SignedAttestation(payload="not json")
        assert _parse_attestation(signed) == {}

    def test_extract_models(self) -> None:
        att = _build_sample_attestation()
        models = _extract_models(att)
        assert models == ["gemini-2.5-pro"]

    def test_extract_models_empty(self) -> None:
        att = _build_sample_attestation(models=[])
        assert _extract_models(att) == []

    def test_extract_resolved_deps(self) -> None:
        att = _build_sample_attestation()
        deps = _extract_resolved_deps(att)
        assert len(deps) == 2
        assert deps[0]["uri"] == "prompt://triage.md"

    def test_extract_cross_check_results(self) -> None:
        att = _build_sample_attestation()
        ccr = _extract_cross_check_results(att)
        assert ccr["diff_consistency"]["passed"] is True


# ===========================================================================
# Model allowlist rule
# ===========================================================================


class TestModelAllowlist:
    def test_pass(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        model_rule = next(r for r in result.rule_results if r.rule_name == "model_allowlist")
        assert model_rule.passed is True

    def test_fail_disallowed(self) -> None:
        att = _build_sample_attestation(
            models=[{"id": "gpt-4o", "provider": "openai", "total_calls": 1}]
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        model_rule = next(r for r in result.rule_results if r.rule_name == "model_allowlist")
        assert model_rule.passed is False
        assert "gpt-4o" in model_rule.details

    def test_empty_allowlist_warning(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(model_allowlist={"enabled": True, "models": []})
        result = PolicyEvaluator().evaluate(signed, policy)
        model_rule = next(r for r in result.rule_results if r.rule_name == "model_allowlist")
        assert model_rule.passed is True
        assert model_rule.severity == "warning"

    def test_no_models_in_attestation(self) -> None:
        att = _build_sample_attestation(models=[])
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        model_rule = next(r for r in result.rule_results if r.rule_name == "model_allowlist")
        assert model_rule.passed is True

    def test_multiple_models_one_disallowed(self) -> None:
        att = _build_sample_attestation(
            models=[
                {"id": "gemini-2.5-pro", "provider": "google", "total_calls": 3},
                {"id": "bad-model", "provider": "unknown", "total_calls": 1},
            ]
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        model_rule = next(r for r in result.rule_results if r.rule_name == "model_allowlist")
        assert model_rule.passed is False
        assert "bad-model" in model_rule.details


# ===========================================================================
# Prompt integrity rule
# ===========================================================================


class TestPromptIntegrity:
    def test_pass(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(
            prompt_integrity={
                "enabled": True,
                "known_digests": {
                    "prompt://triage.md": "sha256:aaa",
                    "prompt://implement.md": "sha256:bbb",
                },
            }
        )
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "prompt_integrity")
        assert rule.passed is True

    def test_fail_mismatch(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(
            prompt_integrity={
                "enabled": True,
                "known_digests": {"prompt://triage.md": "sha256:WRONG"},
            }
        )
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "prompt_integrity")
        assert rule.passed is False
        assert "mismatch" in rule.details

    def test_fail_missing_prompt(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(
            prompt_integrity={
                "enabled": True,
                "known_digests": {"prompt://review.md": "sha256:ccc"},
            }
        )
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "prompt_integrity")
        assert rule.passed is False
        assert "not found" in rule.details

    def test_no_known_digests_warning(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(prompt_integrity={"enabled": True, "known_digests": {}})
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "prompt_integrity")
        assert rule.passed is True
        assert rule.severity == "warning"

    def test_disabled_by_default(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        names = [r.rule_name for r in result.rule_results]
        assert "prompt_integrity" not in names


# ===========================================================================
# Scope compliance rule
# ===========================================================================


class TestScopeCompliance:
    def test_pass_with_triage_components(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {
                    "passed": True,
                    "git_files": ["pkg/reconciler.go"],
                },
                "action_completeness": {"passed": True},
                "phase_ordering": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy, triage_components=["pkg/reconciler.go"])
        rule = next(r for r in result.rule_results if r.rule_name == "scope_compliance")
        assert rule.passed is True

    def test_fail_unrelated_files(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {
                    "passed": True,
                    "git_files": ["pkg/reconciler.go", "totally/unrelated.txt"],
                },
                "action_completeness": {"passed": True},
                "phase_ordering": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy, triage_components=["pkg/reconciler.go"])
        rule = next(r for r in result.rule_results if r.rule_name == "scope_compliance")
        assert rule.passed is False
        assert "unrelated" in rule.details.lower()

    def test_pass_with_issue_body_reference(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {
                    "passed": True,
                    "git_files": ["pkg/reconciler.go"],
                },
                "action_completeness": {"passed": True},
                "phase_ordering": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(
            signed,
            policy,
            issue_body="The bug is in pkg/reconciler.go on line 42",
        )
        rule = next(r for r in result.rule_results if r.rule_name == "scope_compliance")
        assert rule.passed is True

    def test_pass_basename_match(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {
                    "passed": True,
                    "git_files": ["a/b/controller.go"],
                },
                "action_completeness": {"passed": True},
                "phase_ordering": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy, triage_components=["controller.go"])
        rule = next(r for r in result.rule_results if r.rule_name == "scope_compliance")
        assert rule.passed is True

    def test_no_files_modified(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {"passed": True, "git_files": []},
                "action_completeness": {"passed": True},
                "phase_ordering": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "scope_compliance")
        assert rule.passed is True

    def test_max_unrelated_threshold(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {
                    "passed": True,
                    "git_files": ["main.go", "unrelated.txt"],
                },
                "action_completeness": {"passed": True},
                "phase_ordering": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(scope_compliance={"enabled": True, "max_unrelated_files": 5})
        result = PolicyEvaluator().evaluate(signed, policy, triage_components=["main.go"])
        rule = next(r for r in result.rule_results if r.rule_name == "scope_compliance")
        assert rule.passed is True


# ===========================================================================
# Cross-checks rule
# ===========================================================================


class TestCrossChecks:
    def test_pass(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "cross_checks")
        assert rule.passed is True

    def test_fail_check_failed(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {"passed": False, "details": "mismatch found"},
                "action_completeness": {"passed": True},
                "phase_ordering": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "cross_checks")
        assert rule.passed is False
        assert "diff_consistency" in rule.details

    def test_fail_missing_check(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {"passed": True},
                "action_completeness": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "cross_checks")
        assert rule.passed is False
        assert "phase_ordering" in rule.details
        assert "missing" in rule.details

    def test_no_results_warning(self) -> None:
        att = _build_sample_attestation(cross_check_results={})
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "cross_checks")
        assert rule.passed is True
        assert rule.severity == "warning"

    def test_custom_required_checks(self) -> None:
        att = _build_sample_attestation(
            cross_check_results={
                "diff_consistency": {"passed": True},
                "token_plausibility": {"passed": True},
            }
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(
            cross_checks={
                "enabled": True,
                "required_checks": ["diff_consistency", "token_plausibility"],
            }
        )
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "cross_checks")
        assert rule.passed is True


# ===========================================================================
# Iteration limits rule
# ===========================================================================


class TestIterationLimits:
    def test_pass(self) -> None:
        att = _build_sample_attestation(iteration_count=3)
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "iteration_limits")
        assert rule.passed is True
        assert "3" in rule.details

    def test_fail_exceeds_max(self) -> None:
        att = _build_sample_attestation(iteration_count=15)
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "iteration_limits")
        assert rule.passed is False
        assert "15" in rule.details
        assert "10" in rule.details

    def test_custom_max(self) -> None:
        att = _build_sample_attestation(iteration_count=6)
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(iteration_limits={"enabled": True, "max_iterations": 5})
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "iteration_limits")
        assert rule.passed is False

    def test_zero_iterations(self) -> None:
        att = _build_sample_attestation(iteration_count=0)
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        rule = next(r for r in result.rule_results if r.rule_name == "iteration_limits")
        assert rule.passed is True


# ===========================================================================
# Full evaluate pipeline
# ===========================================================================


class TestEvaluate:
    def test_all_pass(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        assert result.passed is True
        assert result.violations == []

    def test_multiple_failures(self) -> None:
        att = _build_sample_attestation(
            models=[{"id": "gpt-4o", "provider": "openai", "total_calls": 1}],
            iteration_count=20,
        )
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        assert result.passed is False
        assert len(result.violations) >= 2

    def test_disabled_rules_skipped(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = {
            "policy": {
                "version": "1",
                "rules": {
                    "model_allowlist": {"enabled": False},
                    "cross_checks": {"enabled": False},
                    "iteration_limits": {"enabled": False},
                    "scope_compliance": {"enabled": False},
                },
            }
        }
        result = PolicyEvaluator().evaluate(signed, policy)
        assert result.passed is True
        assert len(result.rule_results) == 0

    def test_empty_payload(self) -> None:
        signed = SignedAttestation(payload="")
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        assert result.passed is True

    def test_warnings_dont_fail(self) -> None:
        att = _build_sample_attestation(cross_check_results={})
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy(model_allowlist={"enabled": True, "models": []})
        result = PolicyEvaluator().evaluate(signed, policy)
        assert result.passed is True
        assert len(result.warnings) >= 1


# ===========================================================================
# Formatting tests
# ===========================================================================


class TestFormatPrComment:
    def test_passed(self) -> None:
        pr = PolicyResult(
            passed=True,
            rule_results=[RuleResult(rule_name="model_allowlist", passed=True, details="ok")],
        )
        comment = PolicyEvaluator.format_pr_comment(pr)
        assert "PASSED" in comment
        assert "\u2705" in comment
        assert "model_allowlist" in comment
        assert "neutral observer" in comment.lower()

    def test_failed_with_violations(self) -> None:
        pr = PolicyResult(
            passed=False,
            rule_results=[
                RuleResult(rule_name="model_allowlist", passed=False, details="bad model"),
            ],
            violations=["model_allowlist: bad model"],
        )
        comment = PolicyEvaluator.format_pr_comment(pr)
        assert "FAILED" in comment
        assert "\u274c" in comment
        assert "Violations" in comment
        assert "bad model" in comment

    def test_with_warnings(self) -> None:
        pr = PolicyResult(
            passed=True,
            rule_results=[
                RuleResult(rule_name="cross_checks", passed=True, details="skipped"),
            ],
            warnings=["cross_checks: no results"],
        )
        comment = PolicyEvaluator.format_pr_comment(pr)
        assert "Warnings" in comment
        assert "no results" in comment


class TestFormatSummary:
    def test_passed(self) -> None:
        pr = PolicyResult(
            passed=True,
            rule_results=[
                RuleResult(rule_name="a", passed=True, details="ok"),
                RuleResult(rule_name="b", passed=True, details="ok"),
            ],
        )
        summary = PolicyEvaluator.format_summary(pr)
        assert "PASSED" in summary
        assert "2/2" in summary

    def test_failed(self) -> None:
        pr = PolicyResult(
            passed=False,
            rule_results=[
                RuleResult(rule_name="a", passed=True, details="ok"),
                RuleResult(rule_name="b", passed=False, details="bad"),
            ],
            violations=["b: bad"],
        )
        summary = PolicyEvaluator.format_summary(pr)
        assert "FAILED" in summary
        assert "1/2" in summary
        assert "Violations" in summary


# ===========================================================================
# ObserverConfig tests
# ===========================================================================


class TestObserverConfig:
    def test_defaults(self) -> None:
        cfg = ObserverConfig()
        assert cfg.enabled is True
        assert cfg.signing_method == "sigstore"
        assert cfg.policy_file == "templates/policies/default.yaml"
        assert cfg.fail_on_policy_violation is False
        assert "diff_consistency" in cfg.cross_checks
        assert cfg.model_allowlist == []

    def test_engine_config_has_observer(self) -> None:
        cfg = EngineConfig()
        assert isinstance(cfg.observer, ObserverConfig)

    def test_yaml_loading(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            observer:
              enabled: false
              signing_method: none
              policy_file: custom/policy.yaml
              model_allowlist:
                - gemini-2.5-pro
              fail_on_policy_violation: true
        """)
        )
        cfg = load_config(str(config_file))
        assert cfg.observer.enabled is False
        assert cfg.observer.signing_method == "none"
        assert cfg.observer.policy_file == "custom/policy.yaml"
        assert cfg.observer.model_allowlist == ["gemini-2.5-pro"]
        assert cfg.observer.fail_on_policy_violation is True

    def test_yaml_preserves_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("observer:\n  enabled: true\n")
        cfg = load_config(str(config_file))
        assert cfg.observer.signing_method == "sigstore"
        assert cfg.observer.policy_file == "templates/policies/default.yaml"


# ===========================================================================
# Integration round-trip
# ===========================================================================


class TestIntegrationRoundTrip:
    def test_build_sign_evaluate(self) -> None:
        """AttestationBuilder → serialize → sign_none → evaluate."""
        from engine.observer import CrossCheckReport, CrossCheckResult, ModelInfo

        report = CrossCheckReport()
        report.add(CrossCheckResult(check_name="diff_consistency", passed=True, details="ok"))
        report.add(CrossCheckResult(check_name="action_completeness", passed=True, details="ok"))
        report.add(CrossCheckResult(check_name="phase_ordering", passed=True, details="ok"))

        builder = AttestationBuilder()
        att = builder.build(
            timeline=[],
            cross_check_report=report,
            execution_metadata={"target": {"repo": "org/repo", "ref": "abc123"}},
            model_info=[ModelInfo(model="gemini-2.5-pro", provider="google", total_calls=3)],
        )
        canonical = AttestationBuilder.serialize(att)

        signer = AttestationSigner()
        signed = signer.sign_none(canonical)

        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        assert result.passed is True
        assert len(result.violations) == 0

    def test_build_sign_evaluate_fails(self) -> None:
        """Pipeline where model is not on allowlist → policy fails."""
        from engine.observer import CrossCheckReport, CrossCheckResult, ModelInfo

        report = CrossCheckReport()
        report.add(CrossCheckResult(check_name="diff_consistency", passed=True))
        report.add(CrossCheckResult(check_name="action_completeness", passed=True))
        report.add(CrossCheckResult(check_name="phase_ordering", passed=True))

        builder = AttestationBuilder()
        att = builder.build(
            timeline=[],
            cross_check_report=report,
            model_info=[ModelInfo(model="gpt-4o-mini", provider="openai", total_calls=1)],
        )
        canonical = AttestationBuilder.serialize(att)

        signer = AttestationSigner()
        signed = signer.sign_none(canonical)

        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        assert result.passed is False
        assert any("gpt-4o-mini" in v for v in result.violations)

    def test_policy_result_serialization(self) -> None:
        att = _build_sample_attestation()
        signed = _make_signed_from_dict(att)
        policy = _all_rules_policy()
        result = PolicyEvaluator().evaluate(signed, policy)
        d = result.to_dict()
        roundtripped = json.loads(json.dumps(d))
        assert roundtripped["passed"] is True
        assert isinstance(roundtripped["rule_results"], list)
