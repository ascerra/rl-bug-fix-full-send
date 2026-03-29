"""Tests for the detail drill-down panels (Phase 9.4).

Covers:
  - NarrativeFormatter: all action types, prompt summarisation, reasoning extraction
  - enrich_scene_with_narratives: scene data enrichment pipeline
  - detail-panel.js: file structure, exported API, fallback rendering
  - Report template: detail-panel.js inclusion, wiring, action list builder
  - ReportGenerator: narrative_html in scene data output
  - No raw JSON/YAML: all output is human-readable HTML
"""

from __future__ import annotations

from pathlib import Path

from engine.visualization.narrative.formatter import (
    NarrativeFormatter,
    _esc,
    _extract_content,
    _extract_decision,
    _format_ms,
    _summarize_command_result,
    _truncate,
    enrich_scene_with_narratives,
)
from engine.visualization.report_generator import ReportGenerator, extract_report_data

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "visual-report"
DETAIL_PANEL_JS = TEMPLATES_DIR / "detail-panel.js"


def _llm_action(
    action_id: str = "act-llm-1",
    description: str = "Classify the issue",
    model: str = "gemini-2.5-pro",
    provider: str = "gemini",
    tokens_in: int = 3000,
    tokens_out: int = 500,
    reasoning: str = "Issue describes a null pointer crash",
    decision: str = "",
    system_prompt: str = "",
    duration_ms: float = 800.0,
    success: bool = True,
) -> dict:
    output: dict = {"success": success}
    if decision:
        output["verdict"] = decision
    return {
        "id": action_id,
        "iteration": 1,
        "phase": "triage",
        "action_type": "llm_query",
        "timestamp": "2026-03-29T10:00:30Z",
        "duration_ms": duration_ms,
        "input": {"description": description, "system_prompt": system_prompt},
        "output": output,
        "llm_context": {
            "model": model,
            "provider": provider,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        },
        "provenance": {"reasoning": reasoning, "decision": decision},
    }


def _file_action(
    action_id: str = "act-file-1",
    action_type: str = "file_read",
    path: str = "pkg/controller/reconciler.go",
    description: str = "Read the affected file",
    content: str = "func Reconcile() { ... }",
    reasoning: str = "",
) -> dict:
    return {
        "id": action_id,
        "iteration": 2,
        "phase": "implement",
        "action_type": action_type,
        "timestamp": "2026-03-29T10:01:00Z",
        "duration_ms": 50.0,
        "input": {"description": description, "path": path},
        "output": {"success": True, "data": {"content": content}},
        "provenance": {"reasoning": reasoning},
    }


def _command_action(
    action_id: str = "act-cmd-1",
    command: str = "go test ./...",
    description: str = "Run test suite",
    success: bool = True,
    stdout: str = "ok  pkg/controller 0.5s",
    stderr: str = "",
    reasoning: str = "",
) -> dict:
    return {
        "id": action_id,
        "iteration": 2,
        "phase": "implement",
        "action_type": "shell_run",
        "timestamp": "2026-03-29T10:02:00Z",
        "duration_ms": 5000.0,
        "input": {"description": description, "command": command},
        "output": {"success": success, "data": {"stdout": stdout, "stderr": stderr}},
        "provenance": {"reasoning": reasoning},
    }


def _api_action(action_id: str = "act-api-1") -> dict:
    return {
        "id": action_id,
        "iteration": 3,
        "phase": "validate",
        "action_type": "github_api",
        "timestamp": "2026-03-29T10:03:00Z",
        "duration_ms": 200.0,
        "input": {"description": "Create pull request"},
        "output": {"success": True},
        "provenance": {},
    }


def _escalation_action(action_id: str = "act-esc-1") -> dict:
    return {
        "id": action_id,
        "iteration": 1,
        "phase": "triage",
        "action_type": "escalation",
        "timestamp": "2026-03-29T10:00:45Z",
        "duration_ms": 10.0,
        "input": {"description": "Ambiguous issue", "reason": "Cannot determine if bug or feature"},
        "output": {"success": False, "escalate": True},
        "provenance": {},
    }


