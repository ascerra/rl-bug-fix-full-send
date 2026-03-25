# Implementation Plan

Phased build plan. Each phase produces usable, testable output. Phases are designed to be completable within the meta ralph loop.

## Phase 0: Foundation (build first)

### 0.1 Python Package Setup ✅
- `pyproject.toml` with dependencies: `google-genai`, `anthropic`, `httpx`, `pyyaml`, `jinja2`, `rich`
- Project structure under `engine/`
- `Makefile` with `lint`, `test`, `fmt` targets
- `ruff.toml` for linting (match fullsend config: py312, line-length 100)

### 0.2 LLM Provider Abstraction ✅
- `engine/integrations/llm.py` — `LLMProvider` protocol and implementations
- `GeminiProvider` — wraps `google-genai` SDK
- `AnthropicProvider` — wraps `anthropic` SDK
- `MockProvider` — returns canned responses for testing
- Provider selection from config YAML

### 0.3 Structured Logging and Tracing ✅
- `engine/observability/logger.py` — structured JSON logger with correlation IDs
- `engine/observability/tracer.py` — action recording (input, output, timing, LLM context)
- `engine/observability/metrics.py` — counters and gauges (iterations, tokens, time per phase)
- All observability writes to a single `execution.json` file that accumulates during the run

### 0.4 Configuration System ✅
- `engine/config.py` — load and validate `.rl-config.yaml`
- Default config embedded in package
- Override mechanism for workflow inputs
- Schema validation with clear error messages

### 0.5 Tool Executor ✅
- `engine/tools/executor.py` — sandboxed tool execution
- Tools: `file_read`, `file_write`, `file_search`, `shell_run`, `git_diff`, `git_commit`, `github_api`
- Each tool execution is logged via the tracer
- Shell commands run with configurable timeout and output capture

**Deliverable**: A Python package that can call an LLM, execute tools, and produce structured logs. Testable with unit tests against the MockProvider.

## Phase 1: Core Loop Engine

### 1.1 Loop Orchestrator ✅
- `engine/loop.py` — the Ralph Loop engine
- Implements: OBSERVE → PLAN → ACT → VALIDATE → REFLECT cycle
- Enforces iteration cap and time budget
- Manages phase transitions (triage → implement → review → validate → report)
- Handles escalation (writes escalation record with full context)
- Phase registry for pluggable phase implementations
- Review rejection backtracking (review → implement) with configurable threshold
- Soft failure retry within same phase
- ToolExecutor wired per-phase with allowed_tools filtering
- CLI wired to create and run the loop via asyncio

### 1.2 Phase Framework ✅
- `engine/phases/base.py` — base phase class with common interface
- Each phase: `observe()`, `plan()`, `act()`, `validate()`, `reflect()`
- Phase-specific system prompts loaded from `templates/prompts/`
- Phase-specific tool sets (implementation phase gets file_write; review phase does not)

### 1.3 Triage Phase Implementation ✅
- `engine/phases/triage.py`
- Read issue (via GitHub API)
- Classify bug vs feature vs ambiguous
- Identify affected components (grep + LLM analysis of repo structure)
- Attempt reproduction (find and run related tests)
- Write failing test if possible

### 1.4 Implementation Phase ✅
- `engine/phases/implement.py`
- Read triage output AND re-read the issue independently
- Analyze affected code
- Generate fix
- Run tests after each edit (inner iteration loop)
- Run linters

### 1.5 Review Phase ✅
- `engine/phases/review.py`
- Re-read the issue and the diff independently
- Correctness check
- Intent alignment check
- Security check
- Scope check (bug fix vs feature creep)
- Produce structured review findings

### 1.6 Validation Phase ✅
- `engine/phases/validate.py`
- Run full test suite
- Run CI-equivalent checks
- Verify minimal diff
- Create PR via GitHub API
- Monitor initial CI status

**Deliverable**: A working loop engine that can process a bug issue end-to-end on a local clone. Testable by running against a prepared test repo with a known bug.

## Phase 2: GitHub Actions Integration

