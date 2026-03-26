# RL Bug Fix Full Send

An agentic SDLC engine that uses iterative **Ralph Loops** to autonomously triage, implement, review, and report on bug fixes in GitHub-hosted repositories.

<!-- Overview diagram rendered inline as SVG below -->

## What Does It Do?

Given a GitHub issue describing a bug, the engine:

1. **Triages** it — classifies severity, identifies affected files, attempts reproduction
2. **Implements** a fix — reads code, identifies root cause, writes a minimal patch, runs linters
3. **Self-reviews** the fix — independent zero-trust review for correctness, intent alignment, security, and scope
4. **Validates** and opens a PR — verifies minimal diff, generates a detailed PR description, pushes to the target repo
5. **Reports** — produces interactive HTML evidence (decision trees, action maps, execution traces)

If the engine gets stuck, it **escalates to a human** with full context of everything it tried.

The engine runs entirely in **GitHub Actions** — no local setup required for production use.

![Phase Pipeline](docs/images/phase-pipeline.png)

## Production Results

The engine has been validated against real [Konflux](https://github.com/konflux-ci) bugs with known human fixes.

### KONFLUX-11443: Race Condition in FIPS Check

**Bug**: The `fbc-fips-check-oci-ta` Tekton task failed inconsistently during parallel image processing — temp file paths collided when images shared identical `component-version-release` labels.

Both fixes identified the same root cause and used the same strategy (make temp paths unique per parallel job). The Ralph Loop matched the human's solution in **2.8 minutes** with better documentation. The human fix scored higher on precision — every path was perfectly consistent, while the Ralph Loop dropped a `:latest` suffix in one cleanup path.

This analysis led directly to improvements: a **deterministic path-consistency checker** now catches these mismatches automatically (see [Continuous Improvement](#continuous-improvement)).

![KONFLUX-11443 Comparison](docs/images/konflux-comparison.png)

## What is a Ralph Loop?

A Ralph Loop is our adaptation of the [Ralph Wiggum Loop](https://ghuntley.com/ralph/), an agentic iteration pattern created by Geoffrey Huntley in 2025. The core idea:

> Run an AI agent in a loop. Feed failures back as context. Iterate until an objective success criterion is met. **Iteration beats perfection; failures are data.**

Our production loop adds structure with a phased OODA execution model. Each phase (triage, implement, review, validate, report) runs this full cycle independently. Phases validate each other with **zero trust** — the review phase re-reads the issue and diff from scratch rather than trusting the implementation phase's summary.

![OODA Loop Cycle](docs/images/ooda-loop.png)

### Two Levels of Loop

| Loop | Where | Purpose |
|------|-------|---------|
| **Meta Loop** | Your laptop (Cursor, Claude Code, etc.) | Builds and iterates on the engine itself |
| **Production Loop** | GitHub Actions | Executes the bug fix workflow against target repos |

The meta loop built the production system over 51 iterations (see [progress/run-log.md](progress/run-log.md)). The production system then runs autonomously in CI.

## Architecture

![Engine Architecture](docs/images/architecture.png)

### Engine Components

| Layer | Module | Responsibility |
|-------|--------|----------------|
| **Orchestration** | `engine/loop.py` | Phase dispatch, transitions, iteration cap, time budget, escalation, retry backoff |
| **Phases** | `engine/phases/` | Triage, implement, review, validate, report — each with OODA cycle |
| **Tools** | `engine/tools/executor.py` | Sandboxed file ops, shell commands, git operations (7 tools) |
| **LLM** | `engine/integrations/llm.py` | Gemini (primary) + Anthropic (fallback), provider-agnostic interface |
| **Integrations** | `engine/integrations/` | GitHub (core), Slack and Jira (optional, off by default) — all with injection guards |
| **Observability** | `engine/observability/` | Structured JSON logging, action tracing, metrics, live narration |
| **Visualization** | `engine/visualization/` | HTML reports, D3.js decision trees and action maps, comparison views |
| **Security** | `engine/secrets.py` | Secret loading, validation, redaction across all outputs |
| **Self-improvement** | `engine/golden_principles.py` | AST-based static analyzer enforcing 7 golden principles |

### Phase Pipeline

```
┌──────────┐     ┌───────────┐     ┌──────────┐     ┌──────────┐     ┌──────────┐
│  TRIAGE  │────►│ IMPLEMENT │────►│  REVIEW  │────►│ VALIDATE │────►│  REPORT  │
│          │     │           │     │          │     │          │     │          │
│ classify │     │ root cause│     │ zero-trust│    │ tests    │     │ HTML     │
│ severity │     │ write fix │     │ correctness   │ lint     │     │ decision │
│ find files│    │ test/lint │     │ security │     │ PR create│     │ tree     │
│ reproduce│     │ iterate   │     │ scope    │     │ CI check │     │ action   │
│          │     │           │     │          │     │          │     │ map      │
└──────────┘     └───────────┘     └──────────┘     └──────────┘     └──────────┘
                       ▲                 │
                       │    reject       │
                       └─────────────────┘
                    (request_changes with
                     specific suggestions)
```

Each phase uses **phase-specific prompts** (in `templates/prompts/`) and **phase-specific tool restrictions** (e.g., the review phase cannot write files or run shell commands — it can only read).

### Security Model

The engine treats all external data as **untrusted** and wraps it in delimiters before passing to the LLM:

| Source | Trust Level | Handling |
|--------|-------------|----------|
| Issue body | UNTRUSTED | Wrapped in delimiters, never in system prompts |
| Code diff | UNTRUSTED | Wrapped in delimiters, reviewed independently |
| Slack messages | UNTRUSTED | Wrapped by integration adapter |
| Jira data | UNTRUSTED | Wrapped by integration adapter |
| System prompts | TRUSTED | Never contain user-supplied data |
| Config | TRUSTED | From repo, not from user input |
| Prior phase output | VERIFIED | Each phase re-reads source material independently |

- **127 prompt injection tests** verify that untrusted content never leaks into system prompts
- **59 security audit tests** verify commit signing, provenance recording, secret redaction
- All secrets redacted from logs, traces, artifacts, and LLM transcripts

### Testing Strategy: CI-First (Tests Are Disabled)

**Test execution is disabled by default.** This is a deliberate architectural decision, not a missing feature.

The engine targets arbitrary GitHub repositories. Running their test suites inside the GitHub Actions runner is unreliable and risky because:

- The correct language runtime/version may not be installed
- Dependencies may require Docker, databases, kind clusters, or specialized infrastructure the runner lacks
- Test suites may exceed timeouts (real suites can run 20+ minutes)
- Pre-existing flaky tests waste the iteration budget chasing unrelated failures
- Executing arbitrary shell commands from untrusted repos is a security surface

**Instead, the engine relies on the target repo's own CI pipeline** — which has the correct build matrix, services, secrets, and infrastructure — to validate the fix after the PR is created.

| Mode | Behavior | When Used |
|------|----------|-----------|
| `disabled` (default) | Tests skipped entirely; linting still runs | All repos unless configured otherwise |
| `opportunistic` | Tests run but failures don't block the loop | Auto-promoted when `test_command` is configured in `.rl-config.yaml` |
| `required` | Tests must pass before PR is created | Explicitly set in `.rl-config.yaml` |

Configure via `.rl-config.yaml` in the target repo:

```yaml
phases:
  implement:
    test_command: "go test ./..."
    test_execution_mode: "opportunistic"  # or "required"
  validate:
    test_command: "go test ./..."
    test_execution_mode: "opportunistic"
```

**Linting is always enabled** — it's cheap, fast, and works across repos without special infrastructure.

## Continuous Improvement

The engine improves itself through multiple feedback mechanisms:

| Feedback Source | Analysis | Improvement |
|----------------|----------|-------------|
| `execution.json` from production runs | Deterministic checks (path consistency, paired operations) | Prompt updates (`review.md`, `implement.md`) + code safety nets (`review.py`) |
| Repeated LLM call patterns | Pattern detection (`extraction.py`) | Deterministic tool proposals replacing expensive LLM calls |
| Engine source code | Golden principles (7 structural properties) | AST-based enforcement via `golden_principles.py` |
| Code metrics + scan results | Background quality scanner | Weekly cron scan + auto-created GitHub issues on critical findings |

**Recent improvement (Run 51)**: After comparing the engine's fix for KONFLUX-11443 against the human fix, we identified that the self-review phase missed a subtle OCI tag mismatch (`:latest` dropped from a cleanup path). Three changes were made:

1. **Review prompt** — added "Consistency of Paired Operations" as review dimension #6
2. **Implement prompt** — added "Consistency Requirements" section for path/parameter alignment
3. **Deterministic checker** — `_check_path_consistency()` in `review.py` runs post-LLM as a safety net, catching path mismatches the LLM might miss

## Quick Start

### Prerequisites

- **Python 3.12+** with `uv` package manager
- **GitHub CLI** (`gh`) installed and authenticated
- **API key**: `GEMINI_API_KEY` (recommended) or `ANTHROPIC_API_KEY`

### Run Locally

```bash
# Install
uv pip install -e ".[dev]"

# Test
make test

# Lint
make lint

# Run the engine
python -m engine --issue-url <ISSUE_URL> --target-repo <PATH> --output-dir ./output
```

### Run in GitHub Actions

1. Set repository secrets:

| Secret | Required | Description |
|--------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `GH_PAT` | Yes | GitHub PAT with `repo` scope |
| `ANTHROPIC_API_KEY` | No | Fallback LLM |
| `SLACK_BOT_TOKEN` | No | Slack notifications |
| `JIRA_API_TOKEN` | No | Jira integration |

2. Go to **Actions** → **Ralph Loop - Bug Fix Engine** → **Run workflow**
3. Enter the issue URL and optional parameters
4. View results in the workflow artifacts

### Test Against a Known Bug

```bash
# Fork a repo and roll back to before the fix
./scripts/setup-fork.sh \
  konflux-ci/build-definitions \
  your-org \
  <commit-before-fix> \
  https://github.com/konflux-ci/build-definitions/issues/123

# Trigger the engine via meta-loop runner
./scripts/meta-loop.sh --issue-url <FORK_ISSUE_URL>
```

## Project Structure

```
rl-bug-fix-full-send/
├── SPEC.md                         # Technical specification
├── ARCHITECTURE.md                 # Architecture decisions (6 ADRs)
├── IMPLEMENTATION-PLAN.md          # Phased build plan (7 phases + hardening)
├── prompt.md                       # Meta ralph loop instruction file
│
├── .github/workflows/
│   ├── ralph-loop.yml              # Main engine workflow
│   └── quality-scan.yml            # Weekly background quality scan
│
├── engine/                         # Python engine package
│   ├── __main__.py                 # CLI entry point
│   ├── config.py                   # Configuration system
│   ├── loop.py                     # Ralph Loop core engine
│   ├── secrets.py                  # Secret management + redaction
│   ├── golden_principles.py        # AST-based linter (7 principles)
│   ├── quality_scanner.py          # Background quality scanner
│   │
│   ├── phases/                     # Phase implementations
│   │   ├── base.py                 #   Base phase class (OODA cycle)
│   │   ├── prompt_loader.py        #   Jinja2 prompt template loading
│   │   ├── triage.py               #   Classify, verify, reproduce
│   │   ├── implement.py            #   Root cause, fix, test, lint
│   │   ├── review.py               #   Independent zero-trust review
│   │   ├── validate.py             #   Tests, lint, PR creation
│   │   └── report.py               #   Visual evidence generation
│   │
│   ├── integrations/               # External system adapters
│   │   ├── llm.py                  #   Gemini + Anthropic providers
│   │   ├── github.py               #   Issues, PRs, CI, commit signing
│   │   ├── slack.py                #   Notifications, channel monitoring
│   │   ├── jira.py                 #   Issues, comments, transitions
│   │   └── discovery.py            #   Auto-detect available integrations
│   │
│   ├── observability/              # Logging, tracing, metrics
│   │   ├── logger.py               #   JSON logger + live narration
│   │   ├── tracer.py               #   Action recording
│   │   ├── metrics.py              #   Counters and gauges
│   │   └── transcript.py           #   LLM call transcript recording
│   │
│   ├── tools/                      # Sandboxed tool execution
│   │   ├── executor.py             #   ToolExecutor + 7 tools
│   │   ├── extraction.py           #   Pattern detection + proposals
│   │   └── test_runner.py          #   Auto-detect test/lint commands
│   │
│   ├── workflow/                    # GitHub Actions self-monitoring
│   │   └── monitor.py              #   CI health checks, step failures
│   │
│   └── visualization/              # Report generation
│       ├── report_generator.py     #   HTML via Jinja2 + D3.js
│       ├── decision_tree.py        #   Interactive decision tree
│       ├── action_map.py           #   Layered phase action map
│       ├── comparison.py           #   Agent vs human fix comparison
│       └── publisher.py            #   Report assembly + publishing
│
├── templates/
│   ├── prompts/                    # Phase-specific LLM prompts
│   │   ├── triage.md
│   │   ├── implement.md
│   │   ├── review.md
│   │   ├── validate.md
│   │   └── report.md
│   └── visual-report/              # HTML report templates
│
├── scripts/
│   ├── setup-fork.sh               # Fork + rollback for testing
│   ├── meta-loop.sh                # CI runner (trigger → monitor → analyze)
│   └── gen-progress.py             # Dashboard generator
│
├── tests/                          # 1945+ tests
│   ├── test_loop.py                #   55 loop behavior tests
│   ├── test_e2e.py                 #   46 end-to-end pipeline tests
│   ├── test_prompt_injection.py    #   127 injection defense tests
│   ├── test_security_audit.py      #   59 security property tests
│   └── ...                         #   Phase, integration, visualization tests
│
├── progress/
│   ├── run-log.md                  # 51 meta loop runs documented
│   └── index.html                  # Auto-generated dashboard (gitignored)
│
├── meta-loop-runs/                 # Production run artifacts (gitignored)
│
├── pyproject.toml                  # Python project config
├── Makefile                        # Build targets
└── ruff.toml                       # Linter config
```

## Execution Traceability

Every run produces full traceability regardless of success or failure:

**Real-time** (visible in GitHub Actions log):
```
>>> [TRIAGE] Classified as bug (confidence: 0.85, severity: high).
>>> [IMPLEMENT] Fix strategy: make paths unique. 1 file change(s) proposed.
>>> [REVIEW] Verdict: approve. 1 nit finding. Confidence: 1.00.
>>> [VALIDATE] PR created. CI status: pending.
```

**Artifacts** (downloadable from the workflow run):

| File | Contents |
|------|----------|
| `execution.json` | Complete machine-readable execution log with all phases, findings, artifacts, LLM calls |
| `progress.md` | Running human-readable narrative, continuously appended during execution |
| `summary.md` | Iteration trace with per-phase pass/fail, duration, findings (piped to `$GITHUB_STEP_SUMMARY`) |
| `report.html` | Interactive D3.js visualizations — decision tree, action map, narrative |
| `log.json` | Structured JSON logs with correlation IDs |
| `transcript.html` | Full LLM call transcripts (prompts, responses, token counts) |
| `status.txt` | Final status: `success`, `escalated`, or `error` |

**On crash**: the execution record captures which OODA step failed, what partial context was gathered, and the full Python traceback.

## Development History

The system was built iteratively over **51 meta loop runs** using a ralph loop on a laptop:

| Phase | Description | Status |
|-------|-------------|--------|
| **Phase 0** | Foundation (package, LLM, logging, config, tools) | Complete |
| **Phase 1** | Core loop engine (orchestrator, 5 phases) | Complete |
| **Phase 2** | GitHub Actions (workflow, monitoring, secrets) | Complete |
| **Phase 3** | Visualization (reports, decision tree, action map, comparison) | Complete |
| **Phase 4** | Integrations (GitHub, Slack, Jira, discovery) | Complete |
| **Phase 5** | Hardening (injection tests, e2e tests, security audit) | Complete |
| **Phase 6** | Self-improvement (golden principles, extraction, quality scanner) | Complete |
| **Phase 7** | Production observability (18 deficiencies found and fixed from live runs) | Complete |
| **Post-7** | KONFLUX-11443 validation, path-consistency checker, review hardening | Complete |

**1945+ tests passing**, lint clean, golden principles enforced.

Key milestones from the development journey:

- **Run 1–10**: Foundation and core loop engine built
- **Run 11–30**: GitHub Actions integration, visualization, and reporting
- **Run 31–40**: Integration layer (GitHub, Slack, Jira) and hardening
- **Run 41–50**: Production observability — 18 deficiencies cataloged from live runs and all resolved (issue fetching, retry adaptation, review leniency, live narration, stack detection handoff, CI-first testing)
- **Run 51**: Post-mortem of KONFLUX-11443 human-vs-AI comparison, deterministic path-consistency checker added

## Design Principles

- **Security is the foundation** — adversarial thinking in every component, 127 injection tests
- **Zero trust between phases** — each phase validates independently, re-reads sources
- **The repo is the coordinator** — branch protection and CODEOWNERS decide what merges
- **Demos are a byproduct** — every execution generates its own visual evidence
- **Everything is auditable** — every action logged, every decision traceable
- **Technology agnostic** — LLM provider, agent runtime, and target stack are all swappable
- **Iteration beats perfection** — failures are data, not dead ends

## Documentation

| Document | Purpose |
|----------|---------|
| [SPEC.md](SPEC.md) | Full technical specification |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Architecture decisions and rationale (6 ADRs) |
| [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) | Phased build plan with completion status |
| [prompt.md](prompt.md) | Meta ralph loop instructions |
| [progress/run-log.md](progress/run-log.md) | Append-only history of all 51 meta loop runs |