def _minimal_execution(actions: list[dict] | None = None) -> dict:
    acts = actions or [_llm_action(), _file_action(), _command_action()]
    return {
        "execution": {
            "id": "test-exec-001",
            "started_at": "2026-03-29T10:00:00Z",
            "completed_at": "2026-03-29T10:05:00Z",
            "trigger": {"type": "manual", "source_url": "https://github.com/org/repo/issues/1"},
            "target": {"repo": "org/repo", "ref": "abc123"},
            "config": {},
            "iterations": [
                {
                    "number": 1,
                    "phase": "triage",
                    "started_at": "2026-03-29T10:00:00Z",
                    "completed_at": "2026-03-29T10:01:00Z",
                    "duration_ms": 1500.0,
                    "result": {"success": True, "next_phase": "implement"},
                },
                {
                    "number": 2,
                    "phase": "implement",
                    "started_at": "2026-03-29T10:01:00Z",
                    "completed_at": "2026-03-29T10:03:00Z",
                    "duration_ms": 5000.0,
                    "result": {"success": True, "next_phase": "review"},
                },
            ],
            "actions": acts,
            "metrics": {"total_llm_calls": 1, "total_tokens_in": 3000, "total_tokens_out": 500},
            "result": {"status": "success", "total_iterations": 2},
        }
    }


# ===========================================================================
# NarrativeFormatter — LLM calls
# ===========================================================================


class TestFormatLLMCall:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_basic_llm_call(self):
        html = self.fmt.format_llm_call(_llm_action())
        assert "What the agent was told" in html
        assert "By the numbers" in html
        assert "gemini-2.5-pro" in html
        assert "3000" in html
        assert "500" in html

    def test_includes_reasoning(self):
        html = self.fmt.format_llm_call(_llm_action(reasoning="The crash is a nil pointer"))
        assert "Key reasoning" in html
        assert "nil pointer" in html

    def test_no_reasoning_when_empty(self):
        html = self.fmt.format_llm_call(_llm_action(reasoning=""))
        assert "Key reasoning" not in html

    def test_includes_decision_verdict(self):
        html = self.fmt.format_llm_call(_llm_action(decision="approve"))
        assert "What it decided" in html
        assert "approve" in html

    def test_prompt_summary_with_system_prompt(self):
        html = self.fmt.format_llm_call(
            _llm_action(system_prompt="You are a triage agent. Classify the issue.")
        )
        assert "classify the issue" in html.lower()

    def test_duration_formatted(self):
        html = self.fmt.format_llm_call(_llm_action(duration_ms=2500.0))
        assert "2.5s" in html

    def test_no_raw_json(self):
        html = self.fmt.format_llm_call(_llm_action())
        assert "{" not in html or "detail-kv-list" in html


# ===========================================================================
# NarrativeFormatter — file operations
# ===========================================================================


class TestFormatFileOperation:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_file_read(self):
        html = self.fmt.format_file_operation(_file_action(action_type="file_read"))
        assert "What was read" in html
        assert "reconciler.go" in html

    def test_file_write(self):
        html = self.fmt.format_file_operation(_file_action(action_type="file_write"))
        assert "What changed" in html

    def test_file_search(self):
        html = self.fmt.format_file_operation(_file_action(action_type="file_search"))
        assert "What was searched" in html

    def test_includes_file_path(self):
        html = self.fmt.format_file_operation(_file_action(path="src/main.go"))
        assert "detail-file-path" in html
        assert "src/main.go" in html

    def test_includes_content_excerpt(self):
        html = self.fmt.format_file_operation(_file_action(content="func main() {}"))
        assert "func main() {}" in html

    def test_content_truncated(self):
        long_content = "x" * 3000
        html = self.fmt.format_file_operation(_file_action(content=long_content))
        assert "truncated" in html

    def test_includes_reasoning(self):
        html = self.fmt.format_file_operation(_file_action(reasoning="Need to check the nil guard"))
        assert "Why" in html
        assert "nil guard" in html

    def test_no_path_no_crash(self):
        html = self.fmt.format_file_operation(_file_action(path=""))
        assert "detail-file-path" not in html


# ===========================================================================
# NarrativeFormatter — command runs
# ===========================================================================


