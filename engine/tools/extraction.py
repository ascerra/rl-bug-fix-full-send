"""Deterministic tool extraction — detect repeated LLM patterns and propose replacements.

Scans execution records for LLM calls that follow repeated patterns (file existence
checks, test running, lint checks, simple classifications, diff analysis) and proposes
extracting them into deterministic Python functions or shell commands.

Run via: python -m engine.tools.extraction [execution.json ...]
Exit code 0 = proposals generated (or none found); exit code 1 = error.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Pattern categories and their keyword signatures
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS: dict[str, list[list[str]]] = {
    "file_check": [
        ["file", "exist"],
        ["file", "found"],
        ["path", "check"],
        ["file", "read"],
        ["file", "present"],
    ],
    "test_run": [
        ["test", "run"],
        ["test", "suite"],
        ["test", "pass"],
        ["test", "fail"],
        ["test", "execute"],
    ],
    "lint_check": [
        ["lint", "run"],
        ["lint", "check"],
        ["ruff", "check"],
        ["style", "check"],
        ["format", "check"],
    ],
    "classification": [
        ["classify", "bug"],
        ["bug", "feature"],
        ["classification", "type"],
        ["is", "bug"],
        ["categorize"],
    ],
    "diff_analysis": [
        ["diff", "minimal"],
        ["diff", "scope"],
        ["change", "size"],
        ["diff", "analysis"],
        ["lines", "changed"],
    ],
}

MIN_PROMPT_LENGTH = 10


@dataclass
class LLMCallPattern:
    """A detected pattern of repeated LLM usage that could be a deterministic tool."""

    pattern_id: str
    category: str
    description: str
    occurrences: int
    phases: list[str] = field(default_factory=list)
    sample_prompts: list[str] = field(default_factory=list)
    estimated_tokens_saved: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "category": self.category,
            "description": self.description,
            "occurrences": self.occurrences,
            "phases": self.phases,
            "sample_prompts": self.sample_prompts[:3],
            "estimated_tokens_saved": self.estimated_tokens_saved,
        }


@dataclass
class ExtractionProposal:
    """A proposed deterministic tool to replace a repeated LLM pattern."""

    pattern: LLMCallPattern
    tool_name: str
    tool_description: str
    implementation: str
    tool_schema: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern.to_dict(),
            "tool_name": self.tool_name,
            "tool_description": self.tool_description,
            "implementation": self.implementation,
            "tool_schema": self.tool_schema,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


# ---------------------------------------------------------------------------
# Text similarity helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Extract lowercase alphanumeric tokens from text."""
    return set(_WORD_RE.findall(text.lower()))


def jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two strings based on word tokens."""
    tokens_a = _tokenize(a)
    tokens_b = _tokenize(b)
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def categorize_prompt(text: str) -> str | None:
    """Determine which extraction category a prompt summary matches, if any."""
    lower = text.lower()
    tokens = _tokenize(lower)
    for category, keyword_groups in CATEGORY_KEYWORDS.items():
        for keywords in keyword_groups:
            if all(kw in tokens or kw in lower for kw in keywords):
                return category
    return None


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


def _extract_llm_actions(execution_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull LLM query actions from an execution record."""
    exec_inner = execution_data.get("execution", execution_data)
    actions = exec_inner.get("actions", [])
    return [a for a in actions if a.get("action_type") == "llm_query"]


def _cluster_by_similarity(
    actions: list[dict[str, Any]],
    threshold: float,
) -> list[list[dict[str, Any]]]:
    """Group LLM actions into clusters where prompt summaries are similar."""
    clusters: list[list[dict[str, Any]]] = []
    assigned: set[int] = set()

    for i, action_a in enumerate(actions):
        if i in assigned:
            continue
        cluster = [action_a]
        assigned.add(i)
        summary_a = _get_prompt_summary(action_a)
        if len(summary_a) < MIN_PROMPT_LENGTH:
            continue

        for j, action_b in enumerate(actions):
            if j in assigned:
                continue
            summary_b = _get_prompt_summary(action_b)
            if len(summary_b) < MIN_PROMPT_LENGTH:
                continue
            if jaccard_similarity(summary_a, summary_b) >= threshold:
                cluster.append(action_b)
                assigned.add(j)

        clusters.append(cluster)

    return clusters


