"""Tests for engine.tools.extraction — deterministic tool extraction from LLM patterns."""

from __future__ import annotations

import json
from typing import Any

import pytest

from engine.tools.extraction import (
    CATEGORY_KEYWORDS,
    ExtractionProposal,
    LLMCallPattern,
    PatternDetector,
    ProposalGenerator,
    _cluster_by_similarity,
    _estimate_tokens,
    _extract_llm_actions,
    _get_prompt_summary,
    categorize_prompt,
    detect_and_propose,
    format_proposals_text,
    jaccard_similarity,
    main,
)

# ---------------------------------------------------------------------------
# Test fixtures — execution record builders
# ---------------------------------------------------------------------------


def _llm_action(
    description: str = "LLM query",
    phase: str = "triage",
    prompt_summary: str = "",
    tokens_in: int = 500,
    tokens_out: int = 200,
    response_summary: str = "response",
) -> dict[str, Any]:
    """Build a minimal LLM action record."""
    return {
        "action_type": "llm_query",
        "phase": phase,
        "input": {"description": description},
        "llm_context": {
            "prompt_summary": prompt_summary or description,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "response_summary": response_summary,
            "model": "mock",
            "provider": "mock",
        },
    }


def _tool_action(tool_name: str = "file_read", phase: str = "triage") -> dict[str, Any]:
    return {"action_type": f"tool:{tool_name}", "phase": phase}


def _execution(actions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"execution": {"actions": actions}}


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestLLMCallPattern:
    def test_to_dict_basic(self):
        p = LLMCallPattern(
            pattern_id="abc",
            category="file_check",
            description="desc",
            occurrences=3,
            phases=["triage"],
            sample_prompts=["a", "b", "c", "d"],
            estimated_tokens_saved=1000,
        )
        d = p.to_dict()
        assert d["pattern_id"] == "abc"
        assert d["category"] == "file_check"
        assert d["occurrences"] == 3
        assert len(d["sample_prompts"]) == 3

    def test_to_dict_truncates_samples(self):
        p = LLMCallPattern(
            pattern_id="x",
            category="test_run",
            description="d",
            occurrences=5,
            sample_prompts=["p1", "p2", "p3", "p4", "p5"],
        )
        assert len(p.to_dict()["sample_prompts"]) == 3

    def test_defaults(self):
        p = LLMCallPattern(pattern_id="x", category="general", description="d", occurrences=1)
        assert p.phases == []
        assert p.sample_prompts == []
        assert p.estimated_tokens_saved == 0


class TestExtractionProposal:
    def test_to_dict(self):
        pattern = LLMCallPattern(
            pattern_id="p1",
            category="file_check",
            description="desc",
            occurrences=2,
        )
        prop = ExtractionProposal(
            pattern=pattern,
            tool_name="check_file_exists",
            tool_description="Check file",
            implementation="def check(): pass",
            tool_schema={"name": "check_file_exists"},
            confidence=0.95,
            rationale="reason",
        )
        d = prop.to_dict()
        assert d["tool_name"] == "check_file_exists"
        assert d["confidence"] == 0.95
        assert d["pattern"]["pattern_id"] == "p1"
        assert d["implementation"] == "def check(): pass"

    def test_defaults(self):
        pattern = LLMCallPattern(
            pattern_id="p1", category="general", description="d", occurrences=1
        )
        prop = ExtractionProposal(
            pattern=pattern,
            tool_name="t",
            tool_description="d",
            implementation="pass",
        )
        assert prop.confidence == 0.0
        assert prop.rationale == ""
        assert prop.tool_schema == {}


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------


