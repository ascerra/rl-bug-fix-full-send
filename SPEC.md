# Technical Specification: RL Bug Fix Full Send

## 1. Overview

**RL Bug Fix Full Send** is an agentic SDLC engine — developed and maintained using iterative Ralph Loops — that autonomously triages, implements, reviews, tests, and reports on bug fixes in GitHub-hosted repositories. The production engine is a phased OODA pipeline (not itself a Ralph Loop), though it borrows the Ralph Loop philosophy: iteration beats perfection, and failures are data. The system runs exclusively in GitHub Actions, produces interactive visual evidence of every decision and action, and is designed to eventually drive the entire software development lifecycle for a GitHub organization.

The MVP validates the engine against known-solved bugs: fork a repo, roll back the human fix, let the engine attempt the fix, then compare agent output against the human solution.

### 1.1 What is a Ralph Loop

A **Ralph Loop** is our adaptation of the **Ralph Wiggum Loop**, an agentic iteration pattern created by Geoffrey Huntley in 2025. Named after the perpetually confused but relentlessly persistent Simpsons character, the pattern embodies a core philosophy: **iteration beats perfection, and failures are data**.

#### The Original Pattern

In its purest form, the Ralph Wiggum Loop is a bash loop:

```bash
while :; do cat PROMPT.md | claude-code ; done
```

The agent runs a task, produces output (including all errors and failures), and the loop feeds that output back as context for the next attempt. The prompt stays the same, but the codebase evolves. Each iteration, the agent reads the *current state* of files and learns from what already worked or failed. Key mechanics:

- **Fresh context each iteration** — the agent starts with a clean context window, preventing accumulated confusion from prior attempts
- **State persists in artifacts, not conversation** — changes live in files, git history, and progress markers, not in-memory chat context
- **Unfiltered feedback** — the agent receives raw, unsanitized output including all errors, forcing direct confrontation with its mistakes
- **Concrete stop conditions** — the loop continues until an objective completion criterion is met (tests pass, build succeeds, zero lint errors) or a safety limit halts the run

The technique is "deterministically bad in an undeterministic world" — it embraces that agents will make mistakes, but ensures those mistakes inform the next attempt rather than compounding in a degrading context window.

#### Known Failure Modes

The Ralph Wiggum Loop has well-documented failure modes that our production loop is specifically designed to mitigate:

| Failure Mode | Description | Our Mitigation |
|---|---|---|
| **Infinite looping** | No firm stop condition, or success is unreachable | Iteration cap + time budget + escalation policy |
| **Oscillation** | Fix A breaks B, fix B reintroduces A | Independent phase validation; review phase catches regressions |
| **Context overload** | Accumulated logs make agent lose the original goal | Fresh context per iteration; structured observation of prior history rather than raw log dump |
| **Hallucination amplification** | False assumption becomes entrenched context | Zero-trust between phases; each phase validates independently |
| **Metric gaming** | Agent deletes tests instead of fixing code | Scope checks in review phase; minimal-diff validation; test count assertions |
| **Cost blow-up** | Many iterations for simple tasks | Time and iteration budgets; cost tracking in observability layer |

#### What the Ralph Loop Built: The Production Engine

The production engine is **not itself a Ralph Loop** — it is a phased OODA pipeline that the Ralph Loop methodology produced. Where the raw Ralph pattern is an unstructured iterate-until-done loop, the production engine adds significant architectural structure:

**Phased pipeline**: Five specialized phases (triage → implement → review → validate → report) run sequentially, each with its own system prompt, tool restrictions, and trust boundaries. Most phases execute once and advance forward.

**OODA decision cycle per phase**: Each phase runs its own observe → plan → act → validate → reflect cycle:

```
OBSERVE → PLAN → ACT → VALIDATE → REFLECT → (advance, backtrack, or escalate)
```

**Bounded backtracking**: The only true iterative loop is the implement↔review cycle. When review rejects a fix, implement re-runs with the review feedback injected as context. All other phases are single-shot.

**Zero trust**: Phases validate independently — the review phase re-reads the issue and diff from scratch rather than trusting the implementation phase's summary. This is an adversarial validation pattern, not iterative refinement.

**Hard limits**: A configurable **iteration cap** (default: 10) and **time budget** (default: 30 minutes) enforce escalation to a human when the engine gets stuck. A raw Ralph Loop would iterate indefinitely.

The engine preserves the core Ralph Loop insight — **failures are data, not dead ends** — but implements it through structured phase backtracking rather than unstructured retry. Failed implementation approaches are recorded and fed into the next attempt as context to avoid, and review rejection feedback is injected as specific guidance for the next implementation cycle.

### 1.2 Development Methodology vs Production Engine

| | Ralph Loop (development methodology) | Production Engine (what it built) |
|---|---|---|
| **Where it runs** | Local laptop (Cursor/Claude Code/OpenCode) | GitHub Actions |
| **What it is** | An unstructured iterate-until-done loop | A phased OODA pipeline with zero-trust validation |
| **Purpose** | Builds and maintains the production engine itself | Executes the bug fix workflow against target repos |
| **Loop structure** | Same prompt, evolving codebase, fresh context each run | 5 specialized phases with different prompts, tools, and trust boundaries |
| **Iteration** | Every run is a full retry | Only implement↔review iterates; other phases are single-shot |

This specification defines the **production engine**. The `prompt.md` file drives the **meta Ralph Loop** that builds and maintains it.

## 2. System Requirements

### 2.1 Functional Requirements