class TestFormatCommandRun:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_successful_command(self):
        html = self.fmt.format_command_run(_command_action(success=True))
        assert "What was run" in html
        assert "go test" in html
        assert "PASS" in html

    def test_failed_command(self):
        html = self.fmt.format_command_run(
            _command_action(success=False, stderr="FAIL: TestReconcile")
        )
        assert "FAIL" in html
        assert "TestReconcile" in html

    def test_includes_reasoning(self):
        html = self.fmt.format_command_run(
            _command_action(reasoning="Will re-run with updated code")
        )
        assert "What the agent did about it" in html
        assert "re-run" in html

    def test_command_from_input(self):
        html = self.fmt.format_command_run(_command_action(command="pytest tests/"))
        assert "pytest tests/" in html


# ===========================================================================
# NarrativeFormatter — API calls, escalation, generic
# ===========================================================================


class TestFormatOtherTypes:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_api_call_success(self):
        html = self.fmt.format_api_call(_api_action())
        assert "What was requested" in html
        assert "Create pull request" in html
        assert "SUCCESS" in html

    def test_api_call_failure(self):
        action = _api_action()
        action["output"] = {"success": False, "error": "404 not found"}
        html = self.fmt.format_api_call(action)
        assert "FAIL" in html
        assert "404" in html

    def test_escalation(self):
        html = self.fmt.format_escalation(_escalation_action())
        assert "Escalation" in html
        assert "human review" in html
        assert "Cannot determine" in html

    def test_generic_action(self):
        action = {
            "action_type": "custom_tool",
            "input": {"description": "Custom op"},
            "output": {"success": True},
            "provenance": {},
        }
        html = self.fmt.format_generic(action)
        assert "Action" in html
        assert "Custom op" in html
        assert "SUCCESS" in html


# ===========================================================================
# NarrativeFormatter — dispatch
# ===========================================================================


class TestFormatActionDispatch:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_dispatches_llm_query(self):
        html = self.fmt.format_action(_llm_action())
        assert "By the numbers" in html

    def test_dispatches_file_read(self):
        html = self.fmt.format_action(_file_action(action_type="file_read"))
        assert "What was read" in html

    def test_dispatches_file_write(self):
        html = self.fmt.format_action(_file_action(action_type="file_write"))
        assert "What changed" in html

    def test_dispatches_shell_run(self):
        html = self.fmt.format_action(_command_action())
        assert "What was run" in html

    def test_dispatches_tool_execution(self):
        action = _command_action()
        action["action_type"] = "tool_execution"
        html = self.fmt.format_action(action)
        assert "What was run" in html

    def test_dispatches_github_api(self):
        html = self.fmt.format_action(_api_action())
        assert "What was requested" in html

    def test_dispatches_escalation(self):
        html = self.fmt.format_action(_escalation_action())
        assert "Escalation" in html

    def test_dispatches_unknown(self):
        action = {
            "action_type": "mystery",
            "input": {"description": "?"},
            "output": {},
            "provenance": {},
        }
        html = self.fmt.format_action(action)
        assert "Action" in html


# ===========================================================================
# NarrativeFormatter — phase transitions
# ===========================================================================


class TestFormatPhaseTransition:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_successful_transition(self):
        html = self.fmt.format_phase_transition(
            {"success": True, "next_phase": "implement", "reasoning": "Bug confirmed"}
        )
        assert "Why did the agent move on?" in html
        assert "completed successfully" in html
        assert "implement" in html

    def test_escalation_transition(self):
        html = self.fmt.format_phase_transition(
            {"success": False, "escalate": True, "escalation_reason": "Ambiguous"}
        )
        assert "escalated" in html
        assert "Ambiguous" in html

    def test_carried_forward(self):
        html = self.fmt.format_phase_transition(
            {"success": True, "next_phase": "review", "carried_forward": "Diff is 5 lines"}
        )
        assert "What was carried forward?" in html
        assert "5 lines" in html

    def test_no_carried_forward(self):
        html = self.fmt.format_phase_transition({"success": True, "next_phase": "review"})
        assert "What was carried forward?" not in html


# ===========================================================================
# NarrativeFormatter — summarize_prompt
# ===========================================================================


