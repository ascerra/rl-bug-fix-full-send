"""Target repository language detection and test/lint command selection.

Fixes D13 (§7.13): the generic ``pytest || go test || npm test`` chain runs
the wrong test runner because pytest is an engine dev dependency.  This module
detects the target repo's primary language from project manifest files and file
extension frequency, then returns the correct test and lint commands.

Detection priority:
    1. Explicit ``test_command`` / ``lint_command`` in ``.rl-config.yaml``
    2. Makefile with ``test`` target
    3. Project manifest files (go.mod, package.json, Cargo.toml, pyproject.toml, …)
    4. File extension frequency analysis
"""

from __future__ import annotations

from dataclasses import dataclass

MANIFEST_FILES: dict[str, str] = {
    "go.mod": "go",
    "go.sum": "go",
    "Cargo.toml": "rust",
    "package.json": "node",
    "pyproject.toml": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "requirements.txt": "python",
    "Pipfile": "python",
}

EXTENSION_LANGUAGE: dict[str, str] = {
    ".go": "go",
    ".py": "python",
    ".js": "node",
    ".ts": "node",
    ".tsx": "node",
    ".jsx": "node",
    ".rs": "rust",
}

TEST_COMMANDS: dict[str, str] = {
    "go": "go test ./... 2>&1",
    "python": "python -m pytest --tb=short -q 2>&1",
    "node": "npm test 2>&1",
    "rust": "cargo test 2>&1",
}

LINT_COMMANDS: dict[str, str] = {
    "go": "golangci-lint run ./... 2>&1",
    "python": "ruff check . 2>&1",
    "node": "npx eslint . 2>&1",
    "rust": "cargo clippy -- -D warnings 2>&1",
}

FALLBACK_TEST_COMMAND = "echo 'No test runner detected — unable to determine repo language'"
FALLBACK_LINT_COMMAND = "echo 'No linter detected — unable to determine repo language'"


@dataclass
class RepoStack:
    """Detected language/stack for a target repository."""

    language: str
    test_command: str
    lint_command: str
    detected_from: str
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "language": self.language,
            "test_command": self.test_command,
            "lint_command": self.lint_command,
            "detected_from": self.detected_from,
            "confidence": self.confidence,
        }


def detect_repo_stack(
    repo_file_listing: str,
    *,
    test_command_override: str = "",
    lint_command_override: str = "",
) -> RepoStack:
    """Detect the primary language of a target repository.

    Parameters
    ----------
    repo_file_listing:
        Newline-separated list of file paths (from ``find`` or ``ls``).
    test_command_override:
        If provided, used verbatim instead of the detected test command.
    lint_command_override:
        If provided, used verbatim instead of the detected lint command.
    """
    files = [f.strip() for f in repo_file_listing.strip().splitlines() if f.strip()]

    language, detected_from, confidence = _detect_language(files)

    test_cmd = test_command_override or TEST_COMMANDS.get(language, FALLBACK_TEST_COMMAND)
    lint_cmd = lint_command_override or LINT_COMMANDS.get(language, FALLBACK_LINT_COMMAND)

    if test_command_override:
        detected_from = f"config_override+{detected_from}"
        confidence = max(confidence, 1.0)

    return RepoStack(
        language=language,
        test_command=test_cmd,
        lint_command=lint_cmd,
        detected_from=detected_from,
        confidence=confidence,
    )


def _detect_language(files: list[str]) -> tuple[str, str, float]:
    """Return ``(language, detected_from, confidence)``."""

    has_makefile = False
    for f in files:
        basename = f.rsplit("/", 1)[-1] if "/" in f else f
        if basename == "Makefile" or basename == "makefile":
            has_makefile = True

    for f in files:
        basename = f.rsplit("/", 1)[-1] if "/" in f else f
        if basename in MANIFEST_FILES:
            lang = MANIFEST_FILES[basename]
            return lang, basename, 0.95

    ext_counts: dict[str, int] = {}
    for f in files:
        dot_pos = f.rfind(".")
        if dot_pos >= 0:
            ext = f[dot_pos:]
            lang = EXTENSION_LANGUAGE.get(ext)
            if lang:
                ext_counts[lang] = ext_counts.get(lang, 0) + 1

    if ext_counts:
        primary = max(ext_counts, key=lambda k: ext_counts[k])
        total_source = sum(ext_counts.values())
        ratio = ext_counts[primary] / total_source if total_source > 0 else 0
        confidence = min(0.5 + 0.4 * ratio, 0.85)
        return primary, "file_extensions", confidence

    if has_makefile:
        return "unknown", "makefile_only", 0.1

    return "unknown", "none", 0.0


def build_makefile_test_command() -> str:
    """Return a Makefile-based test command for repos with a ``test`` target."""
    return "make test 2>&1"
