# rl-bug-fix-full-send: Complete Review & fullsend Contribution

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
│ GitHub Actions Workflow (rl-engine.yml)  │
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

Each phase subclasses `Phase` and implements an **OODA cycle** (observe → plan → act → validate → reflect). The loop (`PipelineEngine`) manages phase transitions, backtracking on review rejection, escalation caps, exponential backoff, time budgets, and execution recording.

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
The `rl-engine.yml` declares `outputs.pr_number` and `outputs.repo` on the agent job, but the "Run RL Engine" step only writes `status` to `$GITHUB_OUTPUT`. The observer job's "Post policy result to PR" step depends on `pr_number` being set — so it **never runs**, even on successful PR-producing runs. The observer attestation gets uploaded as an artifact but never reaches the PR as a comment.

**3. `PipelineEngine` is a god object**
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

## Part 3: The Meta-Loop Experiment — Feeding a Bug Fix Engine's Results Back to an LLM to Improve the Engine

### Artifacts

Everything described below is public. Click through to see the actual runs, commits, and PRs.

| Artifact | Link |
|----------|------|
| Repository | [ascerra/rl-bug-fix-full-send](https://github.com/ascerra/rl-bug-fix-full-send) |
| Target issue | [nonflux/build-definitions#1](https://github.com/nonflux/build-definitions/issues/1) |
| Run #26 (success) | [23618411249](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23618411249) |
| PR #3 (first engine-produced PR) | [nonflux/build-definitions#3](https://github.com/nonflux/build-definitions/pull/3) |
| PR #4 (from run #26) | [nonflux/build-definitions#4](https://github.com/nonflux/build-definitions/pull/4) |
| Auto-fix #1: scope creep | [`e06bd71`](https://github.com/ascerra/rl-bug-fix-full-send/commit/e06bd71) |
| Auto-fix #2: truncation 5k→50k | [`4e2623b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/4e2623b) |
| Auto-fix #3: missing git commit | [`f13e984`](https://github.com/ascerra/rl-bug-fix-full-send/commit/f13e984) |
| Auto-fix #4: unique branch names | [`1a1c56b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/1a1c56b) |
| All commit history | [ascerra/rl-bug-fix-full-send/commits/main](https://github.com/ascerra/rl-bug-fix-full-send/commits/main/) |
| All workflow runs | [ascerra/rl-bug-fix-full-send/actions](https://github.com/ascerra/rl-bug-fix-full-send/actions) |

### The Engine Works on Any Repo Without Changing It

Most agent tooling requires the target repository to opt in — install a GitHub App, add a config file, label issues a certain way, configure permissions. The RL Bug Fix Engine doesn't. It's a single GitHub Actions workflow that contains the entire bug fix pipeline: read the issue, clone the repo, analyze the code, write a fix, review it, and open a cross-fork PR. All in one workflow run.

The target repo owners don't set anything up. They don't install anything. A PR shows up on their repo like any other contribution. They review it, merge it or don't — the same workflow they already have for human contributors. The engine reads their code and issues through public APIs and git, operates on repositories it has never seen before, and requires zero coordination from the people who maintain them.

### The Core Idea

When the engine runs, it produces artifacts — execution traces, review findings, phase results, error messages, and the PRs it created. These artifacts contain everything about *how* the engine approached the problem, *what* it tried, *why* it failed or succeeded, and *what the fix looked like*. If you feed all of that back to an LLM — not just the error, but the full history of decisions and outcomes — it can do more than patch a crash. It can rethink the strategy: should the prompts give different guidance? Was the context too limited for the engine's LLM to reason correctly? Did the workflow skip a step that only matters in production? The LLM isn't just fixing failures — it's reviewing the engine's past solutions to improve how the engine solves future problems.

There are two separate things running in two separate places:

1. **The RL Bug Fix Engine** — a GitHub Actions workflow that runs entirely in CI. Each run is a complete end-to-end bug fix attempt: it reads the issue, clones the target repo, analyzes the code, writes a fix, self-reviews it, and opens a cross-fork PR. Five phases (triage, implement, review, validate, report), no human intervention. **The target repository doesn't need any configuration, labels, bot integrations, or code changes to support this** — you just point the engine at an issue URL and it works.

2. **The meta-loop** — a local script (`scripts/meta-loop.sh` + `scripts/meta_loop_agent.py`) that I ran on my machine. It triggers a full engine run in GitHub Actions, waits for it to finish, downloads the execution artifacts, and feeds them to an LLM. The LLM reviews how the engine approached the problem, what went wrong, and generates patches to the engine's own source code (prompts, phase logic, workflow config). Then the script pushes those changes and kicks off another complete engine run to see if the fix worked.

```
[local machine]                              [GitHub Actions]
meta-loop.sh                                 RL Bug Fix Engine
    │                                             │
    ├─ trigger workflow ─────────────────────────►│ full e2e bug fix run
    │                                             │ (triage → implement → review
    │                                             │  → validate → push PR → report)
    │◄─ download execution artifacts ─────────────┤
    │                                             │
    ├─ LLM reads full trace                       │
    │  (how the engine reasoned, what it tried,   │
    │   what the fix looked like, why it failed)  │
    │                                             │
    ├─ patch engine source code                   │
    ├─ push changes                               │
    ├─ trigger next workflow ────────────────────►│ another full e2e bug fix run
    │                                             │
    └─ repeat until success                       │
```

Each arrow labeled "trigger workflow" kicks off a **complete, independent bug fix attempt** — not a retry of a failed step, but the whole pipeline from scratch against the target issue. The meta-loop isn't fixing the bug; it's fixing the *engine* so the next full run gets it right.

### How the Loop Got It Working

The target issue was [nonflux/build-definitions#1](https://github.com/nonflux/build-definitions/issues/1) — an intermittent race condition in a Tekton StepAction where parallel image processing used shared temp directories. The meta-loop was launched in continuous mode with `--auto-push`, meaning: if the LLM diagnoses the problem and generates a patch, the script commits it, pushes it, and triggers the next run automatically. No human in the loop.

```bash
./scripts/meta-loop.sh \
  --issue-url "https://github.com/nonflux/build-definitions/issues/1" \
  --fork-repo "ascerra/build-definitions" \
  --provider gemini \
  --continuous \
  --max-runs 10 \
  --auto-push
```

Four autonomous self-corrections happened in sequence:

**Auto-fix #1 — Scope creep in implement phase**

| | |
|---|---|
| **Failed run** | [23613985882](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23613985882) @ [`40b7e95`](https://github.com/ascerra/rl-bug-fix-full-send/commit/40b7e95) |
| **What happened** | The review agent rejected the fix because the implement agent added unrelated changes (scope creep). The implement-review loop hit the escalation cap without converging. |
| **LLM diagnosis** | The LLM read the [execution JSON](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23613985882) (downloadable as a workflow artifact from every run), saw repeated review rejections with findings about out-of-scope changes, and identified that the implement prompt had no guidance about staying in scope when review feedback flagged it. |
| **Auto-fix commit** | [`e06bd71`](https://github.com/ascerra/rl-bug-fix-full-send/commit/e06bd71) — Strengthened the implement agent's prompt with a scope creep warning, added a `_check_path_consistency()` safety net to the review agent, improved the review prompt template. |
| **Files changed** | `engine/phases/implement.py`, `engine/phases/review.py`, `engine/config.py`, `templates/prompts/review.md` |

**Auto-fix #2 — File content truncation broke generated code**

| | |
|---|---|
| **Failed run** | [23614415889](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23614415889) @ [`e06bd71`](https://github.com/ascerra/rl-bug-fix-full-send/commit/e06bd71) |
| **What happened** | The implement agent truncated file content at 5,000 characters before sending it to the LLM. The target file was larger than 5k, so the LLM received a cut-off file and generated broken code with syntax errors. The review agent correctly rejected the broken output, but the implement agent kept receiving the same truncated input, creating an infinite rejection loop. |
| **LLM diagnosis** | Read the review findings (syntax errors, unterminated functions), correlated with the file sizes in the execution record, and identified the 5k truncation limit as the root cause. |
| **Auto-fix commit** | [`4e2623b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/4e2623b) — Increased file content truncation from 5,000 to 50,000 characters in both `engine/phases/implement.py` and `engine/phases/review.py`. |
| **Files changed** | `engine/phases/implement.py` (+1 −1), `engine/phases/review.py` (+1 −1) |

**Intermediate success + manual fixes**

Run [23615068030](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23615068030) @ [`4e2623b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/4e2623b) succeeded partially — the engine got through all agents but the validate agent had logging issues. Two manual fixes followed:
- [`a0cc93c`](https://github.com/ascerra/rl-bug-fix-full-send/commit/a0cc93c) — Fixed validate phase error logging
- [`6236e49`](https://github.com/ascerra/rl-bug-fix-full-send/commit/6236e49) — Made validate phase fail properly when PR creation fails

**Auto-fix #3 — Implement didn't commit changes before validate**

| | |
|---|---|
| **Failed run** | [23616933542](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23616933542) @ [`6236e49`](https://github.com/ascerra/rl-bug-fix-full-send/commit/6236e49) |
| **What happened** | The implement agent wrote file changes to the working directory but never ran `git commit`. When the validate agent tried to push the branch and create a PR, there were no committed changes to push. |
| **LLM diagnosis** | Read the execution trace showing the implement agent succeeded (files written) but the validate agent failed (nothing to push). Identified the missing git commit step. |
| **Auto-fix commit** | [`f13e984`](https://github.com/ascerra/rl-bug-fix-full-send/commit/f13e984) — Added a git commit step to the implement agent's workflow after file writes succeed. |
| **Files changed** | `engine/phases/implement.py` (+19) |

**First fully successful run → [PR #3](https://github.com/nonflux/build-definitions/pull/3)**

Run [23617134590](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23617134590) @ [`f13e984`](https://github.com/ascerra/rl-bug-fix-full-send/commit/f13e984) — **SUCCESS**. The triage agent identified the root cause, the implement agent wrote a fix (unique per-image temp paths), the review agent approved it, the validate agent committed and pushed, and created [PR #3 on nonflux/build-definitions](https://github.com/nonflux/build-definitions/pull/3). This was the first real PR the engine ever produced.

**Grading the engine's PR against the real human fix**

After PR #3, I had Cursor read the engine's fix ([run 23617134590](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23617134590)), then read the actual human-authored fix for the same bug — [PR #3057 on konflux-ci/build-definitions](https://github.com/konflux-ci/build-definitions/pull/3057) by zxiong, which had already been merged upstream. The goal was to compare the engine's output against what a human engineer actually shipped.

Both used the same strategy — adding a unique `image_num` to temp paths. The engine matched the human's approach and arrived at it in 2.8 minutes autonomously, with better documentation (comprehensive PR body with root cause analysis and testing plan). But the human fix was more precise: the engine dropped `:latest` from the OCI cleanup path, which the human kept consistent across all operations. The engine's self-review (0 findings) failed to catch this subtle inconsistency.

| | Human Fix ([PR #3057](https://github.com/konflux-ci/build-definitions/pull/3057)) | Engine Fix ([PR #3](https://github.com/nonflux/build-definitions/pull/3)) |
|---|---|---|
| **Grade** | **A** | **A-** |
| Root cause | A | A+ (detailed, precise explanation) |
| Code quality | A+ (perfectly consistent paths) | A- (correct strategy, but `:latest` dropped in cleanup) |
| Scope | A+ (minimal) | A+ (minimal) |
| Documentation | B+ (clear but terse) | A+ (comprehensive PR body) |
| Speed | C (hours to merge) | A+ (2.8 min autonomous) |
| Review depth | N/A | B (missed path consistency) |

This comparison led to concrete improvements, committed as [`98144ad`](https://github.com/ascerra/rl-bug-fix-full-send/commit/98144ad):

| Finding | Engine improvement |
|---------|-------------------|
| The review agent missed the `:latest` path inconsistency between creation and cleanup | Added review dimension #6: "Consistency of Paired Operations" to the review prompt. Added a deterministic `_check_path_consistency()` safety net in the review agent that regex-extracts paths from shell scripts and detects OCI tag mismatches — this would have caught the exact bug. |
| The implement agent didn't maintain exact path patterns across paired operations | Added "Consistency Requirements" section to the implement prompt — maintain path patterns across create/cleanup, follow parameter ordering conventions, verify all call sites |

These were also added to the implementation plan and built into the engine during the final ralph loop session. This is the kind of improvement the meta-loop itself could produce if configured to review successful runs and their PRs, not just failures.

**Auto-fix #4 — Branch name collision**

| | |
|---|---|
| **Failed run** | [23618209219](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23618209219) @ [`98144ad`](https://github.com/ascerra/rl-bug-fix-full-send/commit/98144ad) |
| **What happened** | The validate agent tried to push to branch `rl/fix`, but that branch already existed from PR #3. The push failed with a conflict. |
| **LLM diagnosis** | Read the validate agent's error (push rejection), identified that branch names were hardcoded and would collide on repeat runs. |
| **Auto-fix commit** | [`1a1c56b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/1a1c56b) — Generate unique branch names with UUID suffix (e.g., `rl/fix-1-3f3b380e`). |
| **Files changed** | `engine/phases/validate.py` (+13 −4) |

**Second successful run → [PR #4](https://github.com/nonflux/build-definitions/pull/4)**

[**Run #26** (23618411249)](https://github.com/ascerra/rl-bug-fix-full-send/actions/runs/23618411249) @ [`1a1c56b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/1a1c56b) — **SUCCESS** in 6 minutes. Produced [PR #4](https://github.com/nonflux/build-definitions/pull/4) on branch `rl/fix-1-3f3b380e`.

### The Self-Improvement Pattern

Here is what makes this interesting as an experiment:

**1. Each auto-fix addressed a genuinely different category of bug:**

| Auto-fix | Category | What the LLM identified |
|----------|----------|----------------------|
| #1 ([`e06bd71`](https://github.com/ascerra/rl-bug-fix-full-send/commit/e06bd71)) | Prompt design | The implement agent's prompt needed scope constraints when the review agent flags drift |
| #2 ([`4e2623b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/4e2623b)) | Context window | 5k char file limit was too small for the implement agent to work with real-world files |
| #3 ([`f13e984`](https://github.com/ascerra/rl-bug-fix-full-send/commit/f13e984)) | Missing step | The implement agent wrote files but never committed them |
| #4 ([`1a1c56b`](https://github.com/ascerra/rl-bug-fix-full-send/commit/1a1c56b)) | State management | Hardcoded branch names collide on repeated runs |

**2. The LLM had real signal to work with.** The engine produces structured execution artifacts (JSON with phase results, review findings, error traces, iteration counts). The LLM received ~350k characters of context per diagnosis call. This isn't a vague "it failed" — it's a detailed execution trace that lets the LLM reason about *why* the engine failed.

**3. The fixes were small and correct.** Auto-fix #2 changed 2 lines. Auto-fix #4 changed 17 lines. The LLM wasn't rewriting the engine; it was making targeted, surgical fixes based on specific evidence from the execution trace.

**4. The loop discovered bugs that testing couldn't.** 2,983 unit tests all passed before any production run. The failures were integration-level: real file sizes exceeding limits, real git branches colliding, missing workflow steps that only matter in a real CI environment. These bugs only manifest when the full system runs against real repositories — exactly the environment the meta-loop provides.

### What This Means

The meta-loop demonstrates a concrete pattern: **production execution as a feedback signal for improving the engine**. You don't need human-written evals or curated benchmarks. You need:

1. **Structured execution artifacts** — not just "pass/fail" but detailed traces of what happened and why
2. **An LLM** that can read those artifacts and propose targeted fixes to the engine
3. **A script** that applies the fixes, pushes, and re-runs the engine to verify them

The four auto-fix commits took the engine from "fails on every real repo" to "produces correct PRs in a single pass." The total time from first meta-loop run to first successful PR was about 90 minutes of wall clock (most of which was CI execution time, not human effort).

Two findings from this experiment: the meta-loop pattern works for improving an engine using its own production results, and the engine itself proves you can build a fully autonomous bug fix system as a single workflow that operates on any repo without requiring the target to change anything.

