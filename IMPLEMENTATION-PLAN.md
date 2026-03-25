# Implementation Plan

Phased build plan. Each phase produces usable, testable output. Phases are designed to be completable within the meta ralph loop.

## Phase 0: Foundation (build first)

### 0.1 Python Package Setup
- `pyproject.toml` with dependencies: `google-genai`, `anthropic`, `httpx`, `pyyaml`, `jinja2`, `rich`
- Project structure under `engine/`
- `Makefile` with `lint`, `test`, `fmt` targets
- `ruff.toml` for linting (match fullsend config: py312, line-length 100)

### 0.2 LLM Provider Abstraction
- `engine/integrations/llm.py` — `LLMProvider` protocol and implementations
- `GeminiProvider` — wraps `google-genai` SDK
- `AnthropicProvider` — wraps `anthropic` SDK
- `MockProvider` — returns canned responses for testing
- Provider selection from config YAML

### 0.3 Structured Logging and Tracing
- `engine/observability/logger.py` — structured JSON logger with correlation IDs
- `engine/observability/tracer.py` — action recording (input, output, timing, LLM context)
- `engine/observability/metrics.py` — counters and gauges (iterations, tokens, time per phase)
- All observability writes to a single `execution.json` file that accumulates during the run

### 0.4 Configuration System
- `engine/config.py` — load and validate `.rl-config.yaml`
- Default config embedded in package
- Override mechanism for workflow inputs
- Schema validation with clear error messages

### 0.5 Tool Executor
- `engine/tools/executor.py` — sandboxed tool execution
- Tools: `file_read`, `file_write`, `file_search`, `shell_run`, `git_diff`, `git_commit`, `github_api`
- Each tool execution is logged via the tracer
- Shell commands run with configurable timeout and output capture

**Deliverable**: A Python package that can call an LLM, execute tools, and produce structured logs. Testable with unit tests against the MockProvider.

## Phase 1: Core Loop Engine

### 1.1 Loop Orchestrator
- `engine/loop.py` — the Ralph Loop engine
- Implements: OBSERVE → PLAN → ACT → VALIDATE → REFLECT cycle
- Enforces iteration cap and time budget
- Manages phase transitions (triage → implement → review → validate → report)
- Handles escalation (writes escalation record with full context)

### 1.2 Phase Framework
- `engine/phases/base.py` — base phase class with common interface
- Each phase: `observe()`, `plan()`, `act()`, `validate()`, `reflect()`
- Phase-specific system prompts loaded from `templates/prompts/`
- Phase-specific tool sets (implementation phase gets file_write; review phase does not)

### 1.3 Triage Phase Implementation
- `engine/phases/triage.py`
- Read issue (via GitHub API)
- Classify bug vs feature vs ambiguous
- Identify affected components (grep + LLM analysis of repo structure)
- Attempt reproduction (find and run related tests)
- Write failing test if possible

### 1.4 Implementation Phase
- `engine/phases/implement.py`
- Read triage output AND re-read the issue independently
- Analyze affected code
- Generate fix
- Run tests after each edit (inner iteration loop)
- Run linters

### 1.5 Review Phase
- `engine/phases/review.py`
- Re-read the issue and the diff independently
- Correctness check
- Intent alignment check
- Security check
- Scope check (bug fix vs feature creep)
- Produce structured review findings

### 1.6 Validation Phase
- `engine/phases/validate.py`
- Run full test suite
- Run CI-equivalent checks
- Verify minimal diff
- Create PR via GitHub API
- Monitor initial CI status

**Deliverable**: A working loop engine that can process a bug issue end-to-end on a local clone. Testable by running against a prepared test repo with a known bug.

## Phase 2: GitHub Actions Integration

### 2.1 Main Workflow
- `.github/workflows/ralph-loop.yml`
- Triggered by `workflow_dispatch` with `issue_url` input
- Sets up Python environment
- Clones target repo
- Runs the loop engine
- Uploads artifacts (logs, reports)

### 2.2 Self-Monitoring
- The workflow can check its own status via GitHub API
- If a sub-step fails, the loop can read the failure output and react
- Workflow timeout aligned with loop time budget

### 2.3 Secret Management
- `GEMINI_API_KEY` — for LLM access
- `GH_PAT` — for creating PRs in target repos (needs `repo` scope)
- `ANTHROPIC_API_KEY` — fallback LLM
- Secrets are passed as environment variables, never logged

### 2.4 Fork and Rollback Script
- `scripts/setup-fork.sh` — forks a Konflux repo, rolls back to before a specified commit
- Used to prepare the test scenario for MVP validation
- Outputs the fork URL and the issue URL for the loop to process

**Deliverable**: A GitHub Actions workflow that runs the loop engine against a target repo. Can be triggered manually. Produces downloadable artifacts.

## Phase 3: Visualization and Reporting

### 3.1 Report Generator
- `engine/visualization/report_generator.py` — reads `execution.json`, produces HTML
- Template-based: Jinja2 templates with embedded D3.js

### 3.2 Decision Tree Visualization
- `engine/visualization/decision_tree.py` — transforms execution log into tree data structure
- `templates/visual-report/decision-tree.js` — D3.js rendering
- Click-to-expand nodes showing LLM transcripts and action details

### 3.3 Action Map Visualization
- `engine/visualization/action_map.py` — transforms action log into layered map data
- `templates/visual-report/action-map.js` — D3.js rendering
- Layered by phase, objects represent actions, connections show data flow

### 3.4 Comparison Report
- `engine/visualization/comparison.py` — generates side-by-side diff view
- Shows agent fix vs human fix
- Calculates similarity metrics
- AI-generated analysis of differences