class TestJaccardSimilarity:
    def test_identical(self):
        assert jaccard_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self):
        assert jaccard_similarity("abc def", "xyz uvw") == 0.0

    def test_partial_overlap(self):
        sim = jaccard_similarity("check file exists in repo", "check if file exists")
        assert 0.3 < sim < 1.0

    def test_both_empty(self):
        assert jaccard_similarity("", "") == 1.0

    def test_one_empty(self):
        assert jaccard_similarity("hello", "") == 0.0
        assert jaccard_similarity("", "world") == 0.0

    def test_case_insensitive(self):
        assert jaccard_similarity("Hello World", "hello world") == 1.0

    def test_ignores_punctuation(self):
        sim = jaccard_similarity("file_check(path)", "file check path")
        assert sim > 0.5


class TestCategorizePrompt:
    def test_file_check(self):
        assert categorize_prompt("Check if the file exists at this path") == "file_check"

    def test_test_run(self):
        assert categorize_prompt("Run the test suite to check if tests pass") == "test_run"

    def test_lint_check(self):
        assert categorize_prompt("Run the lint check on the codebase") == "lint_check"

    def test_classification(self):
        assert categorize_prompt("Classify this as a bug or feature") == "classification"

    def test_diff_analysis(self):
        assert categorize_prompt("Analyze the diff for minimal scope") == "diff_analysis"

    def test_no_match(self):
        assert categorize_prompt("Random unrelated text about nothing") is None

    def test_empty_string(self):
        assert categorize_prompt("") is None

    def test_ruff_check(self):
        assert categorize_prompt("Run ruff check on the Python files") == "lint_check"

    def test_format_check(self):
        assert categorize_prompt("Format check the output code") == "lint_check"

    def test_is_bug(self):
        assert categorize_prompt("Is this a bug in the system?") == "classification"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestExtractLLMActions:
    def test_filters_to_llm_query(self):
        actions = [
            _llm_action("query 1"),
            _tool_action("file_read"),
            _llm_action("query 2"),
        ]
        result = _extract_llm_actions(_execution(actions))
        assert len(result) == 2
        assert all(a["action_type"] == "llm_query" for a in result)

    def test_handles_flat_format(self):
        data = {"actions": [_llm_action("q1")]}
        result = _extract_llm_actions(data)
        assert len(result) == 1

    def test_empty_actions(self):
        assert _extract_llm_actions(_execution([])) == []

    def test_no_llm_actions(self):
        assert _extract_llm_actions(_execution([_tool_action()])) == []


class TestGetPromptSummary:
    def test_from_llm_context(self):
        action = _llm_action(prompt_summary="triage classification prompt")
        assert _get_prompt_summary(action) == "triage classification prompt"

    def test_falls_back_to_description(self):
        action = {
            "action_type": "llm_query",
            "input": {"description": "Classify the issue"},
            "llm_context": {},
        }
        assert _get_prompt_summary(action) == "Classify the issue"

    def test_empty_when_no_context(self):
        assert _get_prompt_summary({}) == ""


class TestEstimateTokens:
    def test_sums_tokens(self):
        actions = [
            _llm_action(tokens_in=100, tokens_out=50),
            _llm_action(tokens_in=200, tokens_out=100),
        ]
        assert _estimate_tokens(actions) == 450

    def test_empty_actions(self):
        assert _estimate_tokens([]) == 0

    def test_missing_token_counts(self):
        actions = [{"llm_context": {}}]
        assert _estimate_tokens(actions) == 0


class TestClusterBySimilarity:
    def test_identical_prompts_cluster(self):
        actions = [
            _llm_action(prompt_summary="Check if file exists in the repository"),
            _llm_action(prompt_summary="Check if file exists in the repository"),
        ]
        clusters = _cluster_by_similarity(actions, threshold=0.6)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_different_prompts_separate(self):
        actions = [
            _llm_action(prompt_summary="Check if file exists in the repository path"),
            _llm_action(prompt_summary="Run the test suite and report pass or fail status"),
        ]
        clusters = _cluster_by_similarity(actions, threshold=0.6)
        assert len(clusters) == 2

    def test_similar_prompts_cluster(self):
        actions = [
            _llm_action(prompt_summary="Check if file exists in the repository"),
            _llm_action(prompt_summary="Check if the target file is present in the repo"),
        ]
        clusters = _cluster_by_similarity(actions, threshold=0.3)
        assert len(clusters) == 1

    def test_short_prompts_skipped(self):
        actions = [
            _llm_action(prompt_summary="ab"),
            _llm_action(prompt_summary="ab"),
        ]
        clusters = _cluster_by_similarity(actions, threshold=0.6)
        assert all(len(c) == 1 for c in clusters)

    def test_empty_list(self):
        assert _cluster_by_similarity([], 0.6) == []