#### FR-1: Bug Fix Workflow (MVP)
- **FR-1.1**: Accept a GitHub issue URL as input trigger
- **FR-1.2**: Clone the target repository at the specified state
- **FR-1.3**: Triage the issue (classify severity, identify affected components, confirm it is a bug not a feature)
- **FR-1.4**: Reproduce the bug (run existing tests, attempt to write a failing test)
- **FR-1.5**: Implement a fix using LLM-driven code generation
- **FR-1.6**: Validate the fix (run tests, run linters, run any repo-specific checks)
- **FR-1.7**: Self-review the fix (correctness, intent alignment, security, style)
- **FR-1.8**: Open a pull request with structured description
- **FR-1.9**: Respond to review feedback (automated or human) iteratively
- **FR-1.10**: Detect and remediate CI failures
- **FR-1.11**: Escalate to human when judgment is required or iteration cap is reached
- **FR-1.12**: Produce a comparison report when run against a known-solved bug

#### FR-2: Visual Evidence and Reporting (3D Interactive)
- **FR-2.1**: Generate an interactive 3D visualization of the entire execution for every loop run, rendered in the browser using Three.js
- **FR-2.2**: The primary view is a 3D decision landscape showing the agent's journey: phase transitions, branching decisions, iterations, and outcomes rendered as a navigable 3D scene with camera controls (orbit, zoom, pan)
- **FR-2.3**: Every 3D object in the scene is clickable — clicking drills down into a detail panel showing human-readable information: what the AI was told to do, what it did, what it decided, what tools it used, and what happened as a result. No raw JSON, YAML, or code dumps — all content is formatted as plain-English narrative with syntax-highlighted code snippets where relevant
- **FR-2.4**: The 3D scene uses visual metaphors for the pipeline: phases as distinct platform layers, actions as objects on those platforms, data flow as animated particle connections between objects, and status encoded via color and lighting (green glow = success, amber = iteration, red = failure, blue = human escalation)
- **FR-2.5**: The system generates its own demo artifacts — no manual demo creation needed
- **FR-2.6**: Visual reports are published as GitHub Actions artifacts and optionally as GitHub Pages
- **FR-2.7**: Comparison view: side-by-side diff of agent fix vs human fix with annotations
- **FR-2.8**: The detail drill-down panels present LLM interactions as a readable conversation (what the agent was asked, what it answered, what it did next) — not raw API payloads
- **FR-2.9**: Report is a single self-contained HTML file with embedded Three.js, CSS, and data (no external dependencies, works offline)
- **FR-2.10**: The 3D scene includes a timeline scrubber that animates the execution chronologically — the viewer can watch the agent work step by step

#### FR-3: Observability
- **FR-3.1**: Structured JSON logging for every action with correlation IDs
- **FR-3.2**: Metrics collection (loop iterations, time per phase, LLM token usage, success/failure rates)
- **FR-3.3**: Full LLM conversation transcripts stored as artifacts
- **FR-3.4**: Action traces linking every code change to the reasoning that produced it
- **FR-3.5**: Dual-format output: machine-readable (JSON/OTLP) for agent consumption, human-readable (HTML reports) for people
- **FR-3.6**: All observability data available to the AI agents themselves for self-correction

#### FR-4: Integration Discovery
- **FR-4.1**: GitHub API (repos, issues, PRs, actions, status checks, CODEOWNERS)
- **FR-4.2**: Slack (read-only monitoring, notification posting)
- **FR-4.3**: GitLab (for orgs using GitLab, same workflow pattern)
- **FR-4.4**: Jira (issue reading, status updates, comment posting)
- **FR-4.5**: Google Docs/Slides (reading design docs, generating reports)
- **FR-4.6**: WebRCA (incident context)
- **FR-4.7**: Integration interface must be pluggable — new integrations added without modifying core loop logic
- **FR-4.8**: Agent-driven discovery: the engine can query available integrations and determine what context sources exist

#### FR-5: GitHub Actions Native
- **FR-5.1**: All production execution happens in GitHub Actions workflows
- **FR-5.2**: Workflows are reusable across any repository in an organization
- **FR-5.3**: The engine can trigger and monitor its own sub-workflows
- **FR-5.4**: Workflow artifacts are the primary storage for all outputs (logs, reports, traces)
- **FR-5.5**: Secrets management via GitHub Actions secrets and OIDC where possible

#### FR-6: Implement-First Workflow Execution
- **FR-6.1**: The engine completes all implementation and review iterations locally before pushing any changes or triggering CI workflows — no partial pushes
- **FR-6.2**: Only after the engine's internal validation passes (review approved, linters pass, local checks green) does it push the branch and create the PR
- **FR-6.3**: After PR creation, the engine monitors the target repo's CI workflow to completion — downloads test results, build logs, and status check outcomes
- **FR-6.4**: If CI fails, the engine pulls the failure details back into its context and enters a CI remediation loop: analyze failure → implement fix → re-review → push → re-monitor CI
- **FR-6.5**: The CI remediation loop has its own iteration cap (configurable, default: 3) and time budget, independent of the main implementation loop
- **FR-6.6**: CI failure analysis is structured: the engine categorizes failures (test failure, build error, lint violation, timeout, infrastructure flake) and applies different strategies for each
- **FR-6.7**: Infrastructure flakes (network timeouts, runner failures, service unavailability) trigger a CI re-run rather than a code change
- **FR-6.8**: If CI remediation exceeds its cap, the engine escalates to human with the full failure context attached to the PR as a comment

### 2.2 Non-Functional Requirements