def _get_prompt_summary(action: dict[str, Any]) -> str:
    """Extract the prompt summary from an LLM action record."""
    llm_ctx = action.get("llm_context", {})
    summary = llm_ctx.get("prompt_summary", "")
    if not summary:
        summary = action.get("input", {}).get("description", "")
    return summary


def _estimate_tokens(actions: list[dict[str, Any]]) -> int:
    """Estimate total tokens that could be saved by replacing these LLM calls."""
    total = 0
    for action in actions:
        llm_ctx = action.get("llm_context", {})
        total += llm_ctx.get("tokens_in", 0) + llm_ctx.get("tokens_out", 0)
    return total


class PatternDetector:
    """Analyzes execution records for repeated LLM call patterns."""

    def __init__(
        self,
        min_occurrences: int = 2,
        similarity_threshold: float = 0.6,
    ):
        self.min_occurrences = min_occurrences
        self.similarity_threshold = similarity_threshold

    def detect(self, execution_data: dict[str, Any]) -> list[LLMCallPattern]:
        """Detect repeated LLM patterns in an execution record."""
        llm_actions = _extract_llm_actions(execution_data)
        if not llm_actions:
            return []

        clusters = _cluster_by_similarity(llm_actions, self.similarity_threshold)

        patterns: list[LLMCallPattern] = []
        for cluster in clusters:
            if len(cluster) < self.min_occurrences:
                continue

            summaries = [_get_prompt_summary(a) for a in cluster]
            representative = summaries[0] if summaries else ""
            category = categorize_prompt(representative) or "general"
            phases = sorted({a.get("phase", "unknown") for a in cluster})

            patterns.append(
                LLMCallPattern(
                    pattern_id=str(uuid.uuid4())[:8],
                    category=category,
                    description=f"Repeated {category} pattern: {representative[:120]}",
                    occurrences=len(cluster),
                    phases=phases,
                    sample_prompts=summaries[:3],
                    estimated_tokens_saved=_estimate_tokens(cluster),
                )
            )

        patterns.sort(key=lambda p: p.estimated_tokens_saved, reverse=True)
        return patterns

    def detect_multi(
        self,
        execution_records: list[dict[str, Any]],
    ) -> list[LLMCallPattern]:
        """Detect patterns across multiple execution records."""
        all_actions: list[dict[str, Any]] = []
        for record in execution_records:
            all_actions.extend(_extract_llm_actions(record))

        if not all_actions:
            return []

        merged = {"execution": {"actions": all_actions}}
        return self.detect(merged)


