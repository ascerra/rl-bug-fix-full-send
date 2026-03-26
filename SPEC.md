# Technical Specification: RL Bug Fix Full Send

## 1. Overview

**RL Bug Fix Full Send** is an agentic SDLC engine that uses iterative Ralph Loops to autonomously triage, implement, review, test, and report on bug fixes in GitHub-hosted repositories. The system runs exclusively in GitHub Actions, produces interactive visual evidence of every decision and action, and is designed to eventually drive the entire software development lifecycle for a GitHub organization.

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

#### Our Adaptation: The Production Ralph Loop

RL Bug Fix Full Send layers structure onto the raw Ralph pattern for production use in GitHub Actions. Our loop follows a phased execution model:

```
OBSERVE → PLAN → ACT → VALIDATE → REFLECT → (repeat or escalate)
```

Each iteration:
1. **Observe** — gather context (repo state, issue details, test results, CI output, prior iteration history)
2. **Plan** — determine what to do next based on observations and goals
3. **Act** — execute the plan (write code, run tests, call APIs, create PRs)
4. **Validate** — check if the action achieved the goal (tests pass, review feedback addressed, CI green)
5. **Reflect** — assess progress, decide whether to iterate, escalate to human, or declare done

The loop has a configurable **iteration cap** (default: 10) and a **time budget** (default: 30 minutes). If the cap or budget is exceeded without completion, the loop escalates to a human with full context of what was attempted.

Where the raw Ralph pattern uses a single prompt and lets the agent decide everything, our production loop adds **specialized phases** (triage, implement, review, validate) with independent validation at each boundary. This prevents the metric-gaming and hallucination-amplification failures that plague unstructured loops, while preserving the core Ralph insight: **keep iterating with fresh context and concrete feedback until an objective success criterion is met**.

### 1.2 Two Levels of Ralph Loop

| Loop | Where it runs | Purpose |
|------|--------------|---------|
| **Meta Loop** | Local laptop (Cursor/Claude Code/OpenCode) | Builds and iterates on the production system itself |
| **Production Loop** | GitHub Actions | Executes the actual agentic SDLC workflow against target repos |

This specification defines the **Production Loop**. The `prompt.md` file drives the **Meta Loop**.

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

#### FR-2: Visual Evidence and Reporting
- **FR-2.1**: Generate an interactive HTML decision tree for every loop execution
- **FR-2.2**: Generate an interactive action map showing the sequence of actions taken
- **FR-2.3**: Every node in the visualization must be clickable to view underlying logs, diffs, or LLM transcripts
- **FR-2.4**: The system generates its own demo artifacts — no manual demo creation needed
- **FR-2.5**: Visual reports are published as GitHub Actions artifacts and optionally as GitHub Pages
- **FR-2.6**: Comparison view: side-by-side diff of agent fix vs human fix with annotations

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

## 3. Architecture Overview

### 3.1 Component Diagram

```
┌─────────────────────────────────────────────────────────┐
│                    GitHub Actions                         │
│  ┌───────────────────────────────────────────────────┐   │
│  │              Ralph Loop Engine                     │   │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌────────┐ │   │
│  │  │ Triage  │→│Implement│→│ Review  │→│Validate│ │   │
│  │  │ Phase   │ │ Phase   │ │ Phase   │ │ Phase  │ │   │
│  │  └─────────┘ └─────────┘ └─────────┘ └────────┘ │   │
│  │       ↑                                    │      │   │
│  │       └────────── REFLECT & ITERATE ───────┘      │   │
│  │                                                    │   │
│  │  ┌────────────────────────────────────────────┐   │   │
│  │  │           Observability Layer               │   │   │
│  │  │  Logger │ Tracer │ Metrics │ Transcripts   │   │   │
│  │  └────────────────────────────────────────────┘   │   │
│  │                                                    │   │
│  │  ┌────────────────────────────────────────────┐   │   │
│  │  │           Integration Layer                 │   │   │
│  │  │  GitHub │ Slack │ Jira │ LLM │ Discovery   │   │   │
│  │  └────────────────────────────────────────────┘   │   │
│  └───────────────────────────────────────────────────┘   │
│                                                           │
│  ┌───────────────────────────────────────────────────┐   │
│  │          Visualization Generator                   │   │
│  │  Decision Tree │ Action Map │ Comparison Report   │   │
│  └───────────────────────────────────────────────────┘   │
│                         │                                 │
│                    ┌────▼────┐                            │
│                    │Artifacts│                            │
│                    └─────────┘                            │
└─────────────────────────────────────────────────────────┘
```

### 3.2 The Loop as the Agent

Rather than deploying N separate agent services that coordinate via side-channels, the Ralph Loop IS the agent. A single loop execution encompasses all phases (triage, implement, review, test, report) using specialized prompts and tools at each phase. This design choice:

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

## 6. Visualization Specification

### 6.1 Decision Tree

An interactive SVG/HTML tree where:
- Each **node** represents a decision point (phase entry, branching logic, escalation check)
- Each **edge** represents the path taken with confidence scores
- **Color coding**: green (success), yellow (iteration needed), red (failure/escalation), blue (human intervention)
- **Click** any node to expand: shows the LLM prompt, response, reasoning, and resulting action
- **Zoom** in/out for large trees
- **Timeline** scrubber to step through the execution chronologically

### 6.2 Action Map

A 2.5D isometric or layered visualization showing:
- **Layers** represent phases (triage, implement, review, validate)
- **Objects** on each layer represent actions taken (file reads, edits, test runs, API calls)
- **Connections** between objects show data flow (which file read informed which edit)
- **Color/size** encode significance (larger = more tokens, brighter = higher confidence)
- **Click** any object to see the full action record (logs, diffs, LLM transcript)
- **Hover** for summary tooltip

### 6.3 Comparison Report

When running against a known-solved bug:
- **Side-by-side diff**: agent fix vs human fix
- **Structural comparison**: same files changed? same approach?
- **Test comparison**: did both fixes make the same tests pass?
- **Annotation**: AI-generated analysis of similarities and differences
- **Metrics**: lines changed, files touched, complexity delta

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

## 8. Configuration Schema

```yaml
# .rl-config.yaml — placed in target repo or provided at trigger time
ralph_loop:
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

reporting:
  decision_tree: true
  action_map: true
  comparison_mode: false  # set true for known-solved bugs
  publish_to_pages: false
  artifact_retention_days: 30

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
