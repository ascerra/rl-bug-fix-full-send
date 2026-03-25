# RL Bug Fix Full Send

Ralph Loop Bug Fix Engine — an agentic SDLC system that uses iterative Ralph Loops to autonomously triage, implement, review, test, and report on bug fixes in GitHub-hosted repositories.

## What is this?

This project builds an engine that can:

1. Take a GitHub issue describing a bug
2. Triage it (classify, identify affected components, attempt reproduction)
3. Implement a fix (write code, write tests, run linters)
4. Self-review the fix (correctness, intent alignment, security, scope)
5. Open a pull request with full documentation
6. Produce interactive visual evidence of every decision and action taken
7. Compare its fix against a known human fix (for validation)

The engine runs in **GitHub Actions** and is designed to eventually drive the entire SDLC for a GitHub organization.

## What is a Ralph Loop?

A Ralph Loop is our adaptation of the [Ralph Wiggum Loop](https://ghuntley.com/ralph/), an agentic iteration pattern created by Geoffrey Huntley in 2025. The core idea: run an AI agent in a loop, feed failures back as context, and iterate until an objective success criterion is met. **Iteration beats perfection; failures are data.**

Our production loop adds structure with a phased execution model:

```
OBSERVE → PLAN → ACT → VALIDATE → REFLECT → (repeat or escalate)
```

Rather than deploying many separate agent services that coordinate via side-channels, the Ralph Loop IS the agent. One loop execution encompasses all phases with specialized prompts and tools at each phase. See [SPEC.md §1.1](SPEC.md#11-what-is-a-ralph-loop) for the full explanation and [ARCHITECTURE.md](ARCHITECTURE.md) for the design rationale.

## Project Structure

```
rl-bug-fix-full-send/
├── SPEC.md                    # Technical specification (read this first)
├── ARCHITECTURE.md            # Architecture decisions
├── IMPLEMENTATION-PLAN.md     # Phased build plan
├── prompt.md                  # Meta ralph loop instruction file
├── .github/workflows/         # GitHub Actions workflows
│   ├── ralph-loop.yml         # Main engine workflow
│   └── quality-scan.yml       # Weekly background quality scan
├── engine/                    # Python engine package
│   ├── __main__.py           # CLI entry point
│   ├── config.py             # Configuration system
│   ├── loop.py               # Ralph Loop core engine
│   ├── phases/               # Phase implementations
│   │   ├── base.py           # Base phase class
│   │   ├── prompt_loader.py  # Jinja2 prompt template loading
│   │   ├── triage.py         # Triage phase (classify, verify, reproduce)
│   │   ├── implement.py      # Implementation phase (fix, test, lint)
│   │   ├── review.py         # Review phase (correctness, intent, security, scope)
│   │   └── validate.py       # Validation phase (test, lint, minimal diff, PR)
│   ├── secrets.py            # Secret management and redaction
│   ├── golden_principles.py  # SPEC §7 enforcement (AST-based linter)
│   ├── quality_scanner.py    # Background quality scanner (periodic scans)
│   ├── integrations/         # External system adapters
│   │   ├── llm.py            # LLM provider abstraction
│   │   ├── github.py         # GitHub REST API adapter (IntegrationAdapter)
│   │   ├── slack.py          # Slack Web API adapter (IntegrationAdapter)
│   │   ├── jira.py           # Jira REST API adapter (IntegrationAdapter)
│   │   └── discovery.py      # Integration discovery service (FR-4.8)
│   ├── observability/        # Logging, tracing, metrics
│   │   ├── logger.py         # Structured JSON logger
│   │   ├── tracer.py         # Action tracing
│   │   └── metrics.py        # Metrics collection
│   ├── tools/                # Sandboxed tool execution
│   │   ├── executor.py       # ToolExecutor + 7 tools
│   │   └── extraction.py     # Deterministic tool extraction from LLM patterns
│   ├── workflow/             # GitHub Actions self-monitoring
│   │   └── monitor.py        # WorkflowMonitor + health checks
│   └── visualization/        # Report generation
├── templates/
│   ├── prompts/              # Phase-specific LLM prompts
│   └── visual-report/        # HTML report templates
├── progress/
│   ├── run-log.md            # Append-only meta loop run history
│   └── index.html            # Auto-generated progress dashboard (gitignored)
├── scripts/
│   ├── setup-fork.sh         # Prepare test repo with known bug
│   └── gen-progress.py       # Dashboard generator
├── tests/                    # Test suite
├── docs/                     # User and developer documentation
├── pyproject.toml            # Python project config
├── Makefile                  # Build targets
└── ruff.toml                 # Linter config
```

## Quick Start

### Prerequisites

1. **Python 3.12+** with `uv` package manager
2. **GitHub CLI** (`gh`) installed and authenticated
3. **API keys** for at least one LLM provider:
   - `GEMINI_API_KEY` — Google Gemini (recommended for MVP)
   - `ANTHROPIC_API_KEY` — Anthropic Claude (fallback)

### Local Development

```bash
# Install dependencies
uv pip install -e ".[dev]"

# Run tests
make test

# Run linter
make lint

# Run the engine (once implemented)
python -m engine --issue-url <ISSUE_URL> --target-repo <PATH> --output-dir ./output
```

### GitHub Actions Setup

To run the engine in GitHub Actions, you need these repository secrets:

| Secret | Required | Description |
|--------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `GH_PAT` | Yes | GitHub Personal Access Token with `repo` scope |
| `ANTHROPIC_API_KEY` | No | Anthropic API key (fallback) |
| `SLACK_BOT_TOKEN` | No | Slack bot token for notifications and channel reading |
| `JIRA_API_TOKEN` | No | Jira API token (Cloud) or PAT (Data Center) |
| `JIRA_USER_EMAIL` | No | Jira user email (required for Cloud basic auth) |

Then trigger the workflow:
1. Go to Actions → "Ralph Loop - Bug Fix Engine"
2. Click "Run workflow"
3. Enter the issue URL and any optional parameters
4. View results in the workflow artifacts

### Preparing a Test Scenario

To test against a known-solved bug:

```bash
# Fork a repo and roll back to before the fix
./scripts/setup-fork.sh \
  konflux-ci/build-service \
  your-org \
  <commit-before-fix> \
  https://github.com/konflux-ci/build-service/issues/123
```

## GitHub Prerequisites Checklist

Before running the production ralph loop in GitHub Actions, complete these steps:

- [ ] **Create a GitHub repository** for this project (or push to an existing one)
- [ ] **Set up repository secrets**:
  - `GEMINI_API_KEY` — Get from [Google AI Studio](https://aistudio.google.com/)
  - `GH_PAT` — Create at GitHub → Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens. Needs `repo` scope on target repositories
  - `ANTHROPIC_API_KEY` (optional) — Get from [Anthropic Console](https://console.anthropic.com/)
- [ ] **Enable GitHub Actions** in the repository settings
- [ ] **Select a Konflux bug to test against** — Ralph Bean committed to picking one or two complex bugs with known fixes
- [ ] **Fork the target Konflux repo** and roll back the fix commit using `scripts/setup-fork.sh`
- [ ] **Configure branch protection** on the fork's default branch (recommended: require PR reviews, require status checks)

## How to Build This System (Meta Ralph Loop)

This project is designed to be built iteratively using a ralph loop on your laptop:

1. Open `prompt.md` in your coding agent (Cursor, Claude Code, OpenCode, etc.)
2. The prompt instructs the agent to read the specs and build the system phase by phase
3. Each phase produces testable, working output
4. Run `make check` after each phase to verify

The meta loop builds the production system. The production system runs in GitHub Actions.

### Meta Loop Observability

The meta loop has its own observability stack so you always know what's happening:

- **`progress/run-log.md`** — Append-only record of every loop run. Each entry captures: what phase was worked on, what shipped, test results, architectural decisions made, issues encountered, and the next focus item. This is the single source of truth for build history.
- **`progress/index.html`** — Auto-generated dashboard showing overall progress, per-phase breakdown, test/lint status, and run history. Generated by `make progress` or `python scripts/gen-progress.py`.
- **`IMPLEMENTATION-PLAN.md`** — Phase items marked with ✅ as completed. Shows at a glance where the build stands.

After any loop run, check the state:
```bash
# Regenerate the dashboard (the loop does this as a closing step)
make progress

# Open in browser
open progress/index.html   # macOS
xdg-open progress/index.html  # Linux
```

## Current Build Status

**Phase 0: Foundation** — Complete (all sub-phases 0.1–0.5 done)
**Phase 1: Core Loop Engine** — Complete (all sub-phases 1.1–1.6 done)
**Phase 2: GitHub Actions Integration** — Complete (all sub-phases 2.1–2.4 done)
**Phase 3: Visualization and Reporting** — Complete (all sub-phases 3.1–3.5 done)
**Phase 4: Integration Layer** — Complete (all sub-phases 4.1–4.4 done)
**Phase 5: Hardening and Testing** — Complete (all sub-phases 5.1–5.4 done)
**Phase 6: Self-Improvement Infrastructure** — Complete (all sub-phases 6.1–6.3 done)

| Component | Status | Module |
|-----------|--------|--------|
| Package setup | ✅ | `pyproject.toml`, `Makefile`, `ruff.toml` |
| LLM provider abstraction | ✅ | `engine/integrations/llm.py` |
| Structured logging & tracing | ✅ | `engine/observability/` |
| Configuration system | ✅ | `engine/config.py` — includes per-phase config (`PhasesConfig`) |
| Tool executor | ✅ | `engine/tools/executor.py` |
| Loop orchestrator | ✅ | `engine/loop.py` — phase registry, dispatch, transitions, escalation |
| Phase framework | ✅ | `engine/phases/base.py`, `engine/phases/prompt_loader.py` — prompt loading, tool sets, config wiring |
| Triage phase | ✅ | `engine/phases/triage.py` — classify, verify components, attempt reproduction |
| Implementation phase | ✅ | `engine/phases/implement.py` — analyze code, generate fix, inner iteration loop, test/lint |
| Review phase | ✅ | `engine/phases/review.py` — independent review: correctness, intent, security, scope |
| Validation phase | ✅ | `engine/phases/validate.py` — full test suite, CI checks, minimal diff, PR creation |
| GH Actions workflow | ✅ | `.github/workflows/ralph-loop.yml` — workflow_dispatch, config overrides, artifact upload |
| Self-monitoring | ✅ | `engine/workflow/monitor.py` — CI health checks, step failure detection, workflow context |
| Secret management | ✅ | `engine/secrets.py` — `SecretManager` + `SecretRedactor`, env var validation, redaction in logs/traces/tools |
| Fork & rollback script | ✅ | `scripts/setup-fork.sh` — fork repo, rollback to pre-fix commit, JSON summary output |
| CLI entry point | ✅ | `engine/__main__.py` — `--config-override` inline YAML, `--config` file, secret validation, full arg wiring |
| Report generator | ✅ | `engine/visualization/report_generator.py` — reads execution.json, produces self-contained HTML via Jinja2 |
| Decision tree visualization | ✅ | `engine/visualization/decision_tree.py` — transforms execution log into interactive D3.js tree |
| Action map visualization | ✅ | `engine/visualization/action_map.py` — layered phase map with D3.js, data flow edges, token-sized nodes |
| Comparison report | ✅ | `engine/visualization/comparison.py` — side-by-side diff, file overlap, similarity metrics, AI analysis |
| Report publishing | ✅ | `engine/visualization/publisher.py` — `ReportPublisher` + CLI, summary.md, artifact manifest, GitHub Pages deployment |
| Integration adapter protocol | ✅ | `engine/integrations/__init__.py` — `IntegrationAdapter` protocol with discover/read/write/search |
| GitHub integration (enhanced) | ✅ | `engine/integrations/github.py` — `GitHubAdapter`: issues, PRs, comments, labels, CI status, commit signing |
| Slack integration | ✅ | `engine/integrations/slack.py` — `SlackAdapter`: notifications, channel history, injection guards |
| Jira integration | ✅ | `engine/integrations/jira.py` — `JiraAdapter`: read issues, post comments, transitions, JQL search, injection guards |
| Discovery service | ✅ | `engine/integrations/discovery.py` — `DiscoveryService`: enumerate integrations, probe auth, LLM catalog |
| Integrations config | ✅ | `engine/config.py` — `IntegrationsConfig` with GitHub, Slack, Jira sub-configs + YAML loading |
| Prompt injection testing | ✅ | `tests/test_prompt_injection.py` — 127 tests: payload catalog, delimiter wrapping, escape containment, system prompt isolation, integration guards, phase tool restrictions, fail-closed, zero-trust, regression vectors |
| Loop behavior testing | ✅ | `tests/test_loop.py` — 55 tests: iteration cap enforcement (boundary, retries, backtrack), time budget enforcement (monkeypatched time, mid-loop expiry), escalation behavior (all paths, context recording, status values), phase validation independence (per-phase tool filtering, prior results, executor isolation) |
| End-to-end testing | ✅ | `tests/test_e2e.py` — 46 tests: 3 simulated Konflux bugs (Go nil pointer, Python import, YAML typo), full pipeline, comparison mode, metrics/observability, report generation, robustness, cross-scenario quality |
| Security audit | ✅ | `tests/test_security_audit.py` — 59 tests: commit signing verification, provenance recording (all phases), secrets never in logs/artifacts, untrusted content separation in all LLM calls, cross-cutting security properties |
| Golden principles enforcement | ✅ | `engine/golden_principles.py` — AST-based static analyzer: P1 logging, P3 untrusted separation, P5 iteration bounds, P8 provenance, P9 report publishing, P10 config usage. `make principles` CI gate |
| Deterministic tool extraction | ✅ | `engine/tools/extraction.py` — `PatternDetector` + `ProposalGenerator`: scans execution records for repeated LLM patterns, proposes deterministic replacements (5 categories + caching fallback). CLI: `python -m engine.tools.extraction` |
| Background quality scanner | ✅ | `engine/quality_scanner.py` — `BackgroundQualityScanner`: periodic scans combining golden principles, extraction proposals, code metrics. Auto-generates refactoring PR bodies. Weekly cron workflow. CLI: `python -m engine.quality_scanner` |

**1533 tests passing**, lint clean, golden principles PASS.

## Design Principles

From the [fullsend](../fullsend/) project's security threat model and architecture:

- **Security is the foundation** — every component designed with adversarial thinking
- **Zero trust between phases** — each phase validates independently
- **The repo is the coordinator** — branch protection and CODEOWNERS make merge decisions
- **Demos are a byproduct** — the system generates its own visual evidence
- **Everything is auditable** — every action logged, every decision traceable
- **Technology agnostic** — LLM provider, agent runtime, and target stack are all swappable

## Relationship to Fullsend

This project implements the MVP described in the [fullsend](../fullsend/) exploration:
- **Bug fix workflow** (Phase 1 scope from the team meeting)
- **GitHub Actions as infrastructure** (one of the infrastructure options explored)
- **Ralph Loop as the agent model** (single loop > multi-agent services, see ARCHITECTURE.md)
- **Security model** aligned with fullsend's threat model (5 threats, 7 principles)
- **Provenance and supply chain** compatible with Konflux/SLSA/Enterprise Contract

## Documentation

- [SPEC.md](SPEC.md) — Full technical specification
- [ARCHITECTURE.md](ARCHITECTURE.md) — Architecture decisions and rationale
- [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md) — Phased build plan
- [prompt.md](prompt.md) — Meta ralph loop instructions
- [progress/run-log.md](progress/run-log.md) — Meta loop run history
- `progress/index.html` — Progress dashboard (`make progress` to generate)
