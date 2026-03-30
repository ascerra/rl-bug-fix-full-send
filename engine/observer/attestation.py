"""Attestation builder — produces in-toto Statement v1 attestations.

Transforms the reconstructed execution timeline, cross-check report,
and configuration into a signed provenance attestation conforming to
the in-toto Statement v1 specification with a custom predicate type
aligned to SLSA Build provenance structure.

See SPEC.md §4.3 for the full attestation schema.
"""

from __future__ import annotations

import json
import os
from typing import Any

from engine.observer import CrossCheckReport, ModelInfo, TimelineEvent

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://rl-engine.dev/provenance/agent/v1"
BUILD_TYPE = "https://rl-engine.dev/AgentSynthesis/v1"

_REQUIRED_TOP_LEVEL = {"_type", "subject", "predicateType", "predicate"}
_REQUIRED_PREDICATE = {"buildDefinition", "runDetails"}
_REQUIRED_BUILD_DEF = {"buildType", "externalParameters", "internalParameters"}


class AttestationBuilder:
    """Builds an in-toto Statement v1 attestation from execution data.

    Usage::

        builder = AttestationBuilder()
        attestation = builder.build(
            timeline=timeline,
            cross_check_report=report,
            execution_metadata=metadata,
            execution_config=config,
            model_info=models,
            prompt_digests=digests,
            tool_definitions=tools,
        )
        canonical = AttestationBuilder.serialize(attestation)
    """

    def build(
        self,
        timeline: list[TimelineEvent],
        cross_check_report: CrossCheckReport,
        execution_metadata: dict[str, Any] | None = None,
        execution_config: dict[str, Any] | None = None,
        execution_result: dict[str, Any] | None = None,
        model_info: list[ModelInfo] | None = None,
        prompt_digests: dict[str, str] | None = None,
        tool_definitions: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build an in-toto Statement v1 attestation.

        Returns a Python dict conforming to the attestation schema defined
        in SPEC.md §4.3.
        """
        meta = execution_metadata or {}
        config = execution_config or {}
        result = execution_result or {}
        models = model_info or []
        digests = prompt_digests or {}
        tools = tool_definitions or {}

        subject = self._build_subject(meta, result)
        build_definition = self._build_definition(meta, config, digests, tools)
        run_details = self._build_run_details(meta, models, tools, cross_check_report)

        return {
            "_type": STATEMENT_TYPE,
            "subject": subject,
            "predicateType": PREDICATE_TYPE,
            "predicate": {
                "buildDefinition": build_definition,
                "runDetails": run_details,
            },
        }

    @staticmethod
    def serialize(attestation: dict[str, Any]) -> str:
        """Produce canonical JSON for deterministic signing.

        Keys are sorted and no extra whitespace is included, ensuring
        that the same attestation always serializes to the same bytes.
        """
        return json.dumps(attestation, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def validate_schema(attestation: dict[str, Any]) -> list[str]:
        """Check required fields and types. Returns a list of violations.

        An empty list means the attestation is valid.
        """
        violations: list[str] = []

        if not isinstance(attestation, dict):
            return ["Attestation must be a dict"]

        for field in _REQUIRED_TOP_LEVEL:
            if field not in attestation:
                violations.append(f"Missing required top-level field: {field}")

        if attestation.get("_type") != STATEMENT_TYPE:
            violations.append(
                f"Invalid _type: expected {STATEMENT_TYPE}, got {attestation.get('_type')!r}"
            )

        if attestation.get("predicateType") != PREDICATE_TYPE:
            violations.append(
                f"Invalid predicateType: expected {PREDICATE_TYPE}, "
                f"got {attestation.get('predicateType')!r}"
            )

        subject = attestation.get("subject")
        if not isinstance(subject, list) or len(subject) == 0:
            violations.append("subject must be a non-empty list")
        elif subject:
            for i, entry in enumerate(subject):
                if not isinstance(entry, dict):
                    violations.append(f"subject[{i}] must be a dict")
                    continue
                if "name" not in entry:
                    violations.append(f"subject[{i}] missing 'name'")
                if "digest" not in entry:
                    violations.append(f"subject[{i}] missing 'digest'")

        predicate = attestation.get("predicate")
        if not isinstance(predicate, dict):
            violations.append("predicate must be a dict")
        else:
            for field in _REQUIRED_PREDICATE:
                if field not in predicate:
                    violations.append(f"Missing required predicate field: {field}")

            build_def = predicate.get("buildDefinition")
            if isinstance(build_def, dict):
                for field in _REQUIRED_BUILD_DEF:
                    if field not in build_def:
                        violations.append(f"Missing required buildDefinition field: {field}")
                if build_def.get("buildType") != BUILD_TYPE:
                    violations.append(
                        f"Invalid buildType: expected {BUILD_TYPE}, "
                        f"got {build_def.get('buildType')!r}"
                    )

            run_details = predicate.get("runDetails")
            if isinstance(run_details, dict):
                if "builder" not in run_details:
                    violations.append("Missing required runDetails field: builder")
                if "metadata" not in run_details:
                    violations.append("Missing required runDetails field: metadata")

        return violations

    # ------------------------------------------------------------------
    # Internal builders
    # ------------------------------------------------------------------

    def _build_subject(
        self,
        meta: dict[str, Any],
        result: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Build the subject list (commit SHAs produced by the agent)."""
        target = meta.get("target", {})
        repo = target.get("repo", target.get("repo_path", ""))

        commit_sha = result.get("commit_sha", "")
        if not commit_sha:
            commit_sha = target.get("ref", "unknown")

        repo_uri = repo
        if repo and not repo.startswith("git+"):
            if "/" in repo and not repo.startswith("/"):
                repo_uri = f"git+https://github.com/{repo}"
            else:
                repo_uri = repo

        return [
            {
                "name": repo_uri,
                "digest": {"sha1": commit_sha},
            }
        ]

    def _build_definition(
        self,
        meta: dict[str, Any],
        config: dict[str, Any],
        prompt_digests: dict[str, str],
        tool_definitions: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the buildDefinition section."""
        trigger = meta.get("trigger", {})
        target = meta.get("target", {})

        resolved_deps: list[dict[str, Any]] = []

        base_ref = target.get("ref", "")
        base_repo = target.get("repo", target.get("repo_path", ""))
        if base_ref:
            repo_uri = base_repo
            if (
                base_repo
                and not base_repo.startswith("git+")
                and "/" in base_repo
                and not base_repo.startswith("/")
            ):
                repo_uri = f"git+https://github.com/{base_repo}"
            resolved_deps.append({"uri": f"{repo_uri}@{base_ref}", "digest": {"sha1": base_ref}})

        for uri, digest in sorted(prompt_digests.items()):
            resolved_deps.append({"uri": uri, "digest": {"sha256": digest}})

        return {
            "buildType": BUILD_TYPE,
            "externalParameters": {
                "issue_url": trigger.get("source_url", ""),
                "config_overrides": config.get("overrides", {}),
            },
            "internalParameters": {
                "engine_version": config.get("engine_version", "dev"),
                "workflow_run_id": os.environ.get("GITHUB_RUN_ID", ""),
                "runner_os": os.environ.get("RUNNER_OS", ""),
            },
            "resolvedDependencies": resolved_deps,
        }

    def _build_run_details(
        self,
        meta: dict[str, Any],
        models: list[ModelInfo],
        tool_definitions: dict[str, Any],
        cross_check_report: CrossCheckReport,
    ) -> dict[str, Any]:
        """Build the runDetails section."""
        workflow_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        invocation_id = f"{workflow_url}/{repo}/actions/runs/{run_id}" if repo else ""

        builder_id = ""
        if repo:
            builder_id = f"{workflow_url}/{repo}/.github/workflows/rl-engine.yml"

        model_entries = []
        for m in models:
            entry: dict[str, Any] = {
                "id": m.model,
                "provider": m.provider,
                "total_calls": m.total_calls,
                "total_tokens_in": m.total_tokens_in,
                "total_tokens_out": m.total_tokens_out,
            }
            if m.temperature:
                entry["temperature"] = m.temperature
            model_entries.append(entry)

        cross_check_results: dict[str, Any] = {}
        for check in cross_check_report.checks:
            cross_check_results[check.check_name] = {
                "passed": check.passed,
            }
            if not check.passed:
                cross_check_results[check.check_name]["details"] = check.details

        return {
            "builder": {"id": builder_id},
            "metadata": {
                "invocationId": invocation_id,
                "startedOn": meta.get("started_at", ""),
                "finishedOn": meta.get("completed_at", ""),
            },
            "models": model_entries,
            "toolDefinitions": {
                "digest": tool_definitions.get("digest", ""),
                "tools": tool_definitions.get("tools", []),
            },
            "crossCheckResults": cross_check_results,
        }