### 2.1 Main Workflow ✅
- `.github/workflows/ralph-loop.yml`
- Triggered by `workflow_dispatch` with `issue_url` input
- Sets up Python environment
- Clones target repo
- Runs the loop engine with inline YAML config overrides (`--config-override`)
- Uploads artifacts (logs, reports)
- Input validation, graceful handling of missing visualization module
- CLI tested with 33 tests covering arg parsing, config overrides, and main() wiring

### 2.2 Self-Monitoring ✅
- `engine/workflow/monitor.py` — `WorkflowMonitor` class auto-created from GitHub Actions env vars
- Detects CI environment, queries workflow run status, finds failed steps via GitHub API
- `check_health()` one-call health check returns run status + step failures
- Loop integration: health checked each iteration, failures recorded via tracer, context in execution record
- `recommended_workflow_timeout()` aligns workflow timeout with engine time budget + 15m buffer
- CLI auto-creates monitor when `GITHUB_ACTIONS=true` via `WorkflowMonitor.from_environment()`
- 44 tests covering dataclass serialization, environment detection, API methods, loop integration

### 2.3 Secret Management ✅
- `engine/secrets.py` — `SecretManager` (env var loading, validation) + `SecretRedactor` (scrubs values from strings)
- `GEMINI_API_KEY` — for LLM access
- `GH_PAT` — for creating PRs in target repos (needs `repo` scope)
- `ANTHROPIC_API_KEY` — fallback LLM
- Secrets are passed as environment variables, never logged
- Redaction integrated into `StructuredLogger`, `Tracer`, `ToolExecutor`
- Provider-specific validation on startup (missing key → clear error before any API calls)
- 63 tests covering redaction, validation, and integration with all observability components

### 2.4 Fork and Rollback Script ✅
- `scripts/setup-fork.sh` — forks a Konflux repo, rolls back to before a specified commit
- Used to prepare the test scenario for MVP validation
- Outputs the fork URL and the issue URL for the loop to process

**Deliverable**: A GitHub Actions workflow that runs the loop engine against a target repo. Can be triggered manually. Produces downloadable artifacts.

## Phase 3: Visualization and Reporting

### 3.1 Report Generator ✅
- `engine/visualization/report_generator.py` — reads `execution.json`, produces HTML
- Template-based: Jinja2 templates with embedded D3.js

### 3.2 Decision Tree Visualization ✅
- `engine/visualization/decision_tree.py` — transforms execution log into tree data structure
- `templates/visual-report/decision-tree.js` — D3.js rendering
- Click-to-expand nodes showing LLM transcripts and action details
- `TreeNode` dataclass with `to_dict()` for D3.js-compatible JSON serialization
- `build_decision_tree()` transforms raw execution records into hierarchical tree
- Phase nodes with action children, outcome node, status-colored nodes
- Integrated into `ReportGenerator` and `report.html` template
- 74 tests covering tree builder, node types, helpers, and report integration

### 3.3 Action Map Visualization ✅
- `engine/visualization/action_map.py` — transforms action log into layered map data
- `templates/visual-report/action-map.js` — D3.js rendering
- Layered by phase, objects represent actions, connections show data flow
- `ActionMapNode`, `ActionMapEdge`, `ActionMapLayer`, `ActionMapData` dataclasses with `to_dict()` serialization
- `build_action_map()` transforms raw execution records into layered structure
- Sequential edges within layers, phase transition edges across layers, file-based data flow edges
- Node size encodes token usage; color encodes action type; status-colored layer backgrounds
- Click-to-expand detail panel, hover tooltips, arrow markers on edges
- Integrated into `ReportGenerator` and `report.html` template
- 69 tests covering dataclasses, builder, edges, layers, summary, helpers, and report integration

### 3.4 Comparison Report ✅
- `engine/visualization/comparison.py` — generates side-by-side diff view
- Shows agent fix vs human fix
- Calculates similarity metrics (Jaccard file overlap, per-file line similarity, heuristic composite score)
- AI-generated analysis of differences
- `ComparisonData`, `DiffSummary`, `FileDiff`, `ComparisonMetrics` dataclasses with `to_dict()` serialization
- `build_comparison()` transforms execution records into comparison data
- `parse_unified_diff()` parses standard git diff output
- Integrated into `ReportGenerator` and `report.html` template with metrics cards, file overlap table, line changes table, analysis display, test comparison, and expandable raw diffs
- 49 tests covering dataclasses, diff parsing, file overlap, metrics computation, build_comparison, and report generator integration