# ---------------------------------------------------------------------------
# PatternDetector
# ---------------------------------------------------------------------------


class TestPatternDetector:
    def test_detects_repeated_file_checks(self):
        actions = [
            _llm_action(
                prompt_summary="Check if file exists in the repository",
                phase="triage",
                tokens_in=500,
                tokens_out=100,
            ),
            _llm_action(
                prompt_summary="Check if file exists in the repository",
                phase="triage",
                tokens_in=500,
                tokens_out=100,
            ),
            _llm_action(
                prompt_summary="Check if file exists in the repo path",
                phase="implement",
                tokens_in=500,
                tokens_out=100,
            ),
        ]
        detector = PatternDetector(min_occurrences=2, similarity_threshold=0.5)
        patterns = detector.detect(_execution(actions))
        assert len(patterns) >= 1
        p = patterns[0]
        assert p.occurrences >= 2
        assert p.category == "file_check"

    def test_detects_test_run_pattern(self):
        actions = [
            _llm_action(
                prompt_summary="Run the test suite to see if tests pass",
                phase="implement",
            ),
            _llm_action(
                prompt_summary="Run the test suite to check if tests pass",
                phase="validate",
            ),
        ]
        detector = PatternDetector(min_occurrences=2, similarity_threshold=0.5)
        patterns = detector.detect(_execution(actions))
        assert len(patterns) >= 1
        assert patterns[0].category == "test_run"

    def test_min_occurrences_filters(self):
        actions = [
            _llm_action(prompt_summary="Check if file exists in the repository"),
        ]
        detector = PatternDetector(min_occurrences=2)
        assert detector.detect(_execution(actions)) == []

    def test_no_llm_actions_returns_empty(self):
        detector = PatternDetector()
        assert detector.detect(_execution([_tool_action()])) == []

    def test_empty_execution(self):
        detector = PatternDetector()
        assert detector.detect(_execution([])) == []

    def test_sorted_by_tokens_saved(self):
        actions = [
            _llm_action(
                prompt_summary="Check if file exists in repository path",
                tokens_in=100,
                tokens_out=50,
            ),
            _llm_action(
                prompt_summary="Check if file exists in repository path",
                tokens_in=100,
                tokens_out=50,
            ),
            _llm_action(
                prompt_summary="Run the test suite and check pass fail",
                tokens_in=1000,
                tokens_out=500,
            ),
            _llm_action(
                prompt_summary="Run the test suite and check pass fail",
                tokens_in=1000,
                tokens_out=500,
            ),
        ]
        detector = PatternDetector(min_occurrences=2, similarity_threshold=0.5)
        patterns = detector.detect(_execution(actions))
        assert len(patterns) == 2
        assert patterns[0].estimated_tokens_saved >= patterns[1].estimated_tokens_saved

    def test_phases_collected(self):
        actions = [
            _llm_action(
                prompt_summary="Check if file exists in the repository",
                phase="triage",
            ),
            _llm_action(
                prompt_summary="Check if file exists in the repository",
                phase="implement",
            ),
        ]
        detector = PatternDetector(min_occurrences=2, similarity_threshold=0.5)
        patterns = detector.detect(_execution(actions))
        assert len(patterns) == 1
        assert sorted(patterns[0].phases) == ["implement", "triage"]

    def test_general_category_fallback(self):
        actions = [
            _llm_action(prompt_summary="Do something completely unique and custom here"),
            _llm_action(prompt_summary="Do something completely unique and custom here"),
        ]
        detector = PatternDetector(min_occurrences=2)
        patterns = detector.detect(_execution(actions))
        assert len(patterns) == 1
        assert patterns[0].category == "general"

    def test_sample_prompts_capped_at_three(self):
        actions = [
            _llm_action(prompt_summary="Check if file exists in the repository path")
            for _ in range(5)
        ]
        detector = PatternDetector(min_occurrences=2)
        patterns = detector.detect(_execution(actions))
        assert len(patterns) == 1
        assert len(patterns[0].sample_prompts) == 3


