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
- `engine/loop.py` — the core phased pipeline engine
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
- `.github/workflows/rl-engine.yml`
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
- Integrated into `PipelineEngine._write_outputs()` — reports generated automatically as byproduct of execution
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

### Review Progressive Leniency ✅
- Review phase counts prior review iterations via `_count_prior_reviews()`
- On 2nd+ review: injects `PROGRESSIVE REVIEW` context into LLM prompt instructing pragmatic evaluation
- `_only_nit_findings()` static method detects when all findings are nit-severity
- `reflect()` auto-upgrades `request_changes` → `approve` when only nits remain on 2nd+ review
- `_summarize_prior_reviews()` builds prior review history for LLM context
- Review prompt (`templates/prompts/review.md`) rewritten with pragmatic guidelines: approve working fixes, nits don't block
- Escalation threshold increased from 3 to 5 (`escalation_on_review_block_after` in `LoopConfig`)

### Meta Loop Runner Script ✅
- `scripts/meta-loop.sh` — production meta loop CI runner
- Triggers `rl-engine.yml` via `gh workflow run`, monitors with polling, downloads artifacts, analyzes `execution.json`
- Review analysis: extracts verdicts, findings, rejection counts, escalation reasons
- Continuous mode (`--continuous`): trigger → wait → analyze → repeat until success or max runs
- Supports `--fork-repo`, `--provider`, `--config` overrides
- Auto-detects GitHub repo from git remote

### Deterministic Path-Consistency Checker ✅
- `engine/phases/review.py` — `_check_path_consistency()` post-LLM safety net in the review `act()` phase
- Regex-based extraction of file/directory paths from shell scripts (handles `${var}` interpolation)
- Categorizes paths by operation type: create (skopeo copy, mkdir), cleanup (rm -rf), reference (umoci, check-payload, grep)
- Detects OCI tag mismatches (e.g., creation uses `:latest` but cleanup omits it)
- Injects findings and downgrades `approve` → `request_changes` when consistency issues found
- Helper functions: `_strip_oci_tag()`, `_has_oci_tag()`, `_extract_path_bases()`
- Motivated by KONFLUX-11443 post-mortem: engine run 23617134590 dropped `:latest` from OCI cleanup path, self-review missed it

### Review Prompt: Paired-Operation Consistency ✅
- `templates/prompts/review.md` — added review dimension #6 "Consistency of Paired Operations"
- Instructs LLM to verify creation paths match cleanup/deletion paths exactly (including OCI tag suffixes)
- Call site consistency: function parameter ordering must match signature at all call sites
- Severity callout: path mismatches are correctness issues, not style nits

### Implement Prompt: Consistency Requirements ✅
- `templates/prompts/implement.md` — added "Consistency Requirements" section
- Path consistency: modifications to creation ops must apply identically to cleanup/deletion ops
- Parameter ordering: follow existing codebase conventions when adding new function parameters
- Call site updates: verify every call site when changing a function signature

### KONFLUX-11443 Production Validation ✅
- Engine validated against KONFLUX-11443 (race condition in fbc-fips-check-oci-ta parallel processing)
- Two successful production runs: 23615068030 (2.5 min) and 23617134590 (2.8 min, created PR)
- Fix matched human PR #3057 strategy (unique image_num per parallel job)
- Graded A- vs human A — deducted for path consistency gap that review didn't catch
- Post-mortem led to the three improvements above

### OCI URI False Positive Fix ✅
- `_check_path_consistency()` now tracks OCI URI-sourced creation paths separately
- OCI tools (skopeo, umoci) use `oci:///dir:tag` format where the tag is an image reference, not a filesystem component
- Cleanup paths correctly use the base directory (without tag) — checker no longer flags this as a mismatch
- Fixed after PR #4 grading (run 23618411249) showed the checker caused an unnecessary implement→review cycle (~3 min)

### LLM-Generated PR Titles ✅
- `validate.py` now reads `pr_title` from LLM response instead of hardcoded `Fix: {issue_title}` format
- `validate.md` prompt updated to require descriptive titles in conventional commit format
- Falls back to old format if LLM doesn't provide a title; truncated at 150 chars
- Motivated by PR #4 having title "Fix: Bug fix" — completely non-descriptive

### Validate Prompt: Comprehensive PR Descriptions ✅
- `validate.md` updated to require PR descriptions covering ALL changes across iterations
- Added PR Title as validation check #4 with conventional commit format guidance
- PR Description check expanded to emphasize root cause analysis and full scope

## Production Hardening (post-build fixes from live runs)

### Cross-Fork PR Workflow ✅
- Added `fork_repo` workflow input to `rl-engine.yml`
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

## Phase 7: Production Observability and Feedback Loops

Deficiency catalog from 4 production runs (workflow runs `23555432272`, `23555603479`, `23555788282`, `23556924033`). Each item includes evidence, root cause, and fix spec.

### CRITICAL — Engine Produces Wrong Results

#### 7.1 Issue Content Never Fetched (D1) ✅
- **Evidence**: Runs 3 and 4 — triage sees `title: N/A, body: N/A`; LLM operates on empty issue
- **Root cause**: `_fetch_issue` fix (commit `41b8ad1`) was pushed AFTER run 4 triggered (ran commit `64d56b8`)
- **Fix**: Added GitHub API fallback (`curl` or `github_api` tool) when `gh` CLI fails. Refactored `_fetch_issue` into `_fetch_issue_gh_cli` + `_fetch_issue_api` with clear separation. Added narration on fetch failure so it's visible in GitHub Actions logs. 23 new tests covering: gh CLI success, API fallback (curl + github_api tool), both-fail N/A defaults, URL parsing, invalid URLs, JSON parse errors, narration on success/failure, observe()-level fetch triggering, and loop-to-triage URL-only handoff.

#### 7.2 Metrics Counters Disconnected from Tracer (D2) ✅
- **Evidence**: Run 4 `execution.json` shows `total_llm_calls: 0, total_tokens_in: 0, total_tokens_out: 0` despite 7 actual LLM calls visible in the `actions` array
- **Root cause**: All phases call `self.tracer.record_llm_call()` but none call `self.metrics.record_llm_call()`. `LoopMetrics.record_llm_call()` exists but is never invoked.
- **Fix**: Added `record_llm_call()` helper on base `Phase` class that calls both `tracer.record_llm_call()` and `metrics.record_llm_call()` in one call. Wired `LoopMetrics` from `PipelineEngine` into phase instantiation. Updated all 4 phase files + golden principles checker. 11 new tests covering helper method, all phases, and e2e metrics verification.

### HIGH — Engine Runs But Produces Wrong Fixes

#### 7.3 Implement Retries Same Failing Approach (D3) ✅
- **Evidence**: Run 3 iterations #2–#5 all fail identically with "No files modified" — same N/A issue, same empty components, same fallback grep
- **Root cause**: On retry, implement re-runs `observe() → plan() → act()` with identical inputs. No escalating strategy or memory of what failed.
- **Fix** (three parts — code, prompt, and search strategy):
  1. Added `_extract_retry_context()` to `engine/phases/implement.py` — reads prior failed implement PhaseResults from `self.prior_results`, extracts approach, root_cause_guess, files_attempted, validation_issues per attempt.
  2. Added `_format_retry_context()` module-level helper that formats retries as `PRIOR IMPLEMENTATION ATTEMPTS` block for LLM trusted context, instructing "do NOT repeat" failed approaches.
  3. Wired into `observe()` (returns `retry_context` + `retry_count`) and `plan()` (appends formatted retry context to trusted LLM context when present).
  4. Updated `reflect()` to store `files_changed` in `artifacts` on failure (not just success) so next retry knows what files were tried.
  5. Replaced `_search_relevant_files()` with adaptive 3-tier strategy: retry 0 = keyword search (>4 chars), retry 1 = broader keywords (>3 chars, 8 results), retry 2+ = `_broad_file_scan()` listing all source files. Added `_extract_keywords()` with stopword filtering and N/A rejection.
  6. Added `_collect_previously_tried_files()` to exclude already-tried files from searches.
  7. Updated `templates/prompts/implement.md` with "Retry Adaptation" section guiding the LLM to change strategy.
  8. 41 new tests covering keywords, retry context extraction, formatting, pipeline integration, reflect metadata, and adaptive search.