### 3.5 Report Publishing ✅
- `engine/visualization/publisher.py` — `ReportPublisher` class + CLI entry point (`python -m engine.visualization.publisher`)
- `publish()` generates report.html, summary.md, artifact-manifest.json to output directory
- `build_summary_markdown()` produces metrics summary for GitHub Actions step summary
- `build_artifact_manifest()` produces JSON manifest of generated files with config snapshot
- Integrated into `RalphLoop._write_outputs()` — reports generated automatically as byproduct of execution
- Upload as GitHub Actions artifacts (workflow steps for execution + reports artifacts)
- Optional GitHub Pages deployment via `publish-to-pages` job gated by `publish_to_pages` config flag
- 45 tests covering publisher, summary, manifest, CLI, error handling, and loop integration

**Deliverable**: Interactive HTML reports generated for every loop execution. Decision tree, action map, and comparison view all functional with click-to-explore.

## Phase 4: Integration Layer

### 4.1 GitHub Integration (enhanced) ✅
- `engine/integrations/github.py` — `GitHubAdapter` implementing `IntegrationAdapter` protocol (SPEC §9.2)
- `IntegrationAdapter` protocol in `engine/integrations/__init__.py` with discover/read/write/search
- `IntegrationsConfig` in `engine/config.py` — `GitHubIntegrationConfig`, `SlackIntegrationConfig`, `JiraIntegrationConfig` with YAML loading
- Resource-based routing: `issue/{n}`, `pr/{n}`, `issue/{n}/comments`, `pr/{n}/reviews`, `ci/ref/{ref}`, `issue/{n}/labels`
- High-level typed methods: `read_issue`, `read_pr`, `create_pr`, `post_comment`, `list_issue_comments`, `add_labels`, `remove_label`, `check_ci_status`, `get_pr_reviews`, `search_issues`
- Commit signing via gitsign and GPG with `configure_commit_signing()`
- `parse_repo_from_url()` and `parse_issue_number_from_url()` helpers
- 74 tests covering protocol compliance, all methods, error paths, URL parsing, config integration, commit signing

### 4.2 Slack Integration ✅
- `engine/integrations/slack.py` — `SlackAdapter` implementing `IntegrationAdapter` protocol (SPEC §9.2)
- `SLACK_BOT_TOKEN` env var added to `KNOWN_SECRET_ENV_VARS` in `engine/secrets.py`
- Resource-based routing: `channel/{id}/messages`, `channel/{id}/post`, `notification`
- High-level typed methods: `post_message`, `post_notification`, `read_history`, `list_channels`
- Notification on loop completion with emoji-prefixed levels (success, failure, escalation, info)
- Read-only channel monitoring with injection guards (`_wrap_untrusted` wraps all message content)
- Channel search by name and purpose (case-insensitive)
- 62 tests covering protocol compliance, all methods, error paths, injection guards, config integration, secret registration

### 4.3 Jira Integration ✅
- `engine/integrations/jira.py` — `JiraAdapter` implementing `IntegrationAdapter` protocol (SPEC §9.2)
- `JIRA_API_TOKEN` and `JIRA_USER_EMAIL` env vars added to `KNOWN_SECRET_ENV_VARS` in `engine/secrets.py`
- `JiraIntegrationConfig` extended with `server_url` field in `engine/config.py`
- Supports both Jira Cloud (Basic auth with email:token) and Jira Data Center (Bearer auth with PAT)
- Resource-based routing: `issue/{key}`, `issue/{key}/comments`, `issue/{key}/transitions`, `issue/{key}/transition`
- High-level typed methods: `read_issue`, `post_comment`, `list_comments`, `get_transitions`, `transition_issue`, `search_issues`
- Injection guards (`_wrap_untrusted`) on issue descriptions and comment bodies
- JQL search with auto-prepend of project clause when configured
- Can use Jira issues as trigger source (alternative to GitHub issues)
- 84 tests covering protocol compliance, all methods, error paths, injection guards, config integration, secret registration