class TestPatternDetectorMulti:
    def test_detects_across_records(self):
        rec1 = _execution(
            [
                _llm_action(
                    prompt_summary="Check if file exists in the repository",
                    phase="triage",
                ),
            ]
        )
        rec2 = _execution(
            [
                _llm_action(
                    prompt_summary="Check if file exists in the repository",
                    phase="implement",
                ),
            ]
        )
        detector = PatternDetector(min_occurrences=2, similarity_threshold=0.5)
        patterns = detector.detect_multi([rec1, rec2])
        assert len(patterns) >= 1

    def test_empty_records(self):
        detector = PatternDetector()
        assert detector.detect_multi([]) == []

    def test_all_empty(self):
        detector = PatternDetector()
        assert detector.detect_multi([_execution([]), _execution([])]) == []


# ---------------------------------------------------------------------------
# ProposalGenerator
# ---------------------------------------------------------------------------


class TestProposalGenerator:
    def test_generates_file_check_proposal(self):
        pattern = LLMCallPattern(
            pattern_id="fc1",
            category="file_check",
            description="File existence check",
            occurrences=3,
            phases=["triage"],
        )
        generator = ProposalGenerator()
        proposals = generator.generate([pattern])
        assert len(proposals) == 1
        p = proposals[0]
        assert p.tool_name == "check_file_exists"
        assert p.confidence == 0.95
        assert "async def check_file_exists" in p.implementation

    def test_generates_test_run_proposal(self):
        pattern = LLMCallPattern(
            pattern_id="tr1",
            category="test_run",
            description="Test running",
            occurrences=2,
            phases=["implement"],
        )
        proposals = ProposalGenerator().generate([pattern])
        assert proposals[0].tool_name == "run_test_suite"
        assert proposals[0].confidence == 0.90

    def test_generates_lint_check_proposal(self):
        pattern = LLMCallPattern(
            pattern_id="lc1",
            category="lint_check",
            description="Lint check",
            occurrences=2,
        )
        proposals = ProposalGenerator().generate([pattern])
        assert proposals[0].tool_name == "run_linter"

    def test_generates_classification_proposal(self):
        pattern = LLMCallPattern(
            pattern_id="cl1",
            category="classification",
            description="Bug classification",
            occurrences=4,
        )
        proposals = ProposalGenerator().generate([pattern])
        assert proposals[0].tool_name == "classify_issue_heuristic"
        assert proposals[0].confidence == 0.60

    def test_generates_diff_analysis_proposal(self):
        pattern = LLMCallPattern(
            pattern_id="da1",
            category="diff_analysis",
            description="Diff size analysis",
            occurrences=2,
        )
        proposals = ProposalGenerator().generate([pattern])
        assert proposals[0].tool_name == "analyze_diff_size"

    def test_unknown_category_falls_back_to_general(self):
        pattern = LLMCallPattern(
            pattern_id="unk1",
            category="unknown_category",
            description="Unknown pattern",
            occurrences=2,
        )
        proposals = ProposalGenerator().generate([pattern])
        assert proposals[0].tool_name == "cached_query"
        assert proposals[0].confidence == 0.50

    def test_general_category(self):
        pattern = LLMCallPattern(
            pattern_id="g1",
            category="general",
            description="General pattern",
            occurrences=2,
        )
        proposals = ProposalGenerator().generate([pattern])
        assert proposals[0].tool_name == "cached_query"

    def test_empty_patterns(self):
        assert ProposalGenerator().generate([]) == []

    def test_multiple_patterns(self):
        patterns = [
            LLMCallPattern(
                pattern_id="a",
                category="file_check",
                description="d1",
                occurrences=2,
            ),
            LLMCallPattern(
                pattern_id="b",
                category="test_run",
                description="d2",
                occurrences=3,
            ),
        ]
        proposals = ProposalGenerator().generate(patterns)
        assert len(proposals) == 2
        names = {p.tool_name for p in proposals}
        assert "check_file_exists" in names
        assert "run_test_suite" in names

    def test_proposal_has_valid_schema(self):
        pattern = LLMCallPattern(
            pattern_id="s1",
            category="file_check",
            description="Schema test",
            occurrences=2,
        )
        proposals = ProposalGenerator().generate([pattern])
        schema = proposals[0].tool_schema
        assert "name" in schema
        assert "parameters" in schema
        assert schema["parameters"]["type"] == "object"