# ---------------------------------------------------------------------------
# Proposal generation — code templates for each category
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, dict[str, Any]] = {
    "file_check": {
        "tool_name": "check_file_exists",
        "tool_description": "Check whether a file exists in the repository.",
        "implementation": '''\
async def check_file_exists(repo_path: str, file_path: str) -> dict[str, Any]:
    """Check whether a file exists in the repository (no LLM needed)."""
    from pathlib import Path

    full_path = (Path(repo_path) / file_path).resolve()
    repo_resolved = Path(repo_path).resolve()
    if not str(full_path).startswith(str(repo_resolved)):
        return {"success": False, "error": "Path traversal denied"}
    exists = full_path.is_file()
    return {"success": True, "exists": exists, "path": file_path}
''',
        "tool_schema": {
            "name": "check_file_exists",
            "description": "Check whether a file exists in the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path relative to repo root",
                    },
                },
                "required": ["file_path"],
            },
        },
        "confidence": 0.95,
        "rationale": (
            "File existence checks are purely deterministic — os.path.exists "
            "gives the same answer as an LLM reading the filesystem, at zero token cost."
        ),
    },
    "test_run": {
        "tool_name": "run_test_suite",
        "tool_description": "Run the repository's test suite and return pass/fail results.",
        "implementation": '''\
async def run_test_suite(
    repo_path: str,
    command: str = "make test",
    timeout: int = 120,
) -> dict[str, Any]:
    """Run test suite deterministically (no LLM needed)."""
    import asyncio
    import subprocess

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return {"success": False, "error": f"Timeout after {timeout}s"}
    return {
        "success": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace")[:10000],
        "stderr": stderr.decode(errors="replace")[:10000],
    }
''',
        "tool_schema": {
            "name": "run_test_suite",
            "description": "Run the repository test suite and return results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Test command (default: make test)",
                        "default": "make test",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 120,
                    },
                },
            },
        },
        "confidence": 0.90,
        "rationale": (
            "Test execution is a deterministic subprocess call. The LLM adds no value "
            "when the only question is 'do the tests pass?'."
        ),
    },
    "lint_check": {
        "tool_name": "run_linter",
        "tool_description": "Run the repository's linter and return results.",
        "implementation": '''\
async def run_linter(
    repo_path: str,
    command: str = "make lint",
    timeout: int = 60,
) -> dict[str, Any]:
    """Run linter deterministically (no LLM needed)."""
    import asyncio
    import subprocess

    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=repo_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        return {"success": False, "error": f"Timeout after {timeout}s"}
    return {
        "success": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace")[:10000],
        "stderr": stderr.decode(errors="replace")[:10000],
    }
''',
        "tool_schema": {
            "name": "run_linter",
            "description": "Run the repository linter and return results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Lint command (default: make lint)",
                        "default": "make lint",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 60,
                    },
                },
            },
        },
        "confidence": 0.90,
        "rationale": (
            "Lint checking is a deterministic subprocess call. The LLM should analyze "
            "lint output, not invoke the linter."
        ),
    },
    "classification": {
        "tool_name": "classify_issue_heuristic",
        "tool_description": "Heuristic classification of an issue as bug, feature, or ambiguous.",
        "implementation": '''\
def classify_issue_heuristic(title: str, body: str) -> dict[str, Any]:
    """Heuristic issue classification (no LLM needed for obvious cases)."""
    lower_title = title.lower()
    lower_body = body.lower()
    combined = lower_title + " " + lower_body

    bug_signals = ["bug", "error", "crash", "fix", "broken", "fail", "exception",
                   "traceback", "nil pointer", "panic", "segfault", "regression"]
    feature_signals = ["feature", "enhancement", "request", "proposal", "add support",
                       "new capability", "would be nice", "should support"]

    bug_score = sum(1 for s in bug_signals if s in combined)
    feature_score = sum(1 for s in feature_signals if s in combined)

    if bug_score > feature_score and bug_score >= 2:
        return {"classification": "bug", "confidence": min(0.9, 0.5 + bug_score * 0.1)}
    if feature_score > bug_score and feature_score >= 2:
        return {"classification": "feature", "confidence": min(0.9, 0.5 + feature_score * 0.1)}
    return {"classification": "ambiguous", "confidence": 0.3}
''',
        "tool_schema": {
            "name": "classify_issue_heuristic",
            "description": "Heuristic classification of a GitHub issue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Issue title"},
                    "body": {"type": "string", "description": "Issue body text"},
                },
                "required": ["title", "body"],
            },
        },
        "confidence": 0.60,
        "rationale": (
            "Many issues have strong keyword signals that make classification trivial. "
            "The LLM is only needed for genuinely ambiguous cases. Use this as a pre-filter "
            "and only call the LLM when the heuristic returns 'ambiguous'."
        ),
    },
    "diff_analysis": {
        "tool_name": "analyze_diff_size",
        "tool_description": "Analyze diff size and scope without LLM.",
        "implementation": '''\
def analyze_diff_size(diff_text: str) -> dict[str, Any]:
    """Analyze diff size and scope deterministically (no LLM needed)."""
    lines = diff_text.splitlines()
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    files = [l.split(" b/")[-1] for l in lines if l.startswith("diff --git")]
    total_changes = added + removed
    is_minimal = total_changes <= 50 and len(files) <= 5
    return {
        "files_changed": len(files),
        "lines_added": added,
        "lines_removed": removed,
        "total_changes": total_changes,
        "is_minimal": is_minimal,
        "file_list": files,
    }
''',
        "tool_schema": {
            "name": "analyze_diff_size",
            "description": "Analyze a git diff for size and scope metrics.",
            "parameters": {
                "type": "object",
                "properties": {
                    "diff_text": {
                        "type": "string",
                        "description": "Unified diff text to analyze",
                    },
                },
                "required": ["diff_text"],
            },
        },
        "confidence": 0.85,
        "rationale": (
            "Diff size metrics (lines added, removed, files changed) are simple arithmetic. "
            "The LLM should focus on semantic analysis, not counting lines."
        ),
    },
    "general": {
        "tool_name": "cached_query",
        "tool_description": "A repeated LLM query that may benefit from caching.",
        "implementation": '''\
import hashlib
from typing import Any

_CACHE: dict[str, Any] = {}

def cached_query(prompt_key: str, result: Any = None) -> dict[str, Any]:
    """Cache repeated LLM query results by prompt key."""
    cache_key = hashlib.sha256(prompt_key.encode()).hexdigest()[:16]
    if result is not None:
        _CACHE[cache_key] = result
        return {"cached": True, "key": cache_key}
    if cache_key in _CACHE:
        return {"hit": True, "result": _CACHE[cache_key], "key": cache_key}
    return {"hit": False, "key": cache_key}
''',
        "tool_schema": {
            "name": "cached_query",
            "description": "Cache for repeated LLM query results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt_key": {
                        "type": "string",
                        "description": "Unique key for the query being cached",
                    },
                    "result": {
                        "description": "Result to cache (omit to look up)",
                    },
                },
                "required": ["prompt_key"],
            },
        },
        "confidence": 0.50,
        "rationale": (
            "This pattern was repeated but doesn't match a known deterministic category. "
            "Caching identical queries avoids redundant LLM calls."
        ),
    },
}