class TestSummarizePrompt:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_triage_prompt(self):
        s = self.fmt.summarize_prompt("You are a triage agent. Classify the issue.", "")
        assert "classify" in s.lower()

    def test_implement_prompt(self):
        s = self.fmt.summarize_prompt("Implement a fix for the bug.", "")
        assert "implement" in s.lower() or "fix" in s.lower()

    def test_review_prompt(self):
        s = self.fmt.summarize_prompt("You are a review agent. Review the diff.", "")
        assert "review" in s.lower()

    def test_validate_prompt(self):
        s = self.fmt.summarize_prompt("Validate and prepare the PR.", "")
        assert "validate" in s.lower() or "pull request" in s.lower()

    def test_ci_prompt(self):
        s = self.fmt.summarize_prompt("CI remediation — fix the failure.", "")
        assert "ci" in s.lower() or "failure" in s.lower()

    def test_with_context(self):
        s = self.fmt.summarize_prompt("triage", "Check issue #42")
        assert "Check issue #42" in s

    def test_no_prompt_returns_context(self):
        s = self.fmt.summarize_prompt("", "Do something")
        assert s == "Do something"

    def test_no_prompt_no_context(self):
        s = self.fmt.summarize_prompt("", "")
        assert "No prompt" in s

    def test_unknown_prompt(self):
        s = self.fmt.summarize_prompt("You are a helpful assistant.", "")
        assert "process" in s.lower()


# ===========================================================================
# NarrativeFormatter — extract_key_reasoning
# ===========================================================================


class TestExtractKeyReasoning:
    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_returns_reasoning(self):
        result = self.fmt.extract_key_reasoning("The bug is a nil pointer")
        assert result == "The bug is a nil pointer"

    def test_empty_returns_empty(self):
        assert self.fmt.extract_key_reasoning("") == ""

    def test_truncates_long_reasoning(self):
        long = "x" * 3000
        result = self.fmt.extract_key_reasoning(long)
        assert len(result) <= 2100
        assert result.endswith("...")

    def test_strips_whitespace(self):
        assert self.fmt.extract_key_reasoning("  hello  ") == "hello"


# ===========================================================================
# Helper functions
# ===========================================================================


class TestHelpers:
    def test_esc_html(self):
        assert _esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"

    def test_esc_empty(self):
        assert _esc("") == ""

    def test_esc_none(self):
        assert _esc(None) == ""

    def test_truncate_short(self):
        assert _truncate("hello", 10) == "hello"

    def test_truncate_exact(self):
        assert _truncate("hello", 5) == "hello"

    def test_truncate_long(self):
        assert _truncate("hello world", 8) == "hello..."

    def test_format_ms_milliseconds(self):
        assert _format_ms(500) == "500ms"

    def test_format_ms_seconds(self):
        assert _format_ms(2500) == "2.5s"

    def test_format_ms_minutes(self):
        assert _format_ms(90000) == "1.5m"

    def test_extract_decision_verdict(self):
        assert _extract_decision({"verdict": "approve"}, {}) == "Verdict: approve"

    def test_extract_decision_classification(self):
        assert _extract_decision({"classification": "bug"}, {}) == "Classification: bug"

    def test_extract_decision_from_provenance(self):
        assert _extract_decision({}, {"decision": "proceed"}) == "proceed"

    def test_extract_decision_summary(self):
        assert _extract_decision({"summary": "All good"}, {}) == "All good"

    def test_extract_decision_empty(self):
        assert _extract_decision({}, {}) == ""

    def test_extract_content_from_data_dict(self):
        assert _extract_content({"data": {"content": "hello"}}) == "hello"

    def test_extract_content_from_stdout(self):
        assert _extract_content({"data": {"stdout": "output"}}) == "output"

    def test_extract_content_from_string(self):
        assert _extract_content({"data": "raw text"}) == "raw text"

    def test_extract_content_empty(self):
        assert _extract_content({}) == ""

    def test_summarize_command_pass_with_summary(self):
        result = _summarize_command_result(True, "test1\ntest2\nAll 5 tests passed", "")
        assert "passed" in result.lower()

    def test_summarize_command_pass_short(self):
        result = _summarize_command_result(True, "ok", "")
        assert result == "ok"

    def test_summarize_command_fail(self):
        result = _summarize_command_result(False, "", "FAIL: TestFoo")
        assert "TestFoo" in result

    def test_summarize_command_no_output(self):
        result = _summarize_command_result(False, "", "")
        assert "No output" in result


# ===========================================================================
# enrich_scene_with_narratives
# ===========================================================================