# ---------------------------------------------------------------------------
# detect_and_propose (integration)
# ---------------------------------------------------------------------------


class TestDetectAndPropose:
    def test_end_to_end(self):
        data = _execution(
            [
                _llm_action(
                    prompt_summary="Check if the file exists in the repository",
                    phase="triage",
                    tokens_in=500,
                    tokens_out=100,
                ),
                _llm_action(
                    prompt_summary="Check if the file exists in the repository",
                    phase="implement",
                    tokens_in=500,
                    tokens_out=100,
                ),
            ]
        )
        proposals = detect_and_propose(data, min_occurrences=2, similarity_threshold=0.5)
        assert len(proposals) >= 1
        p = proposals[0]
        assert p.tool_name == "check_file_exists"
        assert p.pattern.occurrences >= 2
        assert p.pattern.estimated_tokens_saved > 0

    def test_no_patterns(self):
        data = _execution(
            [
                _llm_action(prompt_summary="Unique prompt one about specific topic"),
            ]
        )
        proposals = detect_and_propose(data)
        assert proposals == []

    def test_custom_thresholds(self):
        data = _execution(
            [
                _llm_action(prompt_summary="Check if file exists in the repository"),
                _llm_action(prompt_summary="Check if file exists in the repository"),
                _llm_action(prompt_summary="Check if file exists in the repository"),
            ]
        )
        high_threshold = detect_and_propose(data, min_occurrences=5)
        assert high_threshold == []

    def test_mixed_patterns(self):
        data = _execution(
            [
                _llm_action(
                    prompt_summary="Check if file exists in repository path", phase="triage"
                ),
                _llm_action(
                    prompt_summary="Check if file exists in repository path", phase="implement"
                ),
                _llm_action(
                    prompt_summary="Run the test suite and check pass fail", phase="implement"
                ),
                _llm_action(
                    prompt_summary="Run the test suite and check pass fail", phase="validate"
                ),
            ]
        )
        proposals = detect_and_propose(data, min_occurrences=2, similarity_threshold=0.5)
        assert len(proposals) == 2
        categories = {p.pattern.category for p in proposals}
        assert "file_check" in categories
        assert "test_run" in categories


# ---------------------------------------------------------------------------
# format_proposals_text
# ---------------------------------------------------------------------------