### 4.4 Discovery Service ✅
- `engine/integrations/discovery.py` — `DiscoveryService` class with adapter registration and probe-all
- Auto-detect what's configured (which secrets are set, which APIs respond) via `available_integrations()` and `has_required_secrets()`
- `from_config()` classmethod auto-constructs GitHub/Slack/Jira adapters from `EngineConfig` + `SecretManager`
- `discover_all()` calls each adapter's `discover()` with error isolation (broken adapters don't crash discovery)
- `build_catalog()` and `catalog_as_text()` produce structured/text integration catalogs for LLM context injection
- `INTEGRATION_SECRET_REQUIREMENTS` maps integration names to required env vars (OR logic — any one suffices)
- 54 tests covering protocol compliance, registration, secret checks, availability, discovery, catalog building, from_config, end-to-end

**Deliverable**: Pluggable integration layer. GitHub is fully functional. Slack and Jira are functional for basic operations. Discovery service enumerates available integrations, probes authentication, and builds LLM-ready catalogs.

## Phase 5: Hardening and Testing

### 5.1 Prompt Injection Testing ✅
- `tests/test_prompt_injection.py` — 127 tests with known injection payloads
- Payload catalog: direct instruction, role hijacking, system prompt leak, classification manipulation, approval manipulation, delimiter escape, JSON injection, nested injection
- Verify all phases (triage, implement, review, validate) wrap untrusted content with delimiters
- Verify injection payloads never appear in LLM system prompts
- Verify delimiter escape attempts are contained within the real delimiters
- Verify integration adapters (Slack, Jira) wrap all external content
- Verify phase tool restrictions (triage/review read-only, etc.)
- Verify prompt templates instruct LLMs to treat content as untrusted
- Verify phases escalate when injection is detected
- Verify fail-closed behavior on malformed LLM responses
- Cross-phase zero-trust verification (each phase re-reads source material)
- Regression tests for each documented injection vector

### 5.2 Loop Behavior Testing ✅
- Test iteration cap enforcement
- Test time budget enforcement
- Test escalation behavior
- Test phase validation independence

### 5.3 End-to-End Testing ✅
- `tests/test_e2e.py` — 46 tests across 6 test classes
- Three simulated Konflux-style bugs: Go nil pointer, Python import error, YAML config typo
- Full pipeline tests: all phases run end-to-end with real phase implementations and MockProvider
- Comparison mode tests: comparison_ref recorded, build_comparison produces metrics, injected diffs compute similarity
- Metrics and observability tests: per-phase timing, LLM provenance, tool action recording, time budget compliance
- Report generation tests: extract_report_data, HTML output, decision tree, action map, reports directory
- Robustness tests: no-token graceful handling, triage escalation, review rejection backtrack, iteration cap
- Cross-scenario quality: parametrized across all 3 bugs for serialization, report extraction, zero escalation

### 5.4 Security Audit ✅
- `tests/test_security_audit.py` — 59 tests across 5 test classes
- Verify commit signing works (gitsign config, GPG config, unknown method rejection, YAML configurability)
- Verify provenance recording (model, provider, tokens_in, tokens_out in every LLM action across all 4 phases, execution record persistence)
- Verify no secrets in logs or artifacts (5 secret types through logger, tracer, ToolExecutor, log files, execution.json — full redaction pipeline)
- Verify untrusted content separation in all LLM calls (all 4 phases wrap issue body with delimiters, issue body never in system prompts, prompt templates instruct untrusted handling, inner iteration refinement wraps content)
- Cross-cutting: phase tool restrictions, path traversal prevention, fail-closed on malformed responses, action uniqueness and timestamps, integration adapter injection guards

**Deliverable**: Comprehensive test suite. Validated against real Konflux bugs. Security properties verified.

## Phase 6: Self-Improvement Infrastructure (stretch)

### 6.1 Golden Principles Enforcement ✅
- `engine/golden_principles.py` — AST-based static analyzer checking 7 golden principle properties
- Checks: P1 (phase logging + tool tracing), P3 (untrusted separation), P5 (iteration bounds), P8 (LLM provenance), P9 (report publishing), P10 (config-driven behavior)
- `make principles` target runs the checker; integrated into `make check` as CI gate
- 72 tests covering all checks, AST helpers, CLI, real-engine compliance, and edge cases