class TestEnrichSceneWithNarratives:
    def test_adds_narrative_html_to_objects(self):
        actions = [_llm_action(action_id="a1"), _file_action(action_id="a2")]
        scene_dict = {
            "platforms": [
                {
                    "phase": "triage",
                    "objects": [{"id": "a1", "meta": {}}],
                },
                {
                    "phase": "implement",
                    "objects": [{"id": "a2", "meta": {}}],
                },
            ]
        }
        result = enrich_scene_with_narratives(scene_dict, actions)
        assert "narrative_html" in result["platforms"][0]["objects"][0]["meta"]
        assert "narrative_html" in result["platforms"][1]["objects"][0]["meta"]

    def test_narrative_contains_expected_content(self):
        actions = [_llm_action(action_id="a1", description="Classify bug")]
        scene_dict = {"platforms": [{"phase": "triage", "objects": [{"id": "a1", "meta": {}}]}]}
        enrich_scene_with_narratives(scene_dict, actions)
        narrative = scene_dict["platforms"][0]["objects"][0]["meta"]["narrative_html"]
        assert "What the agent was told" in narrative

    def test_skips_objects_without_matching_action(self):
        actions = [_llm_action(action_id="a1")]
        scene_dict = {
            "platforms": [{"phase": "triage", "objects": [{"id": "no-match", "meta": {}}]}]
        }
        enrich_scene_with_narratives(scene_dict, actions)
        assert "narrative_html" not in scene_dict["platforms"][0]["objects"][0]["meta"]

    def test_handles_empty_actions(self):
        scene_dict = {"platforms": [{"phase": "triage", "objects": [{"id": "a1", "meta": {}}]}]}
        enrich_scene_with_narratives(scene_dict, [])
        assert "narrative_html" not in scene_dict["platforms"][0]["objects"][0]["meta"]

    def test_handles_empty_platforms(self):
        result = enrich_scene_with_narratives({"platforms": []}, [_llm_action()])
        assert result["platforms"] == []

    def test_mutates_in_place(self):
        scene_dict = {"platforms": [{"phase": "t", "objects": [{"id": "a1", "meta": {}}]}]}
        returned = enrich_scene_with_narratives(scene_dict, [_llm_action(action_id="a1")])
        assert returned is scene_dict

    def test_creates_meta_if_missing(self):
        scene_dict = {"platforms": [{"phase": "t", "objects": [{"id": "a1"}]}]}
        enrich_scene_with_narratives(scene_dict, [_llm_action(action_id="a1")])
        assert "narrative_html" in scene_dict["platforms"][0]["objects"][0]["meta"]


# ===========================================================================
# detail-panel.js — file structure
# ===========================================================================