#### 7.4 Review "block" Kills the Loop Instead of Teaching (D4) ✅
- **Evidence**: Run 4 iteration #7 — review says `verdict: block` for a version downgrade (quality issue), not injection. Loop ends with `escalated`.
- **Root cause**: `templates/prompts/review.md` treats "block" as the verdict for both security/injection AND bad quality. The LLM uses "block" when it should use "request_changes".
- **Fix** (two parts — prompt AND code):
  1. Updated `templates/prompts/review.md` with explicit verdict guidelines: `block` reserved for injection/security only, `request_changes` for all fixable quality issues, mandatory `suggestion` field on every finding
  2. Updated `engine/phases/review.py` `reflect()`: programmatic downgrade of `block` to `request_changes` when no `injection_detected` and no finding with `severity: blocking` + `dimension: security`. Added `_has_security_block()` static helper. 12 new tests covering downgrade logic and helper method.

#### 7.5 Implement Doesn't Read Review Feedback (D5) ✅
- **Evidence**: When review returns `next_phase: implement` with `request_changes`, implement only reads triage output via `_extract_triage_report()` — review findings are ignored.
- **Root cause**: `implement.py` `_extract_triage_report()` loops `self.prior_results` looking only for `result.phase == "triage"`. Review feedback is discarded.
- **Fix** (two parts — code AND prompt):
  1. Added `_extract_review_feedback()` in `engine/phases/implement.py` that reads the most recent review `PhaseResult` from `self.prior_results`. Extracts verdict, findings, suggestions, summary, and scope assessment.
  2. Added `_format_review_feedback()` module-level helper that formats review feedback as a structured text block for the LLM trusted context. Includes per-finding dimension, severity, description, file location, and suggestion.
  3. Wired into `observe()` (returns `review_feedback` key) and `plan()` (appends formatted feedback to trusted context when present).
  4. Added "Previous Review Feedback" section to `templates/prompts/implement.md` instructing the LLM to address every finding from the prior review and change its approach accordingly.
  5. 20 new tests covering extraction (8 tests: artifacts, findings fallback, latest pick, empty cases, skips non-review), formatting (6 tests: basic, with findings, location, multiple, empty, cap at 10), and pipeline integration (6 tests: observe includes feedback, plan includes in LLM context, feedback in trusted context not untrusted, omitted when no review).

#### 7.6 LLM Response Parsing Fails Silently + file_changes Reliability (D6) ✅
- **Evidence**: Run 4 iteration #2 — `fix_description: "Failed to parse LLM response. Raw: "` (empty raw content!). Across runs 3 and 4, the LLM fails to return proper `file_changes` in ~80% of attempts — sometimes JSON parsing fails entirely, other times JSON is valid but `file_changes` is empty or contains no `content`.
- **Root cause**: `parse_implement_response()` returns a default dict with `file_changes: []` when parsing fails. No retry, no diagnostic. Even when JSON parses successfully, the LLM frequently omits file content or returns incomplete structures.
- **Fix** (four parts — validation, retry, prompt, and config):
  1. Added `validate_impl_plan()` function that checks: not a parse failure, `file_changes` is non-empty list, each entry has non-empty `path` and `content`. Added `is_parse_failure()` helper to detect the default parse-failure dict.
  2. Added `_parse_with_retry()` method to `ImplementPhase` — validates the initial parse, logs the raw response on failure (truncated), retries the LLM call with explicit "respond ONLY with valid JSON" instruction including the specific validation issues. Prefers parsed-but-incomplete responses over total parse failures when both attempts fail.
  3. Wired `_parse_with_retry()` into both `plan()` and `_request_refinement()` — all LLM response parsing in the implement phase now validates and retries.
  4. Added `max_parse_retries` to `ImplementPhaseConfig` (default: 1, configurable via `.rl-config.yaml`).
  5. Updated `templates/prompts/implement.md` with stronger JSON-only output emphasis, explicit requirements for non-empty `file_changes` with complete file content, and warnings that empty arrays cause iteration failures.
  6. 32 new tests covering `is_parse_failure`, `validate_impl_plan`, `_parse_with_retry` (valid no retry, parse failure retry, empty file_changes retry, missing content retry, both fail best-of, total failure, logs raw, records LLM call, retry message content, configurable retries), refinement retry, and config.

#### 7.7 Keyword Fallback Finds Wrong Files (D7) ✅
- **Evidence**: Run 4 iteration #6 — with N/A issue content, LLM grepped for literal "N/A" in the repo, found a YAML file with "N/A" in its description, and "fixed" it by changing the description.
- **Root cause**: `_search_relevant_files()` extracts keywords from issue title/body. With "N/A" content, either no keywords or "N/A" itself becomes a keyword.
- **Fix**: Addressed as part of 7.3 — `_extract_keywords()` filters stopwords (40 common English words), rejects "N/A", and enforces minimum keyword length (4+ chars). When no valid keywords are found, falls back to `_broad_file_scan()` instead of grepping for garbage terms. Tests verify N/A rejection and stopword filtering.

### MEDIUM — Observability and UX

#### 7.8 No Live Narration / Real-time Progress (D8) ✅
- **Evidence**: User sees only `[phase=implement iter=2]` debug lines in GitHub Actions logs. No human-readable sentences explaining what the engine is doing.
- **Fix** (five parts — logger, loop, phases, progress file, tests):
  1. Added `narrate()` method to `engine/observability/logger.py` — writes `>>> [PHASE] message` to stderr (visible in live GH Actions log), stores in `_narrations` list, and appends to `output/progress.md` running markdown file. Added `write_progress_heading()` for section headers. Added `progress_path` parameter to `StructuredLogger.__init__()`. Redaction applied via the existing `SecretRedactor`.
  2. Wired `progress_path` into `PipelineEngine.__init__()`. Loop narrates at: start (issue URL, config), each iteration start (phase name, iteration number), phase result (succeeded/failed, duration, elapsed time), escalation events, time budget/iteration cap, phase transitions, review rejection cap, retryable failures, and loop completion (status, total time).
  3. All 4 phases emit narration at each OODA step: observe (what context was gathered), plan (LLM result summary), act (what was done), validate (issues found), reflect (decision and next step). Each narration is a 1–2 sentence human-readable summary.
  4. `output/progress.md` is a running markdown file with `# RL Engine Progress` heading, per-iteration `## Iteration N — phase` headings, and bullet-point narrations. Continuously appended during execution.
  5. 34 new tests covering: narrate core (stderr, list, progress.md, parent dir creation, no-path, multi-phase, copy), redaction (stderr, list, file), write_progress_heading, loop narration (start, phase, result, completion, escalation, iteration cap, progress.md, headings), per-phase narration (triage observe/plan/reflect, implement observe/plan, review observe/plan, validate observe/plan), and full-run progress.md structure.

#### 7.9 report.html Lacks Narrative (D9) ✅
- **Evidence**: HTML report shows decision trees and action maps but no plain-English summary of what happened.
- **Fix**: Added `build_narrative()` in `engine/visualization/publisher.py` — deterministic, template-based plain-English paragraph from execution data (no LLM call). Covers issue identification, triage classification + confidence, implementation attempt count + success/failure, review verdict, and final status. Added `narrative` field to `ReportData` in `report_generator.py`. Inserted narrative as the first section in `report.html` (before metrics cards) with accent-colored left border. Added narrative as opening paragraph of `summary.md`. 24 new tests covering all narrative paths (status variants, phase combinations, edge cases) plus integration with summary.md, report.html, and ReportData.

#### 7.10 Artifact Completeness — log.json and progress.md Not Uploaded (D10) ✅
- **Evidence**: `./output/log.json` is written by `StructuredLogger` and `./output/progress.md` will be written by the narrator (7.8), but the artifact upload in `rl-engine.yml` only captures `execution.json`, `reports/`, `transcripts/`, and `status.txt`.
- **Fix**: Added `./output/log.json` and `./output/progress.md` to the `path` list in the "Upload execution artifacts" step of `.github/workflows/rl-engine.yml`. 5 new tests verify: workflow YAML lists all expected artifact paths, loop run produces `log.json` and `progress.md`, all core outputs exist after a run, and retention days match config.

#### 7.11 summary.md Shows Raw JSON (D11) ✅
- **Evidence**: Iteration trace in `$GITHUB_STEP_SUMMARY` dumps truncated JSON for findings (e.g., `impl_plan: {'root_cause': 'unknown'...`).
- **Fix**: Added `_format_finding_value()` and `_summarise_dict()` helpers in `publisher.py`. String/number/bool values render inline, dicts render as `key: value` pairs, lists render as comma-separated items or semicolon-separated summaries. Nested dicts show `(N keys)`, nested lists show `(N items)`. Long values truncated with ellipsis. 37 new tests covering all value types, truncation, integration with `build_summary_markdown()`, and no-raw-repr assertions.

### LOW — Improvements and Polish

