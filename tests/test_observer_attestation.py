"""Tests for the observer attestation builder (Phase 8.2).

Covers:
- AttestationBuilder.build() — full attestation from fixture data
- AttestationBuilder.serialize() — canonical JSON, determinism
- AttestationBuilder.validate_schema() — pass/fail cases
- Subject building (commit SHA, repo URI)
- Build definition (external/internal params, resolved dependencies)
- Run details (models, tools, cross-check results)
- Round-trip serialize → deserialize
- Edge cases: empty inputs, missing metadata, partial data
"""

from __future__ import annotations

import json
from typing import Any

from engine.observer import CrossCheckReport, CrossCheckResult, ModelInfo, TimelineEvent
from engine.observer.attestation import (
    BUILD_TYPE,
    PREDICATE_TYPE,
    STATEMENT_TYPE,
    AttestationBuilder,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_timeline() -> list[TimelineEvent]:
    return [
        TimelineEvent(
            timestamp="2026-03-28T10:00:00Z",
            event_type="phase_transition",
            phase="triage",
            iteration=1,
        ),
        TimelineEvent(
            timestamp="2026-03-28T10:01:00Z",
            event_type="llm_call",
            phase="triage",
            iteration=1,
            description="Classify issue",
            details={"llm_context": {"model": "gemini-2.5-pro", "tokens_in": 5000}},
        ),
        TimelineEvent(
            timestamp="2026-03-28T10:05:00Z",
            event_type="file_operation",
            phase="implement",
            iteration=2,
            description="Write fix",
        ),
    ]


def _make_cross_check_report(*, all_pass: bool = True) -> CrossCheckReport:
    report = CrossCheckReport()
    checks = [
        ("diff_consistency", True),
        ("action_completeness", True),
        ("phase_ordering", True),
        ("token_plausibility", True),
        ("tool_call_integrity", all_pass),
    ]
    for name, passed in checks:
        report.add(
            CrossCheckResult(
                check_name=name,
                passed=passed,
                details=f"{name} {'passed' if passed else 'FAILED'}",
            )
        )
    return report


def _make_model_info() -> list[ModelInfo]:
    return [
        ModelInfo(
            model="gemini-2.5-pro",
            provider="google",
            temperature=0.2,
            total_calls=5,
            total_tokens_in=25000,
            total_tokens_out=5000,
        ),
    ]


def _make_metadata() -> dict[str, Any]:
    return {
        "id": "exec-001",
        "started_at": "2026-03-28T10:00:00Z",
        "completed_at": "2026-03-28T10:25:00Z",
        "trigger": {
            "type": "github_issue",
            "source_url": "https://github.com/org/repo/issues/123",
        },
        "target": {
            "repo": "org/repo",
            "ref": "abc123def",
        },
    }


def _make_config() -> dict[str, Any]:
    return {
        "engine_version": "0.9.0",
        "overrides": {"llm": {"temperature": 0.3}},
    }


def _make_prompt_digests() -> dict[str, str]:
    return {
        "prompt://triage.md": "sha256:aaa111",
        "prompt://implement.md": "sha256:bbb222",
        "prompt://review.md": "sha256:ccc333",
        "prompt://validate.md": "sha256:ddd444",
    }


def _make_tool_definitions() -> dict[str, Any]:
    return {
        "tools": ["file_read", "file_write", "shell_run", "git_diff"],
        "digest": "sha256:toolhash123",
    }


def _build_full_attestation(**overrides: Any) -> dict[str, Any]:
    builder = AttestationBuilder()
    kwargs: dict[str, Any] = {
        "timeline": _make_timeline(),
        "cross_check_report": _make_cross_check_report(),
        "execution_metadata": _make_metadata(),
        "execution_config": _make_config(),
        "execution_result": {"status": "success", "commit_sha": "deadbeef123"},
        "model_info": _make_model_info(),
        "prompt_digests": _make_prompt_digests(),
        "tool_definitions": _make_tool_definitions(),
    }
    kwargs.update(overrides)
    return builder.build(**kwargs)


# ===========================================================================
# Build tests
# ===========================================================================


class TestBuildAttestation:
    def test_top_level_structure(self):
        att = _build_full_attestation()
        assert att["_type"] == STATEMENT_TYPE
        assert att["predicateType"] == PREDICATE_TYPE
        assert isinstance(att["subject"], list)
        assert isinstance(att["predicate"], dict)

    def test_subject_contains_commit_sha(self):
        att = _build_full_attestation()
        assert len(att["subject"]) == 1
        subject = att["subject"][0]
        assert subject["digest"]["sha1"] == "deadbeef123"

    def test_subject_repo_uri(self):
        att = _build_full_attestation()
        subject = att["subject"][0]
        assert subject["name"] == "git+https://github.com/org/repo"

    def test_subject_fallback_ref_when_no_commit_sha(self):
        att = _build_full_attestation(execution_result={"status": "success"})
        subject = att["subject"][0]
        assert subject["digest"]["sha1"] == "abc123def"

    def test_build_definition_present(self):
        att = _build_full_attestation()
        bd = att["predicate"]["buildDefinition"]
        assert bd["buildType"] == BUILD_TYPE
        assert "externalParameters" in bd
        assert "internalParameters" in bd
        assert "resolvedDependencies" in bd

    def test_external_parameters(self):
        att = _build_full_attestation()
        ext = att["predicate"]["buildDefinition"]["externalParameters"]
        assert ext["issue_url"] == "https://github.com/org/repo/issues/123"
        assert ext["config_overrides"] == {"llm": {"temperature": 0.3}}

    def test_internal_parameters(self):
        att = _build_full_attestation()
        internal = att["predicate"]["buildDefinition"]["internalParameters"]
        assert internal["engine_version"] == "0.9.0"

    def test_resolved_dependencies_include_base_repo(self):
        att = _build_full_attestation()
        deps = att["predicate"]["buildDefinition"]["resolvedDependencies"]
        repo_deps = [d for d in deps if "abc123def" in d.get("uri", "")]
        assert len(repo_deps) == 1
        assert repo_deps[0]["digest"]["sha1"] == "abc123def"

    def test_resolved_dependencies_include_prompt_digests(self):
        att = _build_full_attestation()
        deps = att["predicate"]["buildDefinition"]["resolvedDependencies"]
        prompt_deps = [d for d in deps if d.get("uri", "").startswith("prompt://")]
        assert len(prompt_deps) == 4

    def test_run_details_present(self):
        att = _build_full_attestation()
        rd = att["predicate"]["runDetails"]
        assert "builder" in rd
        assert "metadata" in rd
        assert "models" in rd
        assert "toolDefinitions" in rd
        assert "crossCheckResults" in rd

    def test_metadata_timing(self):
        att = _build_full_attestation()
        meta = att["predicate"]["runDetails"]["metadata"]
        assert meta["startedOn"] == "2026-03-28T10:00:00Z"
        assert meta["finishedOn"] == "2026-03-28T10:25:00Z"

    def test_models_section(self):
        att = _build_full_attestation()
        models = att["predicate"]["runDetails"]["models"]
        assert len(models) == 1
        m = models[0]
        assert m["id"] == "gemini-2.5-pro"
        assert m["provider"] == "google"
        assert m["total_calls"] == 5
        assert m["total_tokens_in"] == 25000
        assert m["total_tokens_out"] == 5000
        assert m["temperature"] == 0.2

    def test_tool_definitions_section(self):
        att = _build_full_attestation()
        tools = att["predicate"]["runDetails"]["toolDefinitions"]
        assert tools["digest"] == "sha256:toolhash123"
        assert "file_read" in tools["tools"]
        assert "shell_run" in tools["tools"]

    def test_cross_check_results(self):
        att = _build_full_attestation()
        ccr = att["predicate"]["runDetails"]["crossCheckResults"]
        assert ccr["diff_consistency"]["passed"] is True
        assert ccr["action_completeness"]["passed"] is True
        assert ccr["phase_ordering"]["passed"] is True
        assert ccr["token_plausibility"]["passed"] is True
        assert ccr["tool_call_integrity"]["passed"] is True

    def test_cross_check_failure_includes_details(self):
        report = _make_cross_check_report(all_pass=False)
        att = _build_full_attestation(cross_check_report=report)
        ccr = att["predicate"]["runDetails"]["crossCheckResults"]
        assert ccr["tool_call_integrity"]["passed"] is False
        assert "details" in ccr["tool_call_integrity"]

    def test_multiple_models(self):
        models = [
            ModelInfo(model="gemini-2.5-pro", provider="google", total_calls=3),
            ModelInfo(model="claude-sonnet-4-20250514", provider="anthropic", total_calls=2),
        ]
        att = _build_full_attestation(model_info=models)
        assert len(att["predicate"]["runDetails"]["models"]) == 2

    def test_model_without_temperature(self):
        models = [ModelInfo(model="test-model", provider="test", total_calls=1)]
        att = _build_full_attestation(model_info=models)
        m = att["predicate"]["runDetails"]["models"][0]
        assert "temperature" not in m


# ===========================================================================
# Empty / minimal input tests
# ===========================================================================


class TestBuildWithMinimalInputs:
    def test_empty_timeline(self):
        att = _build_full_attestation(timeline=[])
        assert att["_type"] == STATEMENT_TYPE

    def test_no_metadata(self):
        att = _build_full_attestation(execution_metadata=None, execution_result={})
        assert att["subject"][0]["digest"]["sha1"] == "unknown"

    def test_no_config(self):
        att = _build_full_attestation(execution_config=None)
        internal = att["predicate"]["buildDefinition"]["internalParameters"]
        assert internal["engine_version"] == "dev"

    def test_no_models(self):
        att = _build_full_attestation(model_info=None)
        assert att["predicate"]["runDetails"]["models"] == []

    def test_no_prompt_digests(self):
        att = _build_full_attestation(prompt_digests=None)
        deps = att["predicate"]["buildDefinition"]["resolvedDependencies"]
        prompt_deps = [d for d in deps if d.get("uri", "").startswith("prompt://")]
        assert len(prompt_deps) == 0

    def test_no_tool_definitions(self):
        att = _build_full_attestation(tool_definitions=None)
        tools = att["predicate"]["runDetails"]["toolDefinitions"]
        assert tools["tools"] == []
        assert tools["digest"] == ""

    def test_empty_cross_check_report(self):
        att = _build_full_attestation(cross_check_report=CrossCheckReport())
        ccr = att["predicate"]["runDetails"]["crossCheckResults"]
        assert ccr == {}

    def test_all_defaults(self):
        builder = AttestationBuilder()
        att = builder.build(
            timeline=[],
            cross_check_report=CrossCheckReport(),
        )
        violations = AttestationBuilder.validate_schema(att)
        assert len(violations) == 0


# ===========================================================================
# Serialize tests
# ===========================================================================


class TestSerialize:
    def test_deterministic(self):
        att = _build_full_attestation()
        s1 = AttestationBuilder.serialize(att)
        s2 = AttestationBuilder.serialize(att)
        assert s1 == s2

    def test_sorted_keys(self):
        att = _build_full_attestation()
        canonical = AttestationBuilder.serialize(att)
        parsed = json.loads(canonical)
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_no_extra_whitespace(self):
        att = _build_full_attestation()
        canonical = AttestationBuilder.serialize(att)
        assert "  " not in canonical
        assert "\n" not in canonical

    def test_valid_json(self):
        att = _build_full_attestation()
        canonical = AttestationBuilder.serialize(att)
        parsed = json.loads(canonical)
        assert parsed["_type"] == STATEMENT_TYPE

    def test_round_trip(self):
        att = _build_full_attestation()
        canonical = AttestationBuilder.serialize(att)
        parsed = json.loads(canonical)
        re_serialized = AttestationBuilder.serialize(parsed)
        assert canonical == re_serialized


# ===========================================================================
# Schema validation tests
# ===========================================================================


class TestValidateSchema:
    def test_valid_attestation(self):
        att = _build_full_attestation()
        violations = AttestationBuilder.validate_schema(att)
        assert violations == []

    def test_missing_type(self):
        att = _build_full_attestation()
        del att["_type"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("_type" in v for v in violations)

    def test_wrong_type(self):
        att = _build_full_attestation()
        att["_type"] = "wrong"
        violations = AttestationBuilder.validate_schema(att)
        assert any("Invalid _type" in v for v in violations)

    def test_missing_subject(self):
        att = _build_full_attestation()
        del att["subject"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("subject" in v for v in violations)

    def test_empty_subject(self):
        att = _build_full_attestation()
        att["subject"] = []
        violations = AttestationBuilder.validate_schema(att)
        assert any("non-empty" in v for v in violations)

    def test_subject_missing_name(self):
        att = _build_full_attestation()
        att["subject"] = [{"digest": {"sha1": "abc"}}]
        violations = AttestationBuilder.validate_schema(att)
        assert any("name" in v for v in violations)

    def test_subject_missing_digest(self):
        att = _build_full_attestation()
        att["subject"] = [{"name": "test"}]
        violations = AttestationBuilder.validate_schema(att)
        assert any("digest" in v for v in violations)

    def test_missing_predicate(self):
        att = _build_full_attestation()
        del att["predicate"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("predicate" in v for v in violations)

    def test_missing_predicate_type(self):
        att = _build_full_attestation()
        del att["predicateType"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("predicateType" in v for v in violations)

    def test_wrong_predicate_type(self):
        att = _build_full_attestation()
        att["predicateType"] = "wrong"
        violations = AttestationBuilder.validate_schema(att)
        assert any("Invalid predicateType" in v for v in violations)

    def test_missing_build_definition(self):
        att = _build_full_attestation()
        del att["predicate"]["buildDefinition"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("buildDefinition" in v for v in violations)

    def test_missing_run_details(self):
        att = _build_full_attestation()
        del att["predicate"]["runDetails"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("runDetails" in v for v in violations)

    def test_wrong_build_type(self):
        att = _build_full_attestation()
        att["predicate"]["buildDefinition"]["buildType"] = "wrong"
        violations = AttestationBuilder.validate_schema(att)
        assert any("buildType" in v for v in violations)

    def test_missing_builder(self):
        att = _build_full_attestation()
        del att["predicate"]["runDetails"]["builder"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("builder" in v for v in violations)

    def test_missing_metadata(self):
        att = _build_full_attestation()
        del att["predicate"]["runDetails"]["metadata"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("metadata" in v for v in violations)

    def test_non_dict_input(self):
        violations = AttestationBuilder.validate_schema("not a dict")  # type: ignore[arg-type]
        assert len(violations) == 1
        assert "must be a dict" in violations[0]

    def test_subject_entry_not_dict(self):
        att = _build_full_attestation()
        att["subject"] = ["not-a-dict"]
        violations = AttestationBuilder.validate_schema(att)
        assert any("must be a dict" in v for v in violations)

    def test_predicate_not_dict(self):
        att = _build_full_attestation()
        att["predicate"] = "string"
        violations = AttestationBuilder.validate_schema(att)
        assert any("predicate must be a dict" in v for v in violations)

    def test_multiple_violations(self):
        att = {"_type": "wrong"}
        violations = AttestationBuilder.validate_schema(att)
        assert len(violations) >= 3


# ===========================================================================
# Subject building edge cases
# ===========================================================================


class TestSubjectBuilding:
    def test_local_path_repo(self):
        meta = {"target": {"repo_path": "/tmp/my-repo", "ref": "abc123"}}
        att = _build_full_attestation(
            execution_metadata=meta,
            execution_result={"commit_sha": "def456"},
        )
        subject = att["subject"][0]
        assert subject["name"] == "/tmp/my-repo"
        assert subject["digest"]["sha1"] == "def456"

    def test_github_repo_slug(self):
        meta = {"target": {"repo": "myorg/myrepo", "ref": "abc"}}
        att = _build_full_attestation(execution_metadata=meta)
        assert att["subject"][0]["name"] == "git+https://github.com/myorg/myrepo"

    def test_already_prefixed_repo(self):
        meta = {"target": {"repo": "git+https://github.com/a/b", "ref": "x"}}
        att = _build_full_attestation(execution_metadata=meta)
        assert att["subject"][0]["name"] == "git+https://github.com/a/b"


# ===========================================================================
# Integration: reconstructor data → attestation
# ===========================================================================


class TestIntegration:
    def test_build_from_reconstructor_types(self):
        """Verify the attestation builder works with types from the observer package."""
        timeline = _make_timeline()
        report = _make_cross_check_report()
        models = _make_model_info()

        builder = AttestationBuilder()
        att = builder.build(
            timeline=timeline,
            cross_check_report=report,
            execution_metadata=_make_metadata(),
            execution_config=_make_config(),
            execution_result={"status": "success", "commit_sha": "abc123"},
            model_info=models,
            prompt_digests=_make_prompt_digests(),
            tool_definitions=_make_tool_definitions(),
        )

        violations = AttestationBuilder.validate_schema(att)
        assert violations == []

        canonical = AttestationBuilder.serialize(att)
        assert isinstance(canonical, str)
        parsed = json.loads(canonical)
        assert parsed["_type"] == STATEMENT_TYPE

    def test_full_round_trip_serialization(self):
        att = _build_full_attestation()
        canonical = AttestationBuilder.serialize(att)
        parsed = json.loads(canonical)

        violations = AttestationBuilder.validate_schema(parsed)
        assert violations == []

        re_serialized = AttestationBuilder.serialize(parsed)
        assert canonical == re_serialized