class TestDetailPanelJS:
    def test_file_exists(self):
        assert DETAIL_PANEL_JS.exists()

    def test_declares_ralph_detail_panel(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "RalphDetailPanel" in js

    def test_exports_detail_panel_constructor(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "DetailPanel:" in js

    def test_exports_build_action_list(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "buildActionList:" in js

    def test_has_init_method(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "prototype.init" in js

    def test_has_open_method(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "prototype.open" in js

    def test_has_close_method(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "prototype.close" in js

    def test_has_prev_next_methods(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "prototype.prev" in js
        assert "prototype.next" in js

    def test_has_dispose_method(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "prototype.dispose" in js

    def test_has_keyboard_handler(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "Escape" in js
        assert "ArrowLeft" in js
        assert "ArrowRight" in js

    def test_renders_narrative_html(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "narrative_html" in js

    def test_has_fallback_renderer(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "renderFallbackContent" in js

    def test_has_slide_animation(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "translateX" in js

    def test_has_overlay_for_click_outside(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "overlay" in js

    def test_no_raw_json_display(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "JSON.stringify" not in js

    def test_escape_html_helper(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "escapeHtml" in js

    def test_format_duration_helper(self):
        js = DETAIL_PANEL_JS.read_text()
        assert "formatDuration" in js


# ===========================================================================
# Report template — detail panel integration
# ===========================================================================


class TestReportTemplateIntegration:
    def test_template_includes_detail_panel_js(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "detail-panel.js" in html

    def test_template_creates_detail_panel(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "RalphDetailPanel.DetailPanel" in html

    def test_template_builds_action_list(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "buildActionList" in html

    def test_template_opens_panel_on_click(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "detailPanel.open" in html

    def test_scene_container_exists(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "scene-3d-container" in html

    def test_usage_instructions_in_template(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "arrow keys" in html.lower()


# ===========================================================================
# ReportGenerator — narrative_html in scene data
# ===========================================================================


class TestReportGeneratorNarrative:
    def test_scene_data_contains_narrative_html(self):
        report_data = extract_report_data(_minimal_execution())
        scene = report_data.scene_data
        objects = scene.get("platforms", [{}])[0].get("objects", [])
        assert len(objects) > 0
        has_narrative = any("narrative_html" in obj.get("meta", {}) for obj in objects)
        assert has_narrative, "At least one object should have narrative_html"

    def test_narrative_html_is_string(self):
        report_data = extract_report_data(_minimal_execution())
        for platform in report_data.scene_data.get("platforms", []):
            for obj in platform.get("objects", []):
                narrative = obj.get("meta", {}).get("narrative_html", "")
                if narrative:
                    assert isinstance(narrative, str)

    def test_narrative_html_contains_sections(self):
        report_data = extract_report_data(_minimal_execution())
        first_obj = report_data.scene_data["platforms"][0]["objects"][0]
        narrative = first_obj["meta"]["narrative_html"]
        assert "detail-section" in narrative

    def test_report_html_output_includes_narrative(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "detail-panel.js" in html or "RalphDetailPanel" in html

    def test_report_html_embeds_scene_data_with_narrative(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "narrative_html" in html


# ===========================================================================
# No raw JSON/YAML exposed
# ===========================================================================


class TestNoRawDataExposed:
    """Verify that narrative output is human-readable, not raw machine formats."""

    def setup_method(self):
        self.fmt = NarrativeFormatter()

    def test_llm_no_raw_json_keys(self):
        html = self.fmt.format_llm_call(_llm_action())
        assert '"tokens_in"' not in html
        assert '"action_type"' not in html

    def test_file_no_raw_json_keys(self):
        html = self.fmt.format_file_operation(_file_action())
        assert '"action_type"' not in html

    def test_command_no_raw_json_keys(self):
        html = self.fmt.format_command_run(_command_action())
        assert '"action_type"' not in html

    def test_all_output_contains_html_tags(self):
        for action in [_llm_action(), _file_action(), _command_action(), _api_action()]:
            html = self.fmt.format_action(action)
            assert "<div" in html
            assert "detail-section" in html

    def test_html_escapes_special_chars(self):
        html = self.fmt.format_llm_call(_llm_action(description='<script>alert("xss")</script>'))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ===========================================================================
# Integration: full pipeline (build scene → enrich → render)
# ===========================================================================


class TestFullPipeline:
    def test_build_enrich_extract(self):
        execution = _minimal_execution()
        report_data = extract_report_data(execution)
        scene = report_data.scene_data
        assert "platforms" in scene
        all_objects = []
        for p in scene.get("platforms", []):
            all_objects.extend(p.get("objects", []))
        narratives = [
            o["meta"]["narrative_html"]
            for o in all_objects
            if "narrative_html" in o.get("meta", {})
        ]
        assert len(narratives) >= 1

    def test_all_action_types_produce_narrative(self):
        actions = [
            _llm_action(action_id="a1"),
            _file_action(action_id="a2"),
            _command_action(action_id="a3"),
            _api_action(action_id="a4"),
            _escalation_action(action_id="a5"),
        ]
        execution = _minimal_execution(actions)
        execution["execution"]["iterations"] = [
            {"number": 1, "phase": "triage", "result": {"success": True}},
            {"number": 2, "phase": "implement", "result": {"success": True}},
            {"number": 3, "phase": "validate", "result": {"success": True}},
        ]
        report_data = extract_report_data(execution)
        all_objects = []
        for p in report_data.scene_data.get("platforms", []):
            all_objects.extend(p.get("objects", []))
        with_narrative = [o for o in all_objects if "narrative_html" in o.get("meta", {})]
        assert len(with_narrative) == len(actions)

    def test_report_generation_does_not_crash(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert len(html) > 1000
        assert "Ralph Loop" in html
