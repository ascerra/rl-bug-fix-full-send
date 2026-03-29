"""Observer pipeline entry point — ``python -m engine.observer``.

Wires together the full observer pipeline:

1. **Reconstruct** — load agent artifacts, build execution timeline
2. **Cross-check** — verify agent claims against evidence
3. **Build attestation** — produce in-toto Statement v1
4. **Sign** — sign via Sigstore OIDC, cosign key, or no-op
5. **Evaluate policy** — apply rules against the attestation
6. **Write outputs** — attestation, policy result, and PR comment
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from engine.config import load_config
from engine.observer.attestation import AttestationBuilder
from engine.observer.cli import parse_args
from engine.observer.cross_checker import CrossChecker
from engine.observer.policy import PolicyEvaluator, load_policy
from engine.observer.reconstructor import ExecutionReconstructor
from engine.observer.signer import AttestationSigner

EXIT_OK = 0
EXIT_POLICY_FAILED = 1
EXIT_OBSERVER_ERROR = 2


def run_observer(
    artifacts_dir: str,
    output_dir: str = "./attestation",
    config_path: str = "",
    branch_dir: str = "",
    templates_dir: str = "",
    skip_signing: bool = False,
) -> dict[str, Any]:
    """Execute the full observer pipeline and return a summary dict.

    Returns a dict with keys: ``policy_passed``, ``attestation_path``,
    ``policy_result``, ``pr_comment``, and ``summary``.
    """
    config = load_config(config_path) if config_path else load_config()
    observer_cfg = config.observer

    recon = ExecutionReconstructor()
    recon.load_artifacts(Path(artifacts_dir))
    timeline = recon.build_timeline()
    model_info = recon.extract_model_info()
    prompt_digests = recon.extract_prompt_digests(Path(templates_dir) if templates_dir else None)
    tool_defs = recon.extract_tool_definitions()
    exec_metadata = recon.get_execution_metadata()
    exec_config = recon.get_execution_config()
    exec_result = recon.get_execution_result()

    checker = CrossChecker()
    cross_report = checker.run_all_checks(
        timeline=timeline,
        execution_data=recon.execution_data,
        branch_dir=Path(branch_dir) if branch_dir else None,
        transcript_calls=recon.get_transcript_calls() or None,
    )

    builder = AttestationBuilder()
    attestation = builder.build(
        timeline=timeline,
        cross_check_report=cross_report,
        execution_metadata=exec_metadata,
        execution_config=exec_config,
        execution_result=exec_result,
        model_info=model_info,
        prompt_digests=prompt_digests,
        tool_definitions=tool_defs,
    )

    schema_violations = AttestationBuilder.validate_schema(attestation)
    if schema_violations:
        print(
            f"WARNING: Attestation schema violations: {schema_violations}",
            file=sys.stderr,
        )

    canonical_json = AttestationBuilder.serialize(attestation)

    signer = AttestationSigner()
    signing_method = "none" if skip_signing else observer_cfg.signing_method
    signed = signer.sign(canonical_json, method=signing_method)

    out = Path(output_dir)
    written_files = signed.write(out)

    policy_data = load_policy(observer_cfg.policy_file)
    evaluator = PolicyEvaluator()

    triage_components = _extract_triage_components(recon.execution_data)
    issue_body = _extract_issue_body(recon.execution_data)

    policy_result = evaluator.evaluate(
        signed,
        policy_data,
        triage_components=triage_components,
        issue_body=issue_body,
    )

    policy_path = out / "policy-result.json"
    policy_path.write_text(json.dumps(policy_result.to_dict(), indent=2))
    written_files["policy_result"] = str(policy_path)

    pr_comment = PolicyEvaluator.format_pr_comment(policy_result)
    comment_path = out / "pr-comment.md"
    comment_path.write_text(pr_comment)
    written_files["pr_comment"] = str(comment_path)

    summary = PolicyEvaluator.format_summary(policy_result)
    summary_path = out / "summary.txt"
    summary_path.write_text(summary)
    written_files["summary"] = str(summary_path)

    return {
        "policy_passed": policy_result.passed,
        "attestation_path": written_files.get("attestation", ""),
        "policy_result": policy_result.to_dict(),
        "pr_comment": pr_comment,
        "summary": summary,
        "written_files": written_files,
        "cross_check_all_passed": cross_report.all_passed,
        "schema_violations": schema_violations,
    }


def _extract_triage_components(execution_data: dict[str, Any]) -> list[str]:
    """Extract affected_components from triage iteration artifacts."""
    execution = execution_data.get("execution", execution_data)
    for iteration in execution.get("iterations", []):
        if iteration.get("phase") == "triage":
            artifacts = iteration.get("artifacts", {})
            components = artifacts.get("affected_components", [])
            if isinstance(components, list) and components:
                return components
    return []


def _extract_issue_body(execution_data: dict[str, Any]) -> str:
    """Extract issue body from the execution trigger or triage context."""
    execution = execution_data.get("execution", execution_data)
    trigger = execution.get("trigger", {})
    body = trigger.get("issue_body", "")
    if body:
        return body

    for iteration in execution.get("iterations", []):
        if iteration.get("phase") == "triage":
            obs = iteration.get("observation", {})
            body = obs.get("issue_body", obs.get("body", ""))
            if body:
                return body
    return ""


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns exit code."""
    args = parse_args(argv)

    try:
        result = run_observer(
            artifacts_dir=args.artifacts_dir,
            output_dir=args.output_dir,
            config_path=args.config,
            branch_dir=args.branch_dir,
            templates_dir=args.templates_dir,
            skip_signing=args.skip_signing,
        )
    except Exception as exc:
        print(f"Observer error: {exc}", file=sys.stderr)
        return EXIT_OBSERVER_ERROR

    print(result["summary"])

    if not result["policy_passed"]:
        print("Policy evaluation: FAILED", file=sys.stderr)
        cfg = load_config(args.config) if args.config else load_config()
        if cfg.observer.fail_on_policy_violation:
            return EXIT_POLICY_FAILED
    else:
        print("Policy evaluation: PASSED", file=sys.stderr)

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
