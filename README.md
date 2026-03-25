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
│   └── ralph-loop.yml         # Main engine workflow
├── engine/                    # Python engine package
│   ├── __main__.py           # CLI entry point
│   ├── config.py             # Configuration system
│   ├── loop.py               # Ralph Loop core engine
│   ├── phases/               # Phase implementations
│   │   └── base.py           # Base phase class
│   ├── integrations/         # External system adapters
│   │   └── llm.py            # LLM provider abstraction
│   ├── observability/        # Logging, tracing, metrics
│   │   ├── logger.py         # Structured JSON logger
│   │   ├── tracer.py         # Action tracing
│   │   └── metrics.py        # Metrics collection
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