class ProposalGenerator:
    """Generates deterministic tool proposals from detected LLM patterns."""

    def generate(self, patterns: list[LLMCallPattern]) -> list[ExtractionProposal]:
        """Generate extraction proposals for the given patterns."""
        proposals: list[ExtractionProposal] = []
        for pattern in patterns:
            template = _TEMPLATES.get(pattern.category)
            if template is None:
                template = _TEMPLATES["general"]

            proposals.append(
                ExtractionProposal(
                    pattern=pattern,
                    tool_name=template["tool_name"],
                    tool_description=template["tool_description"],
                    implementation=template["implementation"],
                    tool_schema=template["tool_schema"],
                    confidence=template["confidence"],
                    rationale=template["rationale"],
                )
            )

        return proposals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_and_propose(
    execution_data: dict[str, Any],
    min_occurrences: int = 2,
    similarity_threshold: float = 0.6,
) -> list[ExtractionProposal]:
    """Detect repeated LLM patterns and generate extraction proposals.

    This is the main entry point for the extraction system. It analyzes an
    execution record, finds LLM call patterns that repeat, and proposes
    deterministic tool replacements for each pattern.
    """
    detector = PatternDetector(
        min_occurrences=min_occurrences,
        similarity_threshold=similarity_threshold,
    )
    patterns = detector.detect(execution_data)

    generator = ProposalGenerator()
    return generator.generate(patterns)


def format_proposals_text(proposals: list[ExtractionProposal]) -> str:
    """Format proposals as human-readable text for CLI output."""
    if not proposals:
        return "No extraction opportunities found."

    lines = [f"Found {len(proposals)} extraction proposal(s):\n"]
    for i, p in enumerate(proposals, 1):
        lines.append(f"{'=' * 60}")
        lines.append(f"Proposal {i}: {p.tool_name}")
        lines.append(f"  Category:     {p.pattern.category}")
        lines.append(f"  Occurrences:  {p.pattern.occurrences}")
        lines.append(f"  Phases:       {', '.join(p.pattern.phases)}")
        lines.append(f"  Tokens saved: ~{p.pattern.estimated_tokens_saved:,}")
        lines.append(f"  Confidence:   {p.confidence:.0%}")
        lines.append(f"  Rationale:    {p.rationale}")
        lines.append(f"  Description:  {p.pattern.description}")
        lines.append("")
        lines.append("  Proposed implementation:")
        for code_line in p.implementation.splitlines():
            lines.append(f"    {code_line}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Analyze execution records and propose deterministic tool extractions."""
    args = argv if argv is not None else sys.argv[1:]

    if not args:
        print("Usage: python -m engine.tools.extraction <execution.json> [...]")
        print("  Analyzes execution records for repeated LLM patterns")
        print("  and proposes deterministic tool replacements.")
        return 0

    all_proposals: list[ExtractionProposal] = []
    for path_str in args:
        path = Path(path_str)
        if not path.is_file():
            print(f"Warning: file not found: {path_str}", file=sys.stderr)
            continue

        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: could not read {path_str}: {exc}", file=sys.stderr)
            continue

        proposals = detect_and_propose(data)
        all_proposals.extend(proposals)

    print(format_proposals_text(all_proposals))

    if all_proposals:
        output_path = Path("extraction-proposals.json")
        output = [p.to_dict() for p in all_proposals]
        output_path.write_text(json.dumps(output, indent=2))
        print(f"\nProposals written to {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