class TestFormatProposalsText:
    def test_no_proposals(self):
        text = format_proposals_text([])
        assert "No extraction opportunities found" in text

    def test_single_proposal(self):
        pattern = LLMCallPattern(
            pattern_id="f1",
            category="file_check",
            description="File check",
            occurrences=3,
            phases=["triage", "implement"],
            estimated_tokens_saved=1200,
        )
        prop = ExtractionProposal(
            pattern=pattern,
            tool_name="check_file_exists",
            tool_description="Check file",
            implementation="def check(): pass",
            confidence=0.95,
            rationale="deterministic",
        )
        text = format_proposals_text([prop])
        assert "check_file_exists" in text
        assert "file_check" in text
        assert "3" in text
        assert "95%" in text
        assert "deterministic" in text
        assert "1,200" in text

    def test_multiple_proposals(self):
        patterns = [
            LLMCallPattern(
                pattern_id="a",
                category="file_check",
                description="d1",
                occurrences=2,
            ),
            LLMCallPattern(
                pattern_id="b",
                category="test_run",
                description="d2",
                occurrences=3,
            ),
        ]
        proposals = ProposalGenerator().generate(patterns)
        text = format_proposals_text(proposals)
        assert "2 extraction proposal(s)" in text
        assert "Proposal 1" in text
        assert "Proposal 2" in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestCLI:
    def test_no_args_shows_usage(self, capsys):
        result = main([])
        assert result == 0
        out = capsys.readouterr().out
        assert "Usage" in out

    def test_file_not_found(self, capsys):
        result = main(["/nonexistent/path.json"])
        assert result == 0
        err = capsys.readouterr().err
        assert "not found" in err

    def test_invalid_json(self, tmp_path, capsys):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json {{{")
        result = main([str(bad_file)])
        assert result == 0
        err = capsys.readouterr().err
        assert "could not read" in err

    def test_valid_execution_no_patterns(self, tmp_path, capsys):
        data = _execution(
            [
                _llm_action(prompt_summary="Unique one-off prompt about a specific thing"),
            ]
        )
        f = tmp_path / "exec.json"
        f.write_text(json.dumps(data))
        result = main([str(f)])
        assert result == 0
        out = capsys.readouterr().out
        assert "No extraction opportunities found" in out

    def test_valid_execution_with_patterns(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        data = _execution(
            [
                _llm_action(prompt_summary="Check if file exists in the repository path"),
                _llm_action(prompt_summary="Check if file exists in the repository path"),
            ]
        )
        f = tmp_path / "exec.json"
        f.write_text(json.dumps(data))
        result = main([str(f)])
        assert result == 0
        out = capsys.readouterr().out
        assert "extraction proposal" in out
        assert (tmp_path / "extraction-proposals.json").is_file()
        written = json.loads((tmp_path / "extraction-proposals.json").read_text())
        assert len(written) >= 1

    def test_multiple_files(self, tmp_path, capsys, monkeypatch):
        monkeypatch.chdir(tmp_path)
        data1 = _execution(
            [
                _llm_action(prompt_summary="Check if file exists in the repository path"),
                _llm_action(prompt_summary="Check if file exists in the repository path"),
            ]
        )
        data2 = _execution(
            [
                _llm_action(prompt_summary="Run the test suite and check pass fail results"),
                _llm_action(prompt_summary="Run the test suite and check pass fail results"),
            ]
        )
        f1 = tmp_path / "exec1.json"
        f1.write_text(json.dumps(data1))
        f2 = tmp_path / "exec2.json"
        f2.write_text(json.dumps(data2))
        result = main([str(f1), str(f2)])
        assert result == 0
        out = capsys.readouterr().out
        assert "extraction proposal" in out


# ---------------------------------------------------------------------------
# Category keyword coverage
# ---------------------------------------------------------------------------


class TestCategoryKeywords:
    def test_all_categories_have_keywords(self):
        expected = {"file_check", "test_run", "lint_check", "classification", "diff_analysis"}
        assert set(CATEGORY_KEYWORDS.keys()) == expected

    def test_each_category_has_multiple_keyword_groups(self):
        for category, groups in CATEGORY_KEYWORDS.items():
            assert len(groups) >= 2, f"{category} should have at least 2 keyword groups"

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("file exists check", "file_check"),
            ("file found in path", "file_check"),
            ("path check for file", "file_check"),
            ("test run results", "test_run"),
            ("test suite execution", "test_run"),
            ("lint check run", "lint_check"),
            ("ruff check output", "lint_check"),
            ("classify bug or feature", "classification"),
            ("diff minimal scope", "diff_analysis"),
            ("diff analysis report", "diff_analysis"),
            ("lines changed in diff", "diff_analysis"),
        ],
    )
    def test_categorization_parametrized(self, text, expected):
        assert categorize_prompt(text) == expected


# ---------------------------------------------------------------------------
# Template coverage
# ---------------------------------------------------------------------------


class TestTemplates:
    @pytest.mark.parametrize(
        "category",
        ["file_check", "test_run", "lint_check", "classification", "diff_analysis", "general"],
    )
    def test_all_categories_have_templates(self, category):
        from engine.tools.extraction import _TEMPLATES

        assert category in _TEMPLATES
        template = _TEMPLATES[category]
        assert "tool_name" in template
        assert "tool_description" in template
        assert "implementation" in template
        assert "tool_schema" in template
        assert "confidence" in template
        assert "rationale" in template

    @pytest.mark.parametrize(
        "category",
        ["file_check", "test_run", "lint_check", "classification", "diff_analysis", "general"],
    )
    def test_template_implementation_is_valid_python(self, category):
        from engine.tools.extraction import _TEMPLATES

        code = _TEMPLATES[category]["implementation"]
        compile(code, f"<{category}>", "exec")

    @pytest.mark.parametrize(
        "category",
        ["file_check", "test_run", "lint_check", "classification", "diff_analysis", "general"],
    )
    def test_template_schema_has_required_fields(self, category):
        from engine.tools.extraction import _TEMPLATES

        schema = _TEMPLATES[category]["tool_schema"]
        assert "name" in schema
        assert "description" in schema
        assert "parameters" in schema


# ---------------------------------------------------------------------------
# Edge cases and integration
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_wrapped_execution_format(self):
        data = {
            "execution": {
                "actions": [
                    _llm_action(prompt_summary="Check if file exists in repo"),
                    _llm_action(prompt_summary="Check if file exists in repo"),
                ]
            }
        }
        proposals = detect_and_propose(data)
        assert len(proposals) >= 1

    def test_flat_execution_format(self):
        data = {
            "actions": [
                _llm_action(prompt_summary="Check if file exists in repo"),
                _llm_action(prompt_summary="Check if file exists in repo"),
            ]
        }
        proposals = detect_and_propose(data)
        assert len(proposals) >= 1

    def test_high_similarity_threshold(self):
        data = _execution(
            [
                _llm_action(prompt_summary="Run the test suite now"),
                _llm_action(prompt_summary="Execute tests for the project"),
            ]
        )
        proposals = detect_and_propose(data, similarity_threshold=0.99)
        assert proposals == []

    def test_zero_tokens(self):
        data = _execution(
            [
                _llm_action(prompt_summary="Check file exists in repo", tokens_in=0, tokens_out=0),
                _llm_action(prompt_summary="Check file exists in repo", tokens_in=0, tokens_out=0),
            ]
        )
        proposals = detect_and_propose(data)
        assert len(proposals) >= 1
        assert proposals[0].pattern.estimated_tokens_saved == 0

    def test_many_actions_performance(self):
        actions = [
            _llm_action(prompt_summary=f"Run test suite iteration {i % 5}") for i in range(50)
        ]
        data = _execution(actions)
        proposals = detect_and_propose(data, min_occurrences=5)
        assert isinstance(proposals, list)

    def test_pattern_id_is_unique(self):
        data = _execution(
            [
                _llm_action(prompt_summary="Check if file exists in the repository"),
                _llm_action(prompt_summary="Check if file exists in the repository"),
                _llm_action(prompt_summary="Run test suite and check pass fail status"),
                _llm_action(prompt_summary="Run test suite and check pass fail status"),
            ]
        )
        proposals = detect_and_propose(data, min_occurrences=2, similarity_threshold=0.5)
        ids = [p.pattern.pattern_id for p in proposals]
        assert len(ids) == len(set(ids))