#### 7.12 No Backoff Between LLM Retries (D12) ✅
- **Evidence**: 5 implement iterations fire LLM calls back-to-back with ~10s each.
- **Fix**: Added `retry_backoff_base_seconds` (default 1.0) and `retry_backoff_max_seconds` (default 4.0) to `LoopConfig`. Exponential backoff (`base * 2^(retries-1)`, capped at max) applied on soft failure retries and backward phase transitions (e.g., review → implement). Forward transitions and successful phase advances reset the counter. `asyncio.sleep` used for non-blocking delay. 12 new tests covering formula, soft retries, backtracks, counter reset, narration, normal progression, config via YAML, and escalating delays.

#### 7.13 Test Runner Detection Too Generic (D13) ✅
- **Evidence**: `_run_tests()` tries `pytest || go test || npm test`. Since pytest is an engine dev dependency, it may execute in the wrong context on the target repo.
- **Fix**: Added `engine/tools/test_runner.py` — `detect_repo_stack()` detects the target repo's primary language from project manifest files (go.mod, package.json, Cargo.toml, pyproject.toml, etc.) and file extension frequency. Returns `RepoStack` with language-specific test and lint commands. Detection priority: manifest files (0.95 confidence) > file extension frequency > fallback. Added `test_command` and `lint_command` config overrides to `ImplementPhaseConfig` and `ValidatePhaseConfig` for `.rl-config.yaml` customization. Updated all 3 phases (triage, implement, validate) to detect the stack during `observe()` and use the detected commands in `_run_tests()` / `_run_linters()`. The old chained `pytest || go test || npm test` pattern is completely removed. 50 new tests covering detection, config overrides, phase integration, and absence of old chained commands.

#### 7.14 `affected_components` Always Empty from Triage (D14) ✅
- **Evidence**: Even with valid issue content, triage may not produce file paths in `affected_components`.
- **Fix** (three parts — prompt, code, and tests):
  1. Updated `templates/prompts/triage.md` to strongly mandate at least one `affected_components` entry with a real file path from the repo listing. Prompt now explains downstream dependency and gives file path examples.
  2. Added `_suggest_components()` and `_extract_triage_keywords()` module-level helpers in `engine/phases/triage.py`. When the LLM returns empty or non-existent `affected_components`, `act()` falls back to keyword-based file matching: extracts keywords from issue title/body (stopword-filtered), scores repo files by keyword density (filename match bonus, test file penalty), and suggests the top matches.
  3. Wired into `act()`: after `_verify_components()`, if no components were found, runs the suggestion fallback and re-verifies. Suggested components propagate through to `reflect()` artifacts for the implement phase.
  4. 20 new tests covering: `_extract_triage_keywords` (8 tests: title extraction, stopwords, short words, N/A rejection, body extraction, dedup, max limit, empty), `_suggest_components` (8 tests: keyword matching, source-over-test preference, empty keywords, empty repo, no matches, max results, filename bonus, multi-keyword), and `act()` integration (4 tests: fallback triggered on empty components, skipped on valid components, graceful on no keywords, artifacts propagation).

#### 7.15 Local Filesystem Out of Sync (D15) ✅
- **Evidence**: `engine/__main__.py` and `engine/loop.py` in the working tree showed old TODO placeholder versions while `git show HEAD:` showed the real 438-line/120-line implementations.
- **Fix**: Operational — run `git checkout -- .` to restore working tree. Not a code fix but a development workflow issue to be aware of.

#### 7.16 "report" Phase Silently Skipped (D16) ✅
- **Evidence**: `PHASE_ORDER` includes "report" but no phase class is registered. Handled by `_publish_reports()` in `_write_outputs()` instead.
- **Fix**: Created `engine/phases/report.py` — `ReportPhase` class implementing the full OODA cycle (SPEC §5.5). Wraps `ReportPublisher` to generate decision tree, action map, and comparison reports. Report failures never block the loop (`reflect()` always returns `success=True`). Loop passes execution snapshot and output dir via `issue_data` for the report phase only. `_publish_reports()` in `_write_outputs()` skips when the report phase already published (avoids double generation), retained as fallback when phase is unregistered. Registered in `engine/__main__.py`. 34 tests covering: class attributes (4), observe (4), plan (3), act (6), validate (3), reflect (3), full execute (2), loop integration (5), narration (4).