#### NFR-1: Security (from fullsend threat model)
- **NFR-1.1**: All PR content treated as untrusted input — separation of data and instructions in all LLM calls
- **NFR-1.2**: Zero trust between loop phases — each phase validates independently, does not trust prior phase output
- **NFR-1.3**: Principle of least privilege — each phase gets only the permissions it needs
- **NFR-1.4**: No self-approval — the engine cannot merge its own PRs without human or independent agent approval
- **NFR-1.5**: Immutable agent policy — loop configuration cannot be modified through channels the loop operates on
- **NFR-1.6**: Fail closed — when uncertain, escalate to human rather than proceeding
- **NFR-1.7**: Full auditability — every action logged, attributable, reviewable
- **NFR-1.8**: Commit signing for provenance (GPG or sigstore/gitsign)

#### NFR-2: Provenance and Supply Chain
- **NFR-2.1**: Record which LLM model (name, version, provider) generated each code change
- **NFR-2.2**: Commit metadata includes model provenance
- **NFR-2.3**: All agent-generated artifacts include authorship attestation
- **NFR-2.4**: Compatible with SLSA and Enterprise Contract verification

#### NFR-3: Technology Agnosticism
- **NFR-3.1**: LLM provider is swappable (Gemini, Anthropic Claude, OpenAI, local models via Ollama)
- **NFR-3.2**: Agent runtime is swappable (direct API calls, OpenCode, Claude Code CLI, LangChain)
- **NFR-3.3**: Target repo language/stack agnostic — the engine works with Go, Python, React, shell, YAML
- **NFR-3.4**: Integration layer uses adapter pattern — each external system behind a common interface

#### NFR-4: Performance
- **NFR-4.1**: Single bug fix loop completes within 30 minutes (configurable)
- **NFR-4.2**: Visual report generation completes within 2 minutes
- **NFR-4.3**: Total GitHub Actions cost per bug fix attempt < $5 (target)

#### NFR-5: Neutral Observer and Agent Provenance
- **NFR-5.1**: A neutral observer process runs independently of the agent pipeline, in a separate GitHub Actions job with its own secret scope — the agent cannot access the observer's signing credentials
- **NFR-5.2**: The observer reconstructs the agent's execution timeline from artifacts (`execution.json`, `log.json`, transcripts) and cross-checks claims against evidence (diff consistency, action completeness, phase ordering, token plausibility, tool call integrity)
- **NFR-5.3**: The observer produces a signed agent provenance attestation in in-toto Statement v1 format, aligned with SLSA Build provenance structure, capturing: subject (commit SHA), model identity, system prompt digests, tool definitions, invocation parameters, materials, tool call summary, and cross-check results
- **NFR-5.4**: Attestation signing uses Sigstore keyless signing via GitHub Actions OIDC — the signing identity is bound to the observer job, not the agent job
- **NFR-5.5**: A configurable policy gate evaluates the signed attestation against rules (model allowlist, prompt template integrity, scope compliance, cross-check pass/fail) before the result is posted as a PR comment or used as a required status check
- **NFR-5.6**: The observer is post-hoc for MVP (runs after the agent finishes). Runtime interception (transparent proxy, syscall tracing) and network activity capture are future enhancements
- **NFR-5.7**: Attestation storage is via GitHub Actions artifacts for MVP. OCI registry attachment and `refs/attestations/` git namespace are future work

## 3. Architecture Overview

### 3.1 Component Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                         GitHub Actions                            │
│                                                                   │
│  ┌─── Job 1: agent ──────────────────────────────────────────┐   │
│  │  Secrets: GEMINI_API_KEY, GH_PAT, ANTHROPIC_API_KEY       │   │
│  │                                                            │   │
│  │  ┌──────────────── RL Bug Fix Engine ──────────────────┐  │   │
│  │  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────┐ │  │   │
│  │  │  │ Triage  │→│Implement│→│ Review  │→│ Validate │ │  │   │
│  │  │  └─────────┘ └─────────┘ └─────────┘ └──────────┘ │  │   │
│  │  │       ↑                                    │       │  │   │
│  │  │       └────────── REFLECT & ITERATE ───────┘       │  │   │
│  │  │                                                     │  │   │
│  │  │  ┌─────────────────────────────────────────────┐   │  │   │
│  │  │  │          Observability Layer                 │   │  │   │
│  │  │  │  Logger │ Tracer │ Metrics │ Transcripts    │   │  │   │
│  │  │  └─────────────────────────────────────────────┘   │  │   │
│  │  │                                                     │  │   │
│  │  │  ┌─────────────────────────────────────────────┐   │  │   │
│  │  │  │          Integration Layer                   │   │  │   │
│  │  │  │  GitHub │ Slack │ Jira │ LLM │ Discovery    │   │  │   │
│  │  │  └─────────────────────────────────────────────┘   │  │   │
│  │  └─────────────────────────────────────────────────────┘  │   │
│  │                                                            │   │
│  │  ┌────────────────────────────────────────────────────┐   │   │
│  │  │         Visualization Generator                     │   │   │
│  │  │  Decision Tree │ Action Map │ Comparison Report    │   │   │
│  │  └────────────────────────────────────────────────────┘   │   │
│  │                          │                                │   │
│  │                     ┌────▼────┐                           │   │
│  │                     │Artifacts│ ──── upload ────┐         │   │
│  │                     └─────────┘                 │         │   │
│  └─────────────────────────────────────────────────│─────────┘   │
│                                                    │              │
│  ┌─── Job 2: observer (needs: agent) ──────────────│─────────┐   │
│  │  Secrets: (none — uses OIDC for Sigstore)       │         │   │
│  │  Permissions: id-token: write, contents: read   │         │   │
│  │                                            ┌────▼────┐    │   │
│  │                                            │Download │    │   │
│  │                                            │Artifacts│    │   │
│  │                                            └────┬────┘    │   │
│  │  ┌──────────────── Neutral Observer ───────────┐│         │   │
│  │  │                                             ││         │   │
│  │  │  ┌──────────────┐  ┌─────────────────────┐ ││         │   │
│  │  │  │ Reconstruct  │→ │ Cross-Check Claims  │◄┘│         │   │
│  │  │  │ Timeline     │  │ vs Evidence          │  │         │   │
│  │  │  └──────────────┘  └──────────┬──────────┘  │         │   │
│  │  │                               │              │         │   │
│  │  │  ┌──────────────┐  ┌─────────▼──────────┐  │         │   │
│  │  │  │ Evaluate     │← │ Build & Sign       │  │         │   │
│  │  │  │ Policy       │  │ Attestation (OIDC) │  │         │   │
│  │  │  └──────┬───────┘  └────────────────────┘  │         │   │
│  │  └─────────│───────────────────────────────────┘         │   │
│  │            │                                              │   │
│  │       ┌────▼─────────────────┐                           │   │
│  │       │ Signed Attestation + │ ──── upload ───→ Artifacts│   │
│  │       │ Policy Result        │                           │   │
│  │       └──────────────────────┘                           │   │
│  └───────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 The Pipeline as the Agent