### 6.2 Deterministic Tool Extraction ✅
- `engine/tools/extraction.py` — `PatternDetector` scans execution records for repeated LLM call patterns
- `LLMCallPattern` and `ExtractionProposal` dataclasses with `to_dict()` serialization
- Five extraction categories: `file_check`, `test_run`, `lint_check`, `classification`, `diff_analysis` + `general` fallback
- Each category has a code template with valid Python implementation, tool schema, confidence score, and rationale
- Word-level Jaccard similarity clustering groups similar LLM prompts across phases
- `ProposalGenerator` maps detected patterns to deterministic tool proposals with ready-to-use code
- `detect_and_propose()` one-call entry point for the full pipeline
- `format_proposals_text()` for human-readable CLI output
- CLI entry point: `python -m engine.tools.extraction <execution.json> [...]`
- `detect_multi()` for cross-execution pattern detection
- 109 tests covering dataclasses, similarity, categorization, detection, generation, CLI, templates, edge cases

### 6.3 Background Quality Scans ✅
- `engine/quality_scanner.py` — `BackgroundQualityScanner` class combining golden principles enforcement, extraction proposal scanning, and code metrics collection
- `ScanReport`, `ScanFinding`, `CodeMetrics` dataclasses with `to_dict()` serialization
- `build_refactoring_pr_body()` generates structured PR descriptions from scan results
- `build_scan_summary()` produces concise CI-friendly text output
- `.github/workflows/quality-scan.yml` — weekly cron + manual trigger, auto-creates GitHub issues on critical violations
- `make quality-scan` target for local use
- CLI entry point: `python -m engine.quality_scanner [engine_path] [--execution-dir DIR] [--output FILE]`
- Critical vs warning severity classification based on principle codes
- Execution record scanning for tool extraction opportunities with deduplication
- 72 tests covering dataclasses, scanner, PR body, summary, CLI, real-engine integration, edge cases

**Deliverable**: Self-improvement loop that watches the engine's own behavior and proposes improvements.

## Production Hardening (post-build fixes from live runs)

### Cross-Fork PR Workflow ✅
- Added `fork_repo` workflow input to `ralph-loop.yml`
- `git remote set-url --push origin` redirects pushes to the user's fork
- `validate.py` creates `rl/fix` branch, pushes to fork, constructs `fork_owner:branch` head for cross-fork PR
- Tests: bare-repo git helpers, cross-fork `head` assertion

### LLM API Fix (Gemini usage_metadata) ✅
- `engine/integrations/llm.py` — replaced `dict.get()` with `getattr()` for `GenerateContentResponse.usage_metadata` attribute access

### Triage Sensitivity Tuning ✅
- `templates/prompts/triage.md` — explicit bug indicators (error messages, actual/expected, stack traces) to reduce false "ambiguous" classifications
- `engine/phases/triage.py` — ambiguous with confidence >= 0.4 now proceeds to implement instead of escalating

### Implement Phase: file_changes + Keyword Fallback ✅
- `templates/prompts/implement.md` — prompt now explicitly requests `file_changes` array with `path` + full `content`
- `engine/phases/implement.py` — added `_search_relevant_files()` fallback: greps repo for issue keywords when triage provides no affected_components

### Escalation Reason Transparency ✅
- All escalation paths in `triage.py` now include the LLM's `reasoning` field in `escalation_reason`
- Users see **why** the LLM classified something as feature/ambiguous, not just the classification

### Execution Traceability ✅
- `engine/loop.py` — iteration records now include `findings`, `artifacts`, and `escalation_reason` (truncated to prevent bloat via `_truncate_dict`)
- `engine/phases/base.py` — crash handler captures which OODA step failed, partial context gathered before crash, and the Python traceback
- `engine/visualization/publisher.py` — `summary.md` now includes an "Iteration Trace" section with per-phase pass/fail, duration, escalation reasons, and key findings
- All of this surfaces in `$GITHUB_STEP_SUMMARY` so traceability is visible directly in the workflow run

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