#### 7.18 Test Execution Made Optional — CI-First Validation Strategy (D18) ✅
- **Evidence**: In production, the engine targets arbitrary GitHub repos. Running tests inside the GH Action runner is unreliable because: (1) correct language runtime/version may not be installed, (2) dependencies may require Docker, databases, kind clusters, or other infrastructure the runner lacks, (3) test suites may exceed the 120s `_run_tests()` timeout (real suites can take 20+ minutes), (4) pre-existing flaky tests waste the iteration budget chasing unrelated failures, (5) executing arbitrary shell commands from target repos is a security surface. Once a PR is submitted, the repo's own CI pipeline — which has the correct matrix, services, secrets, and infrastructure — will run tests. That is the purpose-built validation layer.
- **Root cause**: Test execution is a hard gate in both implement (`run_tests_after_each_edit` defaults `True`) and validate (`full_test_suite` defaults `True`). This assumes the GH Action runner has the target repo's full test infrastructure — which is only true for repos that explicitly configure `.rl-config.yaml` with a working `test_command`.
- **Fix** (five parts — config defaults, implement phase, validate phase, post-PR CI monitoring, and tests):
  1. **Change config defaults.** In `engine/config.py`: change `ImplementPhaseConfig.run_tests_after_each_edit` default to `False`. Change `ValidatePhaseConfig.full_test_suite` default to `False`. Add `test_execution_mode` field to both phase configs with values: `"disabled"` (default — skip tests entirely), `"opportunistic"` (run tests but don't gate on failure), `"required"` (current behavior — gate on pass). When `.rl-config.yaml` provides an explicit `test_command`, auto-promote mode to `"opportunistic"` unless `test_execution_mode` is explicitly set.
  2. **Implement phase.** In `engine/phases/implement.py`, respect `test_execution_mode`: `"disabled"` skips `_run_tests()` entirely, `"opportunistic"` runs tests but treats failures as informational (logged and included in LLM context, but does not block inner iteration), `"required"` preserves current hard-gate behavior. Linting remains enabled by default (cheap, fast, high success rate across repos).
  3. **Validate phase.** In `engine/phases/validate.py`, PR submission gate changes: `"disabled"` — gate on lint pass + LLM review only (tests skipped), `"opportunistic"` — include test results in PR description but don't block submission on test failure, `"required"` — current behavior (tests must pass to submit). When tests are skipped or opportunistic, the PR description must note the test status (e.g. "Tests not run locally — CI will validate" or "Local tests ran with failures; see details below — CI will validate").
  4. **Post-PR CI monitoring.** After PR creation, the validate phase should call `check_ci_status()` from the GitHub integration to poll the PR's initial CI status. Log the result and include it in the execution record. This is informational for now — the engine does not iterate on CI feedback yet, but the data is captured for future use.
  5. Tests: verify default config has `test_execution_mode: "disabled"`, verify explicit `test_command` in config auto-promotes to `"opportunistic"`, verify implement phase skips/runs/gates tests based on mode, verify validate phase adjusts PR gate and description based on mode, verify PR description includes appropriate test status messaging for each mode.

#### 7.17 Implement Phase Re-detects Stack Independently — Runs Wrong Tools (D17) ✅
- **Evidence**: Run `23573279294` — triage correctly detected `go (from go.mod, confidence=0.95)`, but implement independently re-detected `python (from file_extensions, confidence=0.85)`. Result: implement ran `pytest` and `ruff` on a Go codebase for ALL 5 inner iterations × 3 outer retries, consuming the entire 30m time budget without ever running `go test` or `golangci-lint`.
- **Root cause**: Two issues combine:
  1. **No cross-phase stack handoff.** Each phase independently calls `detect_repo_stack()` with its own `find` output. Triage's stack result (`RepoStack`) is stored in `self._detected_stack` (an instance attribute) and never serialized into `PhaseResult.artifacts` — so implement cannot read it from `prior_results`.
  2. **Truncated file listing hides manifest files.** Implement's `find` uses `sort | head -100`, while triage uses `head -200`. The `build-definitions` repo has many `.tekton/scripts/*.py` files that sort before `go.mod` and `task-generator/*.go`. With only 100 lines, `.py` files dominate the listing and `go.mod` is truncated, causing `_detect_language()` to fall back to extension counting where Python wins.
- **Fix** (three parts — handoff, fallback, and tests):
  1. **Triage serializes stack into artifacts.** In `triage.py` `reflect()`, added `detected_stack: self._detected_stack.to_dict()` to `PhaseResult.artifacts` in both bug and ambiguous-as-bug success paths. Escalation paths do not serialize the stack (downstream phases won't run).
  2. **Implement/validate inherit triage stack.** Added `_extract_triage_stack()` to both `implement.py` and `validate.py`. Reads the triage `PhaseResult` from `self.prior_results`, reconstructs a `RepoStack` from the serialized dict. Config overrides (`test_command`, `lint_command`) are applied on top. Falls back to independent detection only when no triage stack is available.
  3. **Increased implement/validate `head` limits.** Changed both phases' `find` from `head -100` to `head -200` to match triage, ensuring manifest files survive truncation even when falling back to independent detection.
  4. Also fixed pre-existing issue: narrative section missing from `report.html` template (test `test_narrative_before_metrics_in_html` was failing).
  5. 28 new tests in `tests/test_stack_handoff.py` covering: triage serialization (4 tests: bug path, ambiguous path, no-detection, escalation), implement inheritance (10 tests: present, absent, no-prior, skips-failed, config-override, picks-latest, malformed-dict, non-dict, observe-uses, observe-fallback), validate inheritance (6 tests: present, absent, no-prior, config-override, observe-uses, observe-fallback), head limit verification (2 tests), and round-trip serialization (2 tests: rust, node).

### Phase 7 Build Order

Recommended implementation sequence (each item is one meta-loop session):

| Item | Effort | Depends on | Priority |
|------|--------|------------|----------|
| 7.2 Metrics counters ✅ | 1 session | Nothing | Critical |
| 7.4 Review block → request_changes ✅ | 1 session | Nothing | High |
| 7.5 Implement reads review feedback ✅ | 1 session | 7.4 | High |
| 7.3 Implement retry adaptation ✅ | 1–2 sessions | 7.5 | High |
| 7.6 LLM parse failure retry ✅ | 1 session | Nothing | High |
| 7.7 Keyword fallback quality ✅ | 1 session | Nothing | High |
| 7.17 Stack handoff across phases ✅ | 1 session | 7.13 | **Critical** |
| 7.18 Test execution optional (CI-first) ✅ | 1 session | Nothing | **High** |
| 7.8 Live narration ✅ | 1–2 sessions | Nothing | Medium |
| 7.9 Report narrative ✅ | 1 session | 7.8 | Medium |
| 7.10 log.json in artifacts ✅ | 0.5 session | Nothing | Medium |
| 7.11 Summary rendering ✅ | 0.5 session | Nothing | Medium |
| 7.12 Backoff ✅ | 1 session | Everything above | Low |
| 7.13 Test runner detection ✅ | 1 session | Everything above | Low |
| 7.14 affected_components fallback ✅ | 1 session | Everything above | Low |
| 7.15–7.16 Remaining polish ✅ | 0.5 session | Everything above | Low |

## Phase 8: Neutral Observer and Agent Provenance Attestation

Implements the neutral observer pattern from Ralph Bean's article ["Supply Chain Security Meets the Agentic Factory"](https://medium.com/@rbean_3467/supply-chain-security-meets-the-agentic-factory-5a770c34369b). The observer runs as a separate GitHub Actions job, reconstructs the agent's execution from artifacts, cross-checks claims against evidence, produces a signed in-toto attestation, and evaluates configurable policy gates. The agent never has access to the observer's signing credentials.

See SPEC.md §2.2 NFR-5, §4.3, and §5.6 for full requirements.

### 8.1 Reconstructor and Cross-Checker ✅
- `engine/observer/__init__.py` — package init, shared types (`TimelineEvent`, `CrossCheckResult`, `CrossCheckReport`)
- `engine/observer/reconstructor.py` — `ExecutionReconstructor` class
  - `load_artifacts(artifacts_dir)` reads `execution.json`, `log.json`, `transcripts/`, `progress.md`
  - `build_timeline()` produces a chronological list of `TimelineEvent` (LLM calls, file ops, shell commands, phase transitions) from the raw artifacts
  - `extract_model_info()` returns deduplicated model identities (name, provider, temperature, token totals)
  - `extract_prompt_digests()` computes SHA-256 of each prompt template file found in `templates/prompts/`
  - `extract_tool_definitions()` hashes the tool executor configuration
- `engine/observer/cross_checker.py` — `CrossChecker` class
  - `check_diff_consistency(timeline, branch_dir)` — runs `git diff` on the agent's branch and compares against `file_changes` in execution record
  - `check_action_completeness(timeline)` — every file in the diff has a corresponding action record
  - `check_phase_ordering(timeline)` — phases ran in PHASE_ORDER, no skipped validation
  - `check_token_plausibility(timeline)` — token counts per LLM call are within plausible bounds (e.g., tokens_out <= max_tokens config)
  - `check_tool_call_integrity(timeline, transcripts_dir)` — tool calls in transcripts have matching action records
  - `run_all_checks()` returns `CrossCheckReport` with per-check pass/fail and details
- Tests: timeline reconstruction from fixture `execution.json`, each cross-check with pass and fail cases, malformed input handling

**Deliverable**: Standalone module that reads agent artifacts, builds a timeline, and verifies consistency. Testable with fixture files, no signing or network required.

### 8.2 Attestation Builder ✅
- `engine/observer/attestation.py` — `AttestationBuilder` class
  - `build(timeline, cross_check_report, config)` returns a Python dict conforming to in-toto Statement v1 with predicate type `https://rl-engine.dev/provenance/agent/v1`
  - Subject: git commit SHA(s) produced by the agent
  - Predicate follows SLSA Build provenance structure: `buildDefinition` (buildType, externalParameters, internalParameters, resolvedDependencies) and `runDetails` (builder, metadata, models, toolDefinitions, crossCheckResults)
  - `serialize(attestation)` produces canonical JSON (sorted keys, no extra whitespace) for deterministic signing
  - `validate_schema(attestation)` checks required fields and types
- Tests: build attestation from fixture data, schema validation pass/fail, canonical serialization determinism, round-trip serialize/deserialize

**Deliverable**: Produces a valid in-toto attestation dict from reconstructed execution data. Testable with unit tests, no signing infrastructure required.

### 8.3 Signer (Sigstore Integration) ✅
- `engine/observer/signer.py` — `AttestationSigner` class with three signing modes
  - `sign_sigstore(attestation_json)` — keyless signing via GitHub Actions OIDC using `cosign sign-blob` CLI (cosign automatically acquires the OIDC token from the runner environment)
  - `sign_cosign_key(attestation_json, key_path)` — local key-pair signing via `cosign sign-blob --key` (sets `COSIGN_PASSWORD` for unattended use)
  - `sign_none(attestation_json)` — no-op signer for local development (writes unsigned attestation envelope with digest)
  - `sign(attestation_json, method, key_path=)` — dispatcher that routes to the correct method
  - `verify(signed_attestation, key_path=, certificate_identity=, certificate_oidc_issuer=)` — verifies via `cosign verify-blob` with full pre-flight checks (digest integrity, bundle presence, unsigned detection)
  - `SignedAttestation` dataclass with `to_dict()`, `write(output_dir)` producing `attestation.json`, `attestation.bundle.json`, and `signing-metadata.json`
  - `VerificationResult` dataclass with `to_dict()`
  - Key file existence checked before cosign availability (clear error without requiring cosign installed)
  - `_check_cosign_available()` validates cosign CLI is on PATH
  - `_sha256_hex()` computes payload digests for integrity verification
- 48 tests covering: `SignedAttestation` dataclass (7), `VerificationResult` (2), `sign_none` (3), `sign_sigstore` mocked (3), `sign_cosign_key` mocked (4), dispatch (5), verify all modes (11), `_check_cosign_available` (3), `_sha256_hex` (4), `SUPPORTED_METHODS` (2), integration round-trips (4)

**Deliverable**: Wrapper around Sigstore/cosign that signs attestations. Testable with mocks for OIDC; real Sigstore signing tested in CI integration.

### 8.4 Policy Evaluator ✅
- `engine/observer/policy.py` — `PolicyEvaluator` class
  - `load_policy(policy_file)` reads YAML policy from `templates/policies/default.yaml`; handles missing/empty/malformed files gracefully
  - `evaluate(signed_attestation, policy)` returns `PolicyResult` (pass/fail, per-rule results, list of violations, list of warnings)
  - `RuleResult` and `PolicyResult` dataclasses with `to_dict()` serialization
  - Built-in policy rules:
    - `model_allowlist` — model IDs in attestation must be in the configured allowlist; empty allowlist = warning (not violation)
    - `prompt_integrity` — prompt template digests in attestation must match known-good digests from policy; unconfigured = warning
    - `scope_compliance` — files modified by the agent are related to the issue (heuristic: triage components, basename matching, issue body keyword search); configurable `max_unrelated_files` threshold
    - `cross_checks` — all required cross-checks in the attestation must have `passed: true`; configurable `required_checks` list; empty results = warning
    - `iteration_limits` — iteration count did not exceed configured maximum; reads from both `runDetails.metadata` and `internalParameters`
  - `format_pr_comment(policy_result)` produces a markdown summary with per-rule status icons, violations section, warnings section, and neutral observer attribution
  - `format_summary(policy_result)` produces a concise text summary for `$GITHUB_STEP_SUMMARY` with pass/fail ratio and violation/warning details
  - Helper functions: `_parse_attestation`, `_extract_models`, `_extract_resolved_deps`, `_extract_cross_check_results`
- `templates/policies/default.yaml` — default policy configuration with all 5 rules, comments explaining each setting
- `engine/config.py` — `ObserverConfig` dataclass with fields: `enabled`, `signing_method`, `policy_file`, `cross_checks`, `model_allowlist`, `prompt_template_digests`, `post_policy_result_to_pr`, `fail_on_policy_violation`; wired into `EngineConfig` with YAML loading via `_apply_observer_config()`
- 58 tests covering: dataclasses (4), load_policy (5), helpers (7), model_allowlist rule (5), prompt_integrity rule (5), scope_compliance rule (6), cross_checks rule (5), iteration_limits rule (4), full evaluate pipeline (5), format_pr_comment (3), format_summary (2), ObserverConfig (4), integration round-trips (3)

**Deliverable**: Policy engine that evaluates attestations against configurable rules. Fully testable with fixture attestations.

### 8.5 CLI and Workflow Integration ✅
- `engine/observer/cli.py` — CLI argument parsing (`parse_args`) for `python -m engine.observer`
  - `--artifacts-dir` (required) — path to downloaded agent artifacts
  - `--output-dir` — path to write attestation and policy result (default: `./attestation`)
  - `--config` — path to `.rl-config.yaml` (reads `observer` section)
  - `--branch-dir` — path to the agent's working branch checkout (for diff consistency check)
  - `--templates-dir` — path to prompt templates directory (for digest computation)
  - `--skip-signing` — skip attestation signing (for local testing without cosign)
- `engine/observer/__main__.py` — `run_observer()` function wires the full pipeline: reconstruct → cross-check → build attestation → sign → evaluate policy → write outputs (attestation.json, policy-result.json, pr-comment.md, summary.txt, signing-metadata.json)
  - `main()` CLI entry point with exit codes: 0 = policy passed, 1 = policy failed (when `fail_on_policy_violation: true`), 2 = observer error
  - `_extract_triage_components()` and `_extract_issue_body()` helpers for scope compliance rule
- Workflow changes to `.github/workflows/rl-engine.yml`:
  - New `observer` job with `needs: run-engine`, `if: always()`
  - `permissions: { id-token: write, contents: read, pull-requests: write }`
  - Installs cosign via `sigstore/cosign-installer@v3`
  - Downloads `rl-engine-execution-*` artifact from agent job
  - Runs `python -m engine.observer` with `--artifacts-dir`, `--templates-dir`, `--output-dir`, `--branch-dir`, `--config`
  - Uploads `observer-attestation-*` as a new artifact
  - Posts policy result as PR comment when PR number is available
  - Agent job exports `status` and `pr_number` as job outputs for the observer
- `ObserverConfig` (from 8.4) already in `engine/config.py` — no additional config changes needed
- 54 tests in `tests/test_observer_cli.py` covering: CLI arg parsing (11 tests), pipeline helpers (8 tests), run_observer pipeline (13 tests), main() exit codes (4 tests), workflow YAML validation (15 tests), end-to-end integration (4 tests)

**Deliverable**: Working CLI that runs the full observer pipeline. Observer job added to the GitHub Actions workflow.

### 8.6 Documentation Updates ✅
- `README.md` — added dedicated "Neutral Observer and Agent Provenance" section under Architecture: trust model diagram, 5 cross-checks table, 5 policy rules table, attestation format, configuration example, local CLI usage. Updated Engine Components table, Development History (Phase 8 → Complete), ADR count (6 → 10), run counts, milestone list.
- This file (IMPLEMENTATION-PLAN.md) — already updated with Phase 8
- SPEC.md — already updated with NFR-5, §4.3, §5.6, observer config
- ARCHITECTURE.md — ADR-008 (Neutral Observer as Separate Workflow Job)

**Deliverable**: All documentation reflects the observer capability.

### Phase 8 Build Order

| Item | Effort | Depends on | Priority |
|------|--------|------------|----------|
| 8.1 Reconstructor + cross-checker ✅ | 1–2 sessions | Phase 7 complete | Critical |
| 8.2 Attestation builder ✅ | 1 session | 8.1 | Critical |
| 8.3 Signer ✅ | 1 session | 8.2 | High |
| 8.4 Policy evaluator ✅ | 1 session | 8.2 | High |
| 8.5 CLI + workflow integration ✅ | 1 session | 8.1–8.4 | Critical |
| 8.6 Documentation ✅ | 0.5 session | 8.5 | Medium |

## Phase 9: 3D Interactive Report Overhaul (Three.js)

Replaces the existing D3.js 2D decision tree and action map with a full Three.js 3D execution landscape. The report is a single self-contained HTML file with embedded Three.js, CSS, and execution data. Every piece of information is presented as human-readable narrative, not raw JSON/YAML. See SPEC.md §6 and FR-2.

### 9.1 Three.js Scene Foundation ✅
- `engine/visualization/scene/` — new package for 3D scene generation
- `engine/visualization/scene/__init__.py` — package exports: `SceneBuilder`, `SceneData`, `ScenePlatform`, `SceneObject`, `SceneConnection`, `build_scene`
- `engine/visualization/scene/builder.py` — `SceneBuilder` class that transforms execution data into Three.js scene graph (JSON structure that the embedded JS renders)
- Scene graph structure: `platforms[]` (one per phase) with `objects[]` (one per action) and `connections[]` (data flow edges)
- Platform layout: phases arranged at ascending Y elevations (`PHASE_ELEVATIONS` dict), connected by bridge paths. Unknown phases get incremental elevations.
- Object geometry mapping via `GEOMETRY_MAP`: polyhedra = LLM calls, cubes = file operations, cylinders = command/shell runs, spheres = API calls
- Status color mapping via `STATUS_COLORS`: green = success, amber = retry/iteration, red = failure, blue = escalation, gray = unknown
- Connection data-type colors via `DATA_TYPE_COLORS`: cyan = code, gold = reasoning, green/red = test results, violet = phase transition, slate = sequential
- Token-based object scaling via `_token_scale()` (0.5 - 2.5 range)
- File-flow connections (`_infer_file_flow_connections`) detect shared file paths across phases
- `SceneData.to_json()` produces a JSON blob that the frontend JS consumes; `to_dict()` for programmatic access
- Camera positioning (`_default_camera`) auto-frames the scene with preset positions
- Bridge paths between adjacent platforms for visual phase-to-phase flow
- `build_scene()` module-level convenience function
- `engine/visualization/__init__.py` updated with scene builder exports
- 86 tests covering: dataclasses (SceneObject 4, SceneConnection 2, ScenePlatform 3, SceneData 4), constants (4), helpers (truncate 3, status 4, token scale 6, grouping 2), aggregate phase status (7), infer data type (6), build scene object (5), camera (3), file flow connections (5), SceneBuilder.build integration (14), edge cases (9), build_scene convenience (2), summary builder (2)

**Deliverable**: Python backend that transforms `execution.json` into a Three.js-compatible scene graph. Fully testable without a browser.

### 9.2 Three.js Frontend Renderer ✅
- `templates/visual-report/scene-renderer.js` — `RalphSceneRenderer` IIFE module + `renderScene()` entry point
- Creates WebGL scene from scene graph JSON: camera with configurable FOV/near/far, ambient + directional + fill lighting with status-adaptive tinting (warm=success, cool=failure), platform meshes, action object meshes, Bezier-curved connection lines
- Camera controls: OrbitControls (rotate, zoom, pan) with damping, configurable min/max distance, preset positions via `setCameraPreset()`
- Object interaction: raycasting for click detection (`_onClick`), hover highlight with emissive intensity boost + tooltip (`_onMouseMove`), click opens detail panel with human-readable narrative
- Status glow: `MeshStandardMaterial` with per-status emissive intensity (success=0.3, failure=0.6, escalated=0.4, retry=0.35), pulsing `sin()` animation on failed objects
- Minimap: secondary orthographic camera in corner (`_initMinimap`), rendered each frame alongside main scene
- Level-of-detail: `lodThreshold` (default 100 objects) triggers `createLODGeometry()` with reduced polygon counts (IcosahedronGeometry detail 0 vs 1, CylinderGeometry 8 vs 16 segments, SphereGeometry 8×6 vs 16×12)
- Geometry mapping: polyhedra (IcosahedronGeometry) = LLM calls, cubes (BoxGeometry) = file operations, cylinders (CylinderGeometry) = command runs, spheres (SphereGeometry) = API calls
- Detail panel: `renderDetailPanel()` generates human-readable HTML — LLM actions show "What the agent was told"/"Key reasoning"/"By the numbers", file actions show path + content excerpt, command actions show "What was run"/"What happened". No raw JSON exposed.
- WebGL fallback: `_webglAvailable()` checks for webgl/webgl2/experimental-webgl context; `_showFallback()` displays message directing user to 2D views
- Text sprites via `CanvasTexture` for platform labels
- Bridge paths between adjacent platforms as Bezier curves
- `dispose()` cleans up all resources (event listeners, renderers, controls, tooltip)
- `ReportData.scene_data` field added to `report_generator.py`; `extract_report_data()` builds scene via `build_scene()`
- Report template updated: conditional 3D section with scene container + detail panel, Three.js + OrbitControls script inclusion, `renderScene()` call with embedded scene data JSON
- `ReportingConfig.visualization_engine` field added (default: `"threejs"`, alternative: `"d3"`)
- 88 tests covering: JS file structure (23), ReportData field (4), scene data correctness (9), template integration (9), report generator output (8), config (4), JS validation (5), detail panel content (5), renderer options (7), lighting/feedback (4), geometry mapping (5), end-to-end pipeline (5)

**Deliverable**: Frontend JavaScript that renders the 3D scene in the browser. Self-contained, no CDN dependencies.

### 9.3 Timeline Scrubber ✅
- `engine/visualization/scene/timeline.py` — `TimelineData`, `TimelineMarker`, `TimelineEvent` dataclasses with `to_dict()` serialization; `build_timeline()` transforms execution records into timeline structure with phase markers and action events
- `templates/visual-report/timeline.js` — `RalphTimeline` IIFE module + `renderTimeline()` entry point
- Horizontal bar at bottom of 3D viewport showing wall-clock execution time
- Phase-colored marker segments for quick visual orientation and jumping (`seekToPhase()`)
- Draggable scrubber thumb with grab/grabbing cursor feedback and drag highlight
- Play/pause button with configurable playback speed (1x, 2x, 5x, 10x) via `cycleSpeed()`
- Current-time display with tabular-nums formatting
- Event dot indicators on the timeline track, highlighted as timeline progresses
- Scene synchronization: `onTimeChange` callback controls object visibility chronologically; clicking a 3D object snaps the scrubber to that action's timestamp via `seekToEvent()`
- Click-to-seek on the timeline bar; keyboard-independent (no conflict with detail panel arrows)
- `dispose()` cleans up all DOM elements and event listeners
- `ReportData.timeline_data` field added to `report_generator.py`; `extract_report_data()` builds timeline via `build_timeline()`
- Report template updated: conditional timeline section with `renderTimeline()` call, scene click handler wires `seekToEvent()`, `onTimeChange` callback hides future objects during playback
- Package exports updated in `engine/visualization/scene/__init__.py` and `engine/visualization/__init__.py`
- 86 tests covering: dataclasses (TimelineMarker 3, TimelineEvent 3, TimelineData 2), helpers (parse_timestamp 5, ms_between 2, find_timestamps 5, estimate_duration 3), build_markers (10), build_events (8), build_timeline pipeline (9), truncate (3), JS structure (14), template integration (5), ReportData integration (3), report generator e2e (2), package exports (2), edge cases (5), PHASE_COLORS constants (2)

**Deliverable**: Interactive timeline that animates the 3D scene chronologically.

### 9.4 Detail Drill-Down Panels ✅
- `engine/visualization/narrative/` — new package for narrative generation
- `engine/visualization/narrative/__init__.py` — package exports: `NarrativeFormatter`, `enrich_scene_with_narratives`
- `engine/visualization/narrative/formatter.py` — `NarrativeFormatter` class
  - `format_action(action)` dispatches to type-specific formatters
  - `format_llm_call(action)` → "What the agent was told", "What it decided", "Key reasoning", "By the numbers"
  - `format_file_operation(action)` → "What was read/written" (file path, content excerpt), "Why" (linked reasoning), "What changed" (truncated diff)
  - `format_command_run(action)` → "What was run", "What happened" (pass/fail with relevant excerpt, not full log), "What the agent did about it"
  - `format_api_call(action)` → "What was requested", "Result"
  - `format_escalation(action)` → escalation reason with human-review narrative
  - `format_generic(action)` → fallback for unrecognized action types
  - `format_phase_transition(reflection)` → "Why did the agent move on?", "What was carried forward?"
  - `summarize_prompt(system_prompt, context)` — transforms raw system prompts into 1-2 sentence plain-English descriptions by detecting phase keywords (triage, implement, review, validate, CI, report)
  - `extract_key_reasoning(llm_response)` — formats reasoning as readable text, truncates at 2000 chars
  - All formatters produce HTML fragments using `report.html` CSS classes — no raw JSON/YAML exposed
  - `enrich_scene_with_narratives(scene_dict, actions)` — adds `narrative_html` to each scene object's metadata, keyed by action ID
- `templates/visual-report/detail-panel.js` — `RalphDetailPanel` IIFE module
  - `DetailPanel` constructor + `init(actionList)` method
  - Slide-in overlay panel from right side with `cubic-bezier` transition
  - Semi-transparent overlay for click-outside-to-close
  - Close via X button, Escape key, or overlay click
  - Navigation arrows (prev/next) with Left/Right arrow keyboard shortcuts
  - Counter showing current position (e.g., "3 / 12")
  - Renders server-generated `narrative_html` when available, falls back to client-side rendering
  - `buildActionList(sceneData)` utility flattens scene platforms into a navigable action list
  - `dispose()` cleans up DOM elements and event listeners
- `engine/visualization/report_generator.py` — `extract_report_data()` now calls `enrich_scene_with_narratives()` after building the scene, embedding narrative HTML in the scene data JSON
- `templates/visual-report/report.html` — updated 3D section: removed inline detail panel div, replaced with slide-in overlay; includes `detail-panel.js`; wires click handler to open `RalphDetailPanel` instead of inline `renderDetailPanel`; added usage instructions ("Click any object... arrow keys...")
- `engine/visualization/__init__.py` — added `NarrativeFormatter`, `enrich_scene_with_narratives` exports
- 113 tests in `tests/test_detail_panels.py` covering: NarrativeFormatter (LLM calls 7, file ops 8, commands 4, API/escalation/generic 4, dispatch 8, phase transitions 4, prompt summarisation 9, reasoning extraction 4), helpers (16), enrich_scene_with_narratives (7), detail-panel.js structure (15), report template integration (6), ReportGenerator narrative output (5), no-raw-data verification (5), full pipeline (3)

**Deliverable**: Human-readable detail panels for every object in the 3D scene. No raw data formats exposed to the user.

### 9.5 Narrative Summary Landing Page ✅
- `engine/visualization/narrative/summary.py` — `NarrativeSummaryBuilder` class
  - `build_story(execution_data)` → one-paragraph plain-English story of the execution covering issue identification, triage classification + confidence, implementation attempts + files, review verdict, PR URL, timing, and final status
  - `build_metrics_cards(execution_data)` → key metrics cards: total time, iterations, LLM calls, files modified, tests run, total tokens (when nonzero), final status. Each card carries the execution status for conditional styling
  - `build_phase_timeline(execution_data)` → horizontal bar chart data (CSS-rendered, not Three.js) showing time per phase with color, percentage, iteration count, and status
  - `build_landing(execution_data)` → complete `LandingData` combining story, cards, bars, duration display, and comparison summary
- `MetricCard`, `PhaseBar`, `LandingData` dataclasses with `to_dict()` serialization
- `build_landing()` module-level convenience function
- `ReportData.landing_data` field added to `report_generator.py`; `extract_report_data()` builds landing via `build_landing()`
- `build_narrative()` in `publisher.py` now delegates to `NarrativeSummaryBuilder.build_story()` for a richer narrative
- Landing page section in `report.html` template: narrative paragraph, metrics cards, phase timeline bar with color legend, comparison summary (when active), "Enter 3D View" button that scrolls to the 3D scene. Falls back to the simple narrative + metrics when landing data is unavailable
- Helper functions: `_extract_issue_desc`, `_extract_triage_info` (with confidence + escalation reason), `_extract_impl_info` (with files changed), `_extract_review_info` (multi-review attempt tracking), `_count_files_modified`, `_count_tests_run`, `_format_ms`, `_format_duration_display`
- Package exports updated in `narrative/__init__.py` and `visualization/__init__.py`
- 80 tests in `tests/test_narrative_summary.py` covering: dataclasses (MetricCard 2, PhaseBar 2, LandingData 2), helpers (issue desc 5, triage info 5, impl info 4, review info 5, counts 2, format 2), build_story (14: success/failure/escalation/timeout/PR URL/timing/no-phases/empty/multiple-attempts/escalation/confidence/components), build_metrics_cards (8: basic/tokens/status/time/files/serialization), build_phase_timeline (8: basic/percentages/colors/status/iterations/empty/fallback/serialization), build_landing (5: full pipeline/to_dict/comparison/no-comparison/empty), convenience (1), ReportData integration (3), template integration (8: landing-page/story/phase-bars/enter-3d/comparison/fallback/colors/metrics), publisher delegation (2), package exports (2)

**Deliverable**: Landing page that tells the story before the user enters the 3D scene.

### 9.6 Report Assembly and Publishing ✅
- `engine/visualization/report_generator.py` — `ReportGenerator` now accepts `ReportingConfig`, loads vendored JS files from `templates/visual-report/vendor/`, passes them to the template as context variables for inline embedding. `visualization_engine` config drives template routing: `"threejs"` includes full 3D scene, `"d3"` omits 3D section and Three.js/OrbitControls. `extract_report_data()` skips scene/timeline/landing computation in D3 mode.
- `templates/visual-report/vendor/` — Three.js r137 (`three.min.js`, 619KB), OrbitControls r137 (`orbit-controls.min.js`, 26KB), D3.js v7.9 (`d3.v7.min.js`, 280KB) vendored as local files
- `templates/visual-report/report.html` — replaced all CDN `<script src>` tags with inline `{{ vendor_*_js }}` template variables. Added `visualization_engine` guards on 3D sections (scene, "Enter 3D View" button). Report is now a single self-contained HTML file with no external dependencies, works offline (FR-2.9).
- `engine/visualization/scene/builder.py` — `SceneBuilder.add_comparison_ghosts()` overlays translucent ghost objects from the human fix onto the implement platform. Ghost objects have `metadata.ghost=True`, white color, z-offset, and sequential connections. Comparison ghost connections link agent objects to human ghost objects.
- `templates/visual-report/scene-renderer.js` — ghost object rendering: transparent (`opacity: 0.3`), wireframe, no shadows, reduced emissive intensity. Ghost detection via `objData.meta.ghost`.
- `engine/visualization/publisher.py` — passes `ReportingConfig` to `ReportGenerator` constructor
- 49 tests in `tests/test_report_assembly.py` covering: vendor files (6), self-contained HTML (6), D3 legacy mode (8), config routing (5), publisher config (2), comparison ghosts (7), extract_report_data ghosts (2), scene-renderer ghost JS (4), template vendor integration (6), output writing (3)

**Deliverable**: Self-contained 3D HTML report generated as a byproduct of every execution. Legacy 2D mode preserved.

### 9.7 Agent Sidebar Navigation ✅
- `engine/visualization/report_generator.py` — `_build_agents_data()` extracts per-agent metadata (name, description, icon, source file path, prompt file path, allowed tools, status, timing, iteration count, LLM calls) from `phases_summary` and `iterations`. `_AGENT_DESCRIPTIONS` and `_AGENT_ICONS` constants provide human-readable descriptions and emoji icons for each phase agent. `ReportData.agents` field added. `engine_repo_url` passed to template context from `ReportingConfig`.
- `engine/config.py` — `ReportingConfig.engine_repo_url` field (default: `https://github.com/ascerra/rl-bug-fix-full-send`) used to construct GitHub links to agent source files.
- `templates/visual-report/report.html` — fixed left sidebar (`nav.agent-sidebar`) listing all phase agents with status indicators (success/failure dot), timing, iteration count, LLM call count, and GitHub source links. Scroll-tracking JS highlights the active agent as user scrolls. Section navigation links (Phase Summary, Decision Tree, Action Map, Timeline, LLM Log, Actions Log). Mobile-responsive: sidebar slides in/out via hamburger toggle on screens ≤1100px. Body layout restructured with `.main-content` wrapper offset by sidebar width. Section heading IDs added for scroll-to navigation.

### Phase 9 Build Order

| Item | Effort | Depends on | Priority |
|------|--------|------------|----------|
| 9.1 Scene foundation ✅ | 2 sessions | Phase 7 complete | Critical |
| 9.2 Three.js renderer ✅ | 2-3 sessions | 9.1 | Critical |
| 9.3 Timeline scrubber ✅ | 1 session | 9.2 | High |
| 9.4 Detail drill-down panels ✅ | 2 sessions | 9.1 + 9.2 | Critical |
| 9.5 Narrative summary landing ✅ | 1 session | 9.4 | High |
| 9.6 Report assembly ✅ | 1 session | 9.1-9.5 | Critical |
| 9.7 Agent sidebar navigation ✅ | 0.5 session | 9.6 | Medium |

## Phase 10: Implement-First Workflow Execution and CI Remediation

The engine currently pushes changes during the validate phase. This phase restructures the flow so that all implementation and review iterations happen locally first, and the engine only pushes when it has a fully reviewed change. After pushing, the engine monitors the target repo's CI workflow, downloads results, and iterates on failures. See SPEC.md §5.7 and FR-6.

### 10.1 Validate Phase Restructure (Implement-First) ✅
- `engine/config.py` — `CIRemediationConfig` dataclass with all SPEC §8 fields: `enabled`, `max_iterations`, `time_budget_minutes`, `ci_poll_interval_seconds`, `ci_poll_timeout_minutes`, `rerun_on_infrastructure_flake`, `max_flake_reruns`, `failure_categories` (dict mapping failure types to actions). Wired into `EngineConfig` with YAML loading via `_apply_ci_remediation_config()` (merges `failure_categories` into defaults).
- `engine/phases/validate.py` — added `_is_ready_to_push()` composite gate checking 4 prerequisites before any push/PR creation: (a) review phase approved (`_has_review_approval()` checks latest review result — zero trust, only most recent review counts), (b) local lint checks pass, (c) LLM validation ready, (d) tests pass (only in `required` mode). Returns `(ready, list_of_blockers)` tuple.
- Refactored `act()` to use `_is_ready_to_push()` as the single push gate. Added `push_blockers` field to act result. Removed the old inline gate logic.
- Loop ordering (`PHASE_ORDER`) already ensures implement→review completes before validate. The validate gate independently verifies review approval (defense in depth).
- 34 new tests covering: `CIRemediationConfig` (7: defaults, failure categories, YAML scalars, merge, unknown fields, no-section), `_has_review_approval()` (8: findings path, artifacts path, request_changes, no review, empty, latest wins, rejected latest, non-review phases), `_is_ready_to_push()` (8: all pass, review blocks, lint blocks, LLM blocks, tests in required/disabled/opportunistic, multiple blockers), `act()` enforcement (5: no PR without review, push_blockers in result, no tools, PR attempt, request_changes), loop ordering (3: PHASE_ORDER assertions, full loop run), backward compatibility (3).

**Deliverable**: Validate phase only pushes after full local approval. No partial pushes.

### 10.2 CI Monitor and Result Downloader ✅
- `engine/workflow/ci_monitor.py` — `CIMonitor` class using the GitHub Check Runs API (not commit statuses)
  - `poll_ci_status(ref)` — polls check runs for a git ref until all complete or timeout; configurable `poll_interval` and `poll_timeout` overrides
  - `download_ci_results(ref)` — single-fetch check runs with full output and per-run annotation fetching via `/check-runs/{id}/annotations`
  - `download_workflow_log(workflow_run_id)` — downloads combined workflow run log (truncated to 50 KB for LLM context)
  - `categorize_failure(ci_result)` → `CIFailureCategory` enum with priority ordering: infrastructure flake > timeout > build error > lint violation > test failure > unknown. Uses keyword matching against frozen sets of ~50 terms.
  - `extract_failure_details(ci_result, category)` → `FailureDetails` dataclass with: summary, failing check names, error messages, failing test names (extracted via regex for Go/Python/JS/Rust patterns), annotations, log excerpts, workflow run IDs, and recommended action (remediate/rerun/escalate)
  - `trigger_rerun(workflow_run_id)` and `trigger_rerun_failed_jobs(workflow_run_id)` — re-trigger full or failed-only workflow runs via GitHub API
- `CIFailureCategory` StrEnum, `CheckRunResult`, `CIResult`, `FailureDetails` dataclasses with `to_dict()` serialisation
- `CIResult.passed` and `CIResult.failed_runs` properties for quick status checks
- Configuration from `CIRemediationConfig` (poll interval, timeout), with per-call overrides
- Uses httpx directly (not GitHubAdapter) for check-run-level detail (annotations, log URLs, workflow run IDs)
- 91 tests covering: dataclasses (15), enum (2), module helpers (16), constructor/config (3), poll_ci_status (11), download_ci_results (3), download_workflow_log (3), categorize_failure (13), extract_failure_details (9), trigger_rerun (6), HTTP helpers (2), integration/round-trip (4)

**Deliverable**: CI monitoring and failure analysis module. Testable with mock GitHub API responses.

### 10.3 CI Remediation Loop ✅
- `engine/phases/ci_remediate.py` — `CIRemediatePhase` class implementing full OODA cycle (observe, plan, act, validate, reflect)
  - Triggered after PR creation when CI fails — invoked by the loop's CI monitoring sub-loop, not via `PHASE_ORDER`
  - `observe()` — reads CI failure details from `issue_data` (injected by loop), extracts prior remediation attempts from `prior_results`, reads failing file contents
  - `plan()` — sends failure context + prior attempts + original diff to LLM with `ci_remediate.md` prompt; records LLM call via `record_llm_call()` for metrics sync
  - `act()` — applies file changes, commits with `fix(ci):` prefix, pushes to PR branch; handles no-code-fix (infrastructure flake → `needs_rerun`), empty file_changes, and push failures gracefully
  - `validate()` — runs local lint using triage-inherited repo stack (or independent detection fallback)
  - `reflect()` — returns structured outcomes: `pushed` (success), `needs_rerun` (flake), `no_fix`, `lint_failed`, `push_failed`
- `templates/prompts/ci_remediate.md` — prompt template with: CI failure context structure, prior attempt avoidance instructions, JSON output format (code fix and rerun variants), untrusted content delimiter for CI error messages/logs
- `engine/phases/base.py` — added `CI_REMEDIATE_TOOLS` (implement tools + `github_api`) to `PHASE_TOOL_SETS`
- Integration with `engine/loop.py`:
  - `_pr_was_created()` static method detects successful PR creation from validate result
  - After validate phase creates PR, loop calls `_run_ci_monitoring_loop()` — a self-contained sub-loop with its own iteration counter and time budget from `CIRemediationConfig`
  - Sub-loop: poll CI via `CIMonitor.poll_ci_status()` → if pass → success → if fail → categorize → route to remediate/rerun/escalate
  - `remediate` action: execute `CIRemediatePhase` via `_execute_ci_remediation()` which injects failure details, branch name, original diff, and remediation iteration into `issue_data`
  - `rerun` action: trigger workflow rerun via `CIMonitor.trigger_rerun()`, tracks flake rerun count against `max_flake_reruns`
  - `escalate` action: records escalation with full failure context
  - CI poll timeout (checks never complete) → escalates
  - Main loop time budget checked inside sub-loop — never exceeds overall budget
  - `_extract_branch_from_pr()` and `_extract_repo_parts_from_url()` helpers for PR/repo identification
- Phase registration in `engine/__main__.py` — `ci_remediate` registered alongside other phases
- Module-level helpers: `_extract_failing_files()` (from annotations + error messages), `_build_trusted_context()` / `_build_untrusted_context()` (structured context builders), `_parse_remediation_response()` (JSON + code block extraction with fallback)
- 62 tests in `tests/test_ci_remediate.py` covering: phase attributes (3), module helpers (14: extract files, trusted/untrusted context, response parsing), OODA cycle (observe 3, plan 3, act 4, validate 2, reflect 5, full execute 2), loop helpers (9), CI monitoring disabled/no-token (2), CI monitoring loop (8: CI pass, fail→remediate, iteration cap, timeout escalation, flake rerun, flake limit, poll timeout, no-branch), unregistered phase (1), registration (2), prompt template (3)

**Deliverable**: Full CI remediation loop that detects, categorizes, and fixes CI failures automatically.

### 10.4 CI Failure Context Injection ✅
- `templates/prompts/ci_remediate.md` — enhanced prompt template with per-category remediation strategies (test_failure, build_error, lint_violation, infrastructure_flake, timeout), structured trusted/untrusted context sections, detailed prior-attempt guidance, non-empty file_changes rule
- `engine/phases/ci_remediate.py` — `_extract_prior_attempts()` now extracts `lint_output` and `expected_resolution` from prior results. `_build_trusted_context()` prior attempts section now includes full analysis, fix strategy, files changed (capped at 10), lint output, and expected resolution with truncation. Prior attempts show "succeeded"/"FAILED" outcome labels.
- `engine/phases/implement.py` — no changes needed; CI remediation uses `CIRemediatePhase` directly, not the implement phase
- 33 new tests covering: prompt category strategies (9 tests: per-category sections, human-readable content, no raw JSON), enhanced prior attempts (4 tests: lint_output extraction, expected_resolution, empty defaults, phase filtering), enhanced trusted context (10 tests: analysis, files_changed, lint_output, expected_resolution, outcome labels, strategy formatting, no raw JSON, truncation), context no-raw-JSON (3 tests: trusted, untrusted, plan LLM call), plan failure context (7 tests: category, failing tests, error messages, annotations, log excerpts, prior analysis, original diff)

**Deliverable**: CI failure context flows cleanly into the remediation LLM calls.

### 10.5 PR Comment Reporting ✅
- `engine/workflow/ci_monitor.py` — `build_ci_pr_comment()` function + `CIRemediationAttempt` and `CIRemediationHistory` dataclasses with `to_dict()` serialization
- Three comment variants: success (CI failure history, what was tried, what worked), escalation (full failure context, error messages, annotations, actionable manual fix suggestions per failure category), and flake (infrastructure rerun count)
- `_format_success_comment()`, `_format_escalation_comment()`, `_format_flake_section()`, `_format_generic_comment()`, `_generate_suggestions()`, `_format_elapsed()` helper functions
- Escalation comments include collapsible `<details>` sections for error messages and CI annotations
- Per-category suggestions: test failures → assertion review, build errors → import/type checks, lint violations → local linter, infrastructure flakes → re-run, timeouts → performance regression check
- `engine/loop.py` — `_run_ci_monitoring_loop()` tracks `CIRemediationHistory` throughout the sub-loop and posts comment via `_post_ci_pr_comment()` using `GitHubAdapter.post_comment()`
- `_extract_pr_number_from_url()` helper extracts PR number from GitHub PR URLs
- Comment posted on any CI remediation activity (attempts or escalation); skipped on immediate CI success (no noise)
- Comment posting failures are logged but never block loop completion (non-blocking)
- 40 tests in `tests/test_ci_pr_comment.py` covering: dataclasses (5), _format_elapsed (4), _generate_suggestions (6), success comment (4: no attempts, with attempts, truncation, footer), escalation comment (4: basic, with attempts, timeout, no failure), flake section (2), generic comment (2), _extract_pr_number_from_url (5), _post_ci_pr_comment (4: skip no PR, post success, handle failure, handle exception), CI monitoring loop integration (2: escalation posts comment, immediate success skips), imports (2)

**Deliverable**: Informative PR comments that document the CI remediation process.

### Phase 10 Build Order

| Item | Effort | Depends on | Priority |
|------|--------|------------|----------|
| 10.1 Validate restructure ✅ | 1 session | Phase 7 complete | Critical |
| 10.2 CI monitor ✅ | 1 session | 10.1 | Critical |
| 10.3 CI remediation loop ✅ | 2 sessions | 10.1 + 10.2 | Critical |
| 10.4 Failure context injection ✅ | 1 session | 10.3 | High |
| 10.5 PR comment reporting ✅ | 0.5 session | 10.3 | Medium |

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
                                                                            │
                                                                            ▼
                                                    Phase 7 (Observability & Feedback Loops)
                                                                            │
                                                              ┌─────────────┼─────────────┐
                                                              ▼             ▼             ▼
                                                    Phase 8        Phase 9        Phase 10
                                                  (Observer)    (3D Reports)   (CI Remediation)
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
| Phase 7 | 5-8 loop sessions | Phase 6 + production run data |
| Phase 8 | 5-6 loop sessions | Phase 7 |
| Phase 9 | 8-10 loop sessions | Phase 7 |
| Phase 10 | 4-6 loop sessions | Phase 7 |

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