### 3.5 Report Publishing
- Upload as GitHub Actions artifacts
- Optional GitHub Pages deployment via workflow

**Deliverable**: Interactive HTML reports generated for every loop execution. Decision tree, action map, and comparison view all functional with click-to-explore.

## Phase 4: Integration Layer

### 4.1 GitHub Integration (enhanced)
- `engine/integrations/github.py` — full GitHub API adapter
- Create PRs, post comments, read issues, manage labels, check CI status
- Commit signing via gitsign

### 4.2 Slack Integration
- `engine/integrations/slack.py` — post notifications, read channel history
- Notification on loop completion (success, failure, escalation)
- Read-only channel monitoring for context (with injection guards)

### 4.3 Jira Integration
- `engine/integrations/jira.py` — read issues, post comments, update status
- Can use Jira issues as trigger source (alternative to GitHub issues)

### 4.4 Discovery Service
- `engine/integrations/discovery.py` — enumerate available integrations
- Auto-detect what's configured (which secrets are set, which APIs respond)
- Provide integration catalog to the LLM for context gathering

**Deliverable**: Pluggable integration layer. GitHub is fully functional. Slack and Jira are functional for basic operations. Discovery service can enumerate available integrations.

## Phase 5: Hardening and Testing

### 5.1 Prompt Injection Testing
- Test suite with known injection payloads in issue bodies and PR descriptions
- Verify the engine does not follow injected instructions
- Regression tests for each injection vector

### 5.2 Loop Behavior Testing
- Test iteration cap enforcement
- Test time budget enforcement
- Test escalation behavior
- Test phase validation independence

### 5.3 End-to-End Testing
- Test against 3+ known-solved Konflux bugs
- Compare agent fixes against human fixes
- Measure success rate, fix quality, execution time

### 5.4 Security Audit
- Verify commit signing works
- Verify provenance recording
- Verify no secrets in logs or artifacts
- Verify untrusted content separation in all LLM calls

**Deliverable**: Comprehensive test suite. Validated against real Konflux bugs. Security properties verified.

## Phase 6: Self-Improvement Infrastructure (stretch)

### 6.1 Golden Principles Enforcement
- Linter rules that enforce the golden principles (every action logged, every decision traceable)
- CI checks that verify structured logging compliance

### 6.2 Deterministic Tool Extraction
- When the loop repeatedly uses the LLM for the same kind of decision, detect the pattern
- Propose extracting it into a deterministic tool (shell script, Python function)
- Open a PR for the extraction

### 6.3 Background Quality Scans
- Periodic scans of the engine's own codebase for principle violations
- Auto-generate refactoring PRs for deviations

**Deliverable**: Self-improvement loop that watches the engine's own behavior and proposes improvements.

## Build Order Dependency Graph

```
Phase 0.2 (LLM) ──┐
Phase 0.3 (Logs) ──┤
Phase 0.4 (Config)─┼─→ Phase 1.1 (Loop) ─→ Phase 1.2 (Phases) ─→ Phase 1.3-1.6 (Each Phase)
Phase 0.5 (Tools) ─┘                                                       │
                                                                            ▼
                                                              Phase 2 (GitHub Actions)
                                                                            │
                                                                            ▼
                                                              Phase 3 (Visualization)
                                                                            │
                                                                            ▼
                                                              Phase 4 (Integrations)
                                                                            │
                                                                            ▼
                                                              Phase 5 (Hardening)
                                                                            │
                                                                            ▼
                                                              Phase 6 (Self-Improvement)
```

## Timeline Estimate (for meta ralph loop)

| Phase | Effort | Depends on |
|-------|--------|------------|
| Phase 0 | 2-3 loop sessions | Nothing |
| Phase 1 | 3-5 loop sessions | Phase 0 |
| Phase 2 | 1-2 loop sessions | Phase 1 |
| Phase 3 | 2-3 loop sessions | Phase 1 |
| Phase 4 | 2-3 loop sessions | Phase 2 |
| Phase 5 | 2-3 loop sessions | Phase 1-4 |
| Phase 6 | 2-3 loop sessions | Phase 5 |

A "loop session" is one sitting where you run the meta ralph loop to completion on a phase or sub-phase.

## Operating Rules

- **One phase at a time.** Each phase's deliverables must be working and tested before the next.
- **`make check` after every change.** Lint + test must pass. Zero tolerance for lint warnings.
- **Tests prove correctness.** Every module in `engine/` gets a corresponding test in `tests/`. Use `MockProvider` for LLM interactions. If you say it's done, the tests must prove it.
- **Don't invent requirements.** Build what SPEC.md says. If ambiguous, check ARCHITECTURE.md, then `../fullsend/docs/`. If still unclear, implement the simplest version and add a `# TODO: clarify` comment.
- **Config drives behavior.** Hard-coded values are failures. Everything configurable via `.rl-config.yaml` with sensible defaults.
- **Security is the foundation.** Every LLM call separates trusted/untrusted content. Every action logged. Every tool execution traceable. See SPEC.md §7 and ARCHITECTURE.md ADR-006.
- **Update docs as you build.** README must reflect current state after each phase.

## Handling Problems

- **LLM API error**: Retry with exponential backoff. If persistent, switch to fallback provider.
- **Test failure you can't fix**: Skip with `@pytest.mark.skip(reason="...")` and continue. Don't block progress on edge cases.
- **Ambiguous requirement**: SPEC.md → ARCHITECTURE.md → `../fullsend/docs/`. Simplest version wins.
- **Scope creep**: If it's not in the spec for the current phase, note it as future work and move on.