Rather than deploying N separate agent services that coordinate via side-channels, a single pipeline execution IS the agent. One execution encompasses all phases (triage, implement, review, validate, report) using specialized prompts and tools at each phase. This design choice:

- **Eliminates inter-agent trust problems** — there is one execution context, not multiple services trusting each other
- **Preserves zero-trust validation** — each phase independently validates (the review phase does not trust the implementation phase's self-assessment)
- **Uses the repo as coordinator** — branch protection, CODEOWNERS, and status checks coordinate the outcome, not agent-to-agent messaging
- **Is debuggable** — one linear execution trace instead of distributed system debugging
- **Maps to GitHub Actions** — one workflow, one execution, one set of artifacts

The phases achieve the same effect as specialized agents (triage agent, implementation agent, review agent) but within a single controlled loop rather than as separate services.

### 3.3 LLM Strategy for GitHub Actions

The production loop runs in GitHub Actions where Cursor/Claude Code desktop apps are not available. The LLM access strategy, in priority order:

1. **Direct API calls (recommended for MVP)**: Python script calls Gemini or Anthropic API directly. Simplest, most controllable, works in any CI environment. The API key is stored as a GitHub Actions secret.

2. **OpenCode CLI**: Open-source coding agent CLI that can run headlessly in CI. Supports multiple LLM backends. Good for the "agent runtime" layer if we want tool-use and file-editing capabilities built in.

3. **LangChain/LangGraph**: Framework for building agent loops with tool use. More control than OpenCode, more structure than raw API calls. Good if we need complex tool orchestration.

4. **Anthropic Claude API with tool use**: Claude's native tool-use API allows structured agent behavior (function calling, file operations) without needing a local agent runtime.

For MVP: **direct Gemini API calls** via Python. The `engine/integrations/llm.py` module abstracts the provider so we can swap to Anthropic, OpenCode, or others later.

## 4. Data Model

### 4.1 Loop Execution Record

```yaml
execution:
  id: "uuid"
  trigger:
    type: "github_issue" | "manual" | "scheduled"
    source_url: "https://github.com/org/repo/issues/123"
  target:
    repo: "org/repo"
    ref: "abc123"
    branch: "rl/fix-issue-123"
  config:
    max_iterations: 10
    time_budget_minutes: 30
    llm_provider: "gemini"
    llm_model: "gemini-2.5-pro"
  iterations:
    - number: 1
      phase: "triage"
      started_at: "2026-03-25T10:00:00Z"
      completed_at: "2026-03-25T10:02:30Z"
      observation: { ... }
      plan: { ... }
      actions: [ ... ]
      validation: { passed: true, details: "..." }
      reflection: { continue: true, next_phase: "implement" }
    - number: 2
      phase: "implement"
      # ...
  result:
    status: "success" | "failure" | "escalated" | "timeout"
    pr_url: "https://github.com/org/repo/pull/456"
    comparison:  # only for known-solved bugs
      human_diff_url: "..."
      agent_diff_url: "..."
      similarity_score: 0.85
      analysis: "..."
  artifacts:
    decision_tree: "decision-tree.html"
    action_map: "action-map.html"
    comparison_report: "comparison.html"
    full_log: "execution.json"
    llm_transcripts: "transcripts/"
```

### 4.2 Action Record

Every action taken by the engine is recorded:

```yaml
action:
  id: "uuid"
  iteration: 3
  phase: "implement"
  type: "file_edit" | "command_run" | "api_call" | "llm_query" | "pr_create" | "comment_post"
  timestamp: "2026-03-25T10:05:00Z"
  input:
    description: "Edit controller.go to add nil check"
    context_files: ["pkg/controller/reconciler.go"]
  output:
    success: true
    diff: "..."
    stdout: "..."
  llm_context:
    model: "gemini-2.5-pro"
    tokens_in: 15000
    tokens_out: 2000
    prompt_hash: "sha256:..."
  provenance:
    reasoning: "The nil pointer occurs because X is not checked before..."
    confidence: 0.9
```

### 4.3 Agent Provenance Attestation

The neutral observer produces a signed attestation for every agent execution. The format follows the in-toto Statement v1 specification with a custom predicate type aligned to SLSA Build provenance structure:

```yaml
attestation:
  _type: "https://in-toto.io/Statement/v1"
  subject:
    - name: "git+https://github.com/org/repo"
      digest:
        sha1: "<commit-sha-produced-by-agent>"
  predicateType: "https://rl-engine.dev/provenance/agent/v1"
  predicate:
    buildDefinition:
      buildType: "https://rl-engine.dev/AgentSynthesis/v1"
      externalParameters:
        issue_url: "https://github.com/org/repo/issues/123"
        config_overrides: { ... }
      internalParameters:
        engine_version: "0.8.0"
        workflow_run_id: "23618411249"
        runner_os: "ubuntu-22.04"
      resolvedDependencies:
        - uri: "git+https://github.com/org/repo@<base-commit>"
          digest: { sha1: "<base-commit>" }
        - uri: "prompt://templates/prompts/triage.md"
          digest: { sha256: "<hash>" }
        - uri: "prompt://templates/prompts/implement.md"
          digest: { sha256: "<hash>" }
        - uri: "prompt://templates/prompts/review.md"
          digest: { sha256: "<hash>" }
        - uri: "prompt://templates/prompts/validate.md"
          digest: { sha256: "<hash>" }
    runDetails:
      builder:
        id: "https://github.com/org/rl-bug-fix-full-send/.github/workflows/rl-engine.yml"
      metadata:
        invocationId: "<workflow-run-url>"
        startedOn: "2026-03-28T10:00:00Z"
        finishedOn: "2026-03-28T10:25:00Z"
      models:
        - id: "gemini-2.5-pro"
          provider: "google"
          api_version: "v1"
          temperature: 0.2
          total_calls: 7
          total_tokens_in: 45000
          total_tokens_out: 12000
      toolDefinitions:
        digest: { sha256: "<hash-of-tool-config>" }
        tools: ["file_read", "file_write", "shell_run", "git_diff", "github_api"]
      crossCheckResults:
        diff_consistency: { passed: true }
        action_completeness: { passed: true }
        phase_ordering: { passed: true }
        token_plausibility: { passed: true }
        tool_call_integrity: { passed: true }
```

## 5. Phase Specifications

### 5.1 Triage Phase

**Goal**: Understand the bug, confirm it is a bug (not a feature request), identify affected components.

**Inputs**: Issue URL, repo clone

**Actions**:
1. Read the issue body and comments (treat as untrusted input — separate from system prompt)
2. Classify: is this a bug, feature request, or ambiguous?
3. If ambiguous or feature request → escalate to human, do not proceed
4. Identify affected files/components from issue description and repo structure
5. Assess severity and complexity
6. Attempt to reproduce (run existing tests, look for related test files)
7. Write a failing test if none exists that captures the bug

**Outputs**: Triage report (classification, affected components, reproduction status, failing test if created)

**Validation**: Triage report is coherent, classification is justified, affected files exist in repo

### 5.2 Implementation Phase

**Goal**: Fix the bug.

**Inputs**: Triage report (including detected repository stack), repo clone, failing test (if available)

**Actions**:
1. Read triage report (validate independently — do not blindly trust triage conclusions)
2. Inherit repository stack detection from triage (language, test command, lint command). Only re-detect as fallback if triage did not provide stack information.
3. Analyze the identified files and surrounding code
4. Formulate a fix strategy
5. Implement the fix
6. Run tests locally using the detected stack's test command (e.g., `go test` for Go, `pytest` for Python)
7. If tests fail → iterate on the fix (inner loop within the implementation phase)
8. Run linters using the detected stack's lint command (e.g., `golangci-lint` for Go, `ruff` for Python)

**Outputs**: Code changes (diff), test results, lint results

**Validation**: All existing tests pass, new test (if written in triage) passes, linters pass

**Cross-phase context**: Triage serializes its `RepoStack` detection (language, test_command, lint_command, detected_from, confidence) into `PhaseResult.artifacts["detected_stack"]`. Implement and validate phases read this from `prior_results` before running any tests or linters. This prevents each phase from independently (and potentially incorrectly) re-detecting the repository language.

### 5.3 Review Phase

**Goal**: Independent assessment of the fix quality, correctness, and safety.

**Inputs**: Code diff, triage report, test results (validated independently)

**Actions**:
1. Analyze the diff for correctness (logic errors, edge cases, test adequacy)
2. Check intent alignment (does the fix address the bug described in the issue, and nothing more?)
3. Security review (does the fix introduce any security concerns?)
4. Style check (does it follow repo conventions?)
5. Scope check (is this truly a bug fix or has it morphed into a feature?)
6. If issues found → feed back to implementation phase for another iteration

**Outputs**: Review findings (approve, request changes, or block)

**Validation**: Review assessment is justified with specific code references

### 5.4 Validation Phase

**Goal**: Final verification before PR submission.

**Inputs**: Fixed code, review approval, test results

**Actions**:
1. Run full test suite one more time
2. Run CI-equivalent checks (linters, type checkers, build)
3. Verify the fix is minimal (no unnecessary changes)
4. Generate PR description with structured format
5. Create the PR
6. Monitor CI on the PR
7. If CI fails → feed back to implementation for remediation

**Outputs**: Open PR, CI status

**Validation**: PR is open, CI is green (or failure reason is understood)

### 5.5 Report Phase

**Goal**: Generate all visual and textual evidence of the execution.

**Inputs**: Full execution record (all iterations, all actions, all logs)

**Actions**:
1. Generate decision tree visualization (HTML/SVG)
2. Generate action map visualization (HTML/SVG)
3. If comparison mode: generate side-by-side diff with analysis
4. Package all artifacts (logs, transcripts, visualizations)
5. Upload as GitHub Actions artifacts
6. Optionally publish to GitHub Pages

**Outputs**: Interactive HTML report, comparison report (if applicable), artifact bundle

### 5.6 Observer Phase (Neutral Observer — Separate Job)

**Goal**: Independently verify the agent's execution and produce a signed provenance attestation that the agent cannot forge.

**Execution context**: Runs in a separate GitHub Actions job (`observer`) that depends on the agent job. The observer job has its own secret scope (OIDC identity for Sigstore signing) and cannot access the agent's secrets. The agent cannot access the observer's signing credentials.

**Inputs**: Agent artifacts downloaded from the prior job (`execution.json`, `log.json`, `transcripts/`, `progress.md`, git branch with agent commits)

**Actions**:
1. **Reconstruct execution timeline** — parse the agent's artifacts to build an independent timeline of LLM calls, file operations, shell commands, and phase transitions
2. **Cross-check claims vs evidence**:
   - Diff consistency: verify the git diff on the branch matches the `file_changes` recorded in `execution.json`
   - Action completeness: every file modified in the diff has a corresponding action record
   - Test execution: if tests were claimed to pass, verify test output artifact exists and is consistent
   - Phase ordering: phases executed in declared order, no skipped validation
   - Token plausibility: LLM token counts in the execution record are plausible (not fabricated)
   - Tool call integrity: every tool call in transcripts has a corresponding action record
3. **Build agent provenance attestation** — produce an in-toto Statement v1 attestation with predicate type `https://rl-engine.dev/provenance/agent/v1`, capturing: subject (commit SHA), builder identity, model identity, system prompt digests, tool definitions, invocation parameters, materials, tool call summary, and cross-check results
4. **Sign the attestation** — use Sigstore keyless signing via GitHub Actions OIDC (the signing identity is bound to the observer job)
5. **Evaluate policy** — apply configurable rules against the attestation: model allowlist, prompt template integrity (digest comparison), scope compliance (only modified files related to the issue), cross-check pass/fail, iteration limit compliance
6. **Publish results** — upload signed attestation and policy result as workflow artifacts; optionally post policy summary as a PR comment

**Outputs**: Signed agent provenance attestation (JSON), policy evaluation result (pass/fail with details), optional PR comment

**Validation**: Attestation conforms to in-toto Statement v1 schema; signature is verifiable via Sigstore transparency log; policy evaluation produces a definitive pass/fail

**Trust model**: The observer does not trust the agent's self-reported data. It reconstructs what happened from artifacts and verifies consistency. The attestation is signed with a credential the agent never had access to. A compromised agent cannot forge a valid attestation because it cannot obtain the observer's OIDC-issued signing certificate.

### 5.7 CI Remediation Phase (Implement-First Workflow Execution)

**Goal**: After the engine completes all internal implementation and review cycles, push the changes, trigger the target repo's CI, monitor results, and remediate any CI failures.

**Precondition**: The engine does NOT push changes or create a PR until implementation is internally approved. All implement-review iterations happen locally first. Only a fully reviewed and locally validated change gets pushed.

**Inputs**: Approved code changes (locally validated), PR creation details, CI workflow configuration

**Actions**:
1. Push the branch and create the PR (this triggers the target repo's CI pipeline)
2. Poll CI status via GitHub API (`check_ci_status`) until all required checks complete or timeout
3. Download CI results: test output, build logs, lint results, status check details
4. If CI passes → proceed to report phase, execution is successful
5. If CI fails → categorize the failure:
   - **Test failure**: extract failing test names and error messages, feed into implementation phase for targeted fix
   - **Build error**: extract compiler/build errors, feed into implementation phase
   - **Lint violation**: extract lint errors, feed into implementation phase
   - **Infrastructure flake** (network timeout, runner failure, service unavailability): trigger a CI re-run via GitHub API, do not modify code
   - **Timeout**: check if tests are known to be slow, escalate if repeated
6. For code failures: re-enter implement → review → validate → push cycle with CI failure context injected
7. After pushing the fix, return to step 2 (monitor CI again)

**Iteration limits**:
- CI remediation loop has its own iteration cap (default: 3, configurable via `ci_remediation.max_iterations`)
- CI remediation has its own time budget (default: 15 minutes, configurable via `ci_remediation.time_budget_minutes`)
- These are independent of the main implementation loop limits

**Outputs**: Green CI on the PR (or escalation with full failure context)

**Validation**: All required CI status checks pass, or failure is escalated with actionable context

## 6. Visualization Specification (3D Interactive Report)

The report is a single self-contained HTML file with embedded Three.js, CSS, and execution data. No external dependencies — it works offline, from a local file or as a GitHub Pages deployment. The visualization prioritizes human understanding over data completeness: every piece of information is presented as readable narrative, not raw machine formats.

### 6.1 3D Execution Landscape (Primary View)

The main visualization is a Three.js 3D scene showing the agent's entire execution as a navigable landscape:

**Scene structure**:
- **Platform layers** — each pipeline phase (triage, implement, review, validate, report) is a distinct floating platform at a different elevation, arranged in execution order. Platforms are connected by glowing bridge paths showing phase transitions.
- **Action objects** — each action the agent took (LLM call, file read, file write, test run, API call, git operation) is rendered as a distinct 3D object on its phase's platform. Object shape encodes type: polyhedra for LLM calls, cubes for file operations, cylinders for test/command runs, spheres for API calls.
- **Data flow connections** — animated particle streams between objects show data flow: which file read informed which LLM call, which LLM response produced which file write. Particle color indicates data type (code = cyan, reasoning = gold, test results = green/red).
- **Status encoding** — objects glow with status colors: green (success), amber (iteration/retry), red (failure/error), blue (escalation to human). Failed paths pulse. The overall scene lighting shifts warm (success) to cool (problems) based on execution outcome.
- **Decision branch points** — where the pipeline branched (implement-review loop, escalation checks), the path visually forks with the taken path illuminated and the not-taken path dimmed.

**Camera and navigation**:
- Orbit controls (click-drag to rotate, scroll to zoom, right-drag to pan)
- Preset camera positions: overview (sees entire landscape), per-phase close-up, follow-the-path animation
- Minimap in corner showing top-down view with current camera position

**Timeline scrubber**:
- A timeline bar at the bottom of the viewport shows wall-clock time of the execution
- Dragging the scrubber animates the scene: objects appear as they happened, connections light up in sequence, the camera follows the action
- Play/pause button for automatic playback at configurable speed
- Phase markers on the timeline for quick jumping

### 6.2 Detail Drill-Down (Click-to-Inspect)

Clicking any 3D object opens a slide-in detail panel (overlaid on the 3D scene, not replacing it). The panel presents **human-readable narrative**, not raw data:

**For LLM calls**:
- "What the agent was told" — the system prompt and context summarized in plain English (e.g., "The agent was asked to review the diff for correctness, check if the fix addresses the nil pointer described in issue #123, and verify no security issues were introduced")
- "What it decided" — the LLM's response summarized as a narrative (e.g., "The agent identified that the fix correctly adds a nil check on line 42 of reconciler.go but noted the error message could be more descriptive. Verdict: approve with one suggestion.")
- "Key reasoning" — extracted reasoning/chain-of-thought in the agent's own words, formatted as readable paragraphs
- "By the numbers" — tokens used, model name, response time (small footer, not the focus)

**For file operations**:
- "What was read/written" — file path, with a syntax-highlighted snippet of the relevant code (not the entire file)
- "Why" — the reasoning that led to this operation, traced back to the LLM call that requested it
- "What changed" — for writes, a clean diff view with before/after

**For test and command runs**:
- "What was run" — the command, in plain text
- "What happened" — pass/fail with the relevant output excerpt (not the full 500-line test log — just the failures or the summary line)
- "What the agent did about it" — if tests failed, what the agent decided to do next

**For phase transitions**:
- "Why did the agent move to the next phase?" — the reflect step's conclusion in plain English
- "What was carried forward?" — key context that flowed to the next phase

### 6.3 Narrative Summary (Landing View)

Before the user enters the 3D scene, the report opens with a narrative summary page:
- **One-paragraph story**: "The agent received issue #123 (nil pointer in reconciler). It identified the bug in `pkg/controller/reconciler.go`, implemented a nil check, passed self-review on the second attempt after fixing an error message, and opened PR #456. Total time: 8 minutes across 4 phases."
- **Key metrics cards**: total time, iterations, LLM calls, files modified, tests run, final status
- **Phase timeline**: horizontal bar chart showing time spent in each phase
- "Enter 3D View" button that transitions into the full scene

### 6.4 Comparison Report

When running against a known-solved bug:
- **Side-by-side diff**: agent fix vs human fix, syntax-highlighted
- **Structural comparison**: same files changed? same approach?
- **Test comparison**: did both fixes make the same tests pass?
- **Annotation**: AI-generated analysis of similarities and differences
- **Metrics**: lines changed, files touched, complexity delta
- **3D overlay**: in the 3D scene, human-fix objects appear as ghost outlines alongside agent objects for visual comparison

### 6.5 Design Principles for the Report

1. **No raw JSON, YAML, or API payloads** — every piece of data is transformed into human-readable narrative before display. The execution.json is the data source but is never shown to the user.
2. **Progressive disclosure** — the landing page tells the story in one paragraph. The 3D scene shows the structure. Clicking objects reveals the details. The user controls how deep they go.
3. **Accurate, not decorative** — every visual element maps to real execution data. Object positions, sizes, colors, and connections are all data-driven. The 3D scene is a faithful representation of what happened, not a generic animation.
4. **Works offline** — single HTML file, no CDN dependencies. Three.js is bundled inline (minified). Execution data is embedded as a JavaScript object.
5. **Performant** — executions with up to 200 actions should render smoothly at 60fps on a standard laptop. Level-of-detail rendering for larger executions.

## 7. Golden Principles

These are enforced mechanically (via linters, tests, and the loop itself):

1. **Every action is logged** — no silent operations. If it happened, it is in the execution record.
2. **Every decision is traceable** — from the final PR back to the issue, through every LLM call and reasoning step.
3. **Prompts never mix trusted and untrusted content** — system instructions and user/issue content are always separated with clear delimiters.
4. **Phases validate independently** — the review phase re-reads the code and the issue; it does not trust the implementation phase's summary.
5. **Iterations are bounded** — max iterations and time budgets are enforced. No runaway loops.
6. **Escalation is not failure** — escalating to a human is a valid and expected outcome.
7. **The repo is the coordinator** — branch protection and CODEOWNERS make the merge decision, not the loop.
8. **Provenance is automatic** — model name, version, and prompt hash are recorded for every LLM call.
9. **Demos are a byproduct** — the system generates its own visual evidence as part of normal operation, not as a separate effort.
10. **Configuration is declarative** — loop behavior is configured via YAML, not hardcoded.
11. **Attestation is independent** — a neutral observer, running in an isolated execution context with its own signing credentials, independently verifies and attests the agent's execution. The agent cannot forge its own attestation.

## 8. Configuration Schema

```yaml
# .rl-config.yaml — placed in target repo or provided at trigger time
loop:
  max_iterations: 10
  time_budget_minutes: 30
  escalation:
    on_iteration_cap: "human"
    on_time_budget: "human"
    on_review_block_after: 3  # iterations

llm:
  provider: "gemini"  # gemini | anthropic | openai | ollama | opencode
  model: "gemini-2.5-pro"
  temperature: 0.2
  max_tokens: 8192
  fallback_provider: "anthropic"
  fallback_model: "claude-sonnet-4-20250514"

phases:
  triage:
    enabled: true
    classify_bug_vs_feature: true
    attempt_reproduction: true
    write_failing_test: true
  implement:
    max_inner_iterations: 5
    run_tests_after_each_edit: false
    test_execution_mode: "disabled"  # "disabled" | "opportunistic" | "required"
    run_linters: true
  review:
    correctness: true
    intent_alignment: true
    security: true
    style: true
    scope_check: true
  validate:
    full_test_suite: false
    test_execution_mode: "disabled"  # "disabled" | "opportunistic" | "required"
    ci_equivalent: true
    minimal_diff_check: true

ci_remediation:
  enabled: true
  max_iterations: 3
  time_budget_minutes: 15
  ci_poll_interval_seconds: 30
  ci_poll_timeout_minutes: 20
  rerun_on_infrastructure_flake: true
  max_flake_reruns: 2
  failure_categories:
    test_failure: "remediate"      # re-enter implement loop with failure context
    build_error: "remediate"       # re-enter implement loop with failure context
    lint_violation: "remediate"    # re-enter implement loop with failure context
    infrastructure_flake: "rerun"  # trigger CI re-run, do not modify code
    timeout: "escalate"            # escalate to human

reporting:
  visualization_engine: "threejs"  # threejs (3D) | d3 (legacy 2D, for compatibility)
  decision_tree: true
  action_map: true
  narrative_summary: true
  timeline_scrubber: true
  comparison_mode: false  # set true for known-solved bugs
  publish_to_pages: false
  artifact_retention_days: 30
  detail_format: "narrative"  # narrative (human-readable) | raw (JSON, for debugging only)

integrations:
  github:
    enabled: true
  slack:
    enabled: false
    channel: ""
  jira:
    enabled: false
    project: ""

security:
  commit_signing: true
  signing_method: "gitsign"  # gpg | gitsign
  provenance_recording: true
  untrusted_content_delimiter: "--- UNTRUSTED CONTENT BELOW ---"

observer:
  enabled: true
  signing_method: "sigstore"  # sigstore | cosign-key | none (for local testing)
  policy_file: "templates/policies/default.yaml"
  cross_checks:
    diff_consistency: true
    action_completeness: true
    phase_ordering: true
    token_plausibility: true
    tool_call_integrity: true
  model_allowlist:
    - "gemini-2.5-pro"
    - "claude-sonnet-4-20250514"
  prompt_template_digests: {}  # populated at build/release time with known-good SHA-256 digests
  post_policy_result_to_pr: true
  fail_on_policy_violation: false  # when true, a policy failure marks the workflow as failed
```

## 9. API and Interface Contracts

### 9.1 Workflow Trigger Interface

The GitHub Actions workflow accepts these inputs:

```yaml
inputs:
  issue_url:
    description: "GitHub issue URL to process"
    required: true
  target_ref:
    description: "Git ref to start from (default: HEAD of default branch)"
    required: false
  comparison_ref:
    description: "Git ref containing the human fix (for comparison mode)"
    required: false
  config_override:
    description: "YAML config overrides (inline)"
    required: false
```

### 9.2 Integration Adapter Interface

Every integration implements:

```python
class IntegrationAdapter(Protocol):
    name: str
    
    async def discover(self) -> dict:
        """Return capabilities and available resources."""
        ...
    
    async def read(self, resource_id: str) -> dict:
        """Read a resource (issue, message, document)."""
        ...
    
    async def write(self, resource_id: str, content: dict) -> dict:
        """Write to a resource (comment, status, label)."""
        ...
    
    async def search(self, query: str) -> list[dict]:
        """Search for resources matching a query."""
        ...
```

### 9.3 LLM Provider Interface

```python
class LLMProvider(Protocol):
    async def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        ...

    async def complete_with_tools(
        self,
        system_prompt: str,
        messages: list[dict],
        tools: list[dict],
        tool_executor: Callable,
    ) -> LLMResponse:
        ...
```

## 10. Acceptance Criteria for MVP

1. Given a GitHub issue URL for a known-solved bug in a forked Konflux repo:
   - The engine triages the issue correctly (identifies it as a bug, finds affected components)
   - The engine implements a fix that makes the existing test suite pass
   - The engine opens a PR with a structured description
   - The engine produces an interactive decision tree visualization
   - The engine produces a comparison report against the human fix
   - The entire execution completes within 30 minutes
   - All actions are logged with full provenance

2. The engine runs entirely in GitHub Actions (no local dependencies)

3. The engine can be triggered manually via `workflow_dispatch` and programmatically via API

4. Visual reports are accessible as GitHub Actions artifacts

5. LLM provider can be swapped by changing configuration (tested with at least Gemini and one fallback)
