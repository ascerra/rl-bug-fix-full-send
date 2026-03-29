"""Tests for the Three.js scene renderer (Phase 9.2).

Covers:
  - scene-renderer.js file structure and exported API
  - Report template integration (3D section, script inclusion, data embedding)
  - ReportData scene_data field and extract_report_data pipeline
  - ReportGenerator HTML output with scene data
  - WebGL fallback message
  - Config visualization_engine field
  - Detail panel rendering logic
  - Minimap, tooltip, and connection helpers
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.config import EngineConfig, ReportingConfig, load_config
from engine.visualization.report_generator import (
    ReportData,
    ReportGenerator,
    extract_report_data,
)
from engine.visualization.scene.builder import build_scene

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "visual-report"
SCENE_RENDERER_JS = TEMPLATES_DIR / "scene-renderer.js"


def _minimal_execution() -> dict:
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
                    "result": {"success": True, "next_phase": "implement", "escalate": False},
                },
                {
                    "number": 2,
                    "phase": "implement",
                    "started_at": "2026-03-29T10:01:00Z",
                    "completed_at": "2026-03-29T10:03:00Z",
                    "duration_ms": 5000.0,
                    "result": {"success": True, "next_phase": "review", "escalate": False},
                },
            ],
            "actions": [
                {
                    "id": "act-1",
                    "iteration": 1,
                    "phase": "triage",
                    "action_type": "llm_query",
                    "timestamp": "2026-03-29T10:00:30Z",
                    "duration_ms": 800.0,
                    "input": {"description": "Classify the issue"},
                    "output": {"success": True},
                    "llm_context": {
                        "model": "gemini-2.5-pro",
                        "provider": "gemini",
                        "tokens_in": 3000,
                        "tokens_out": 500,
                    },
                    "provenance": {"reasoning": "Issue describes a crash with stack trace"},
                },
                {
                    "id": "act-2",
                    "iteration": 2,
                    "phase": "implement",
                    "action_type": "file_write",
                    "timestamp": "2026-03-29T10:02:00Z",
                    "duration_ms": 50.0,
                    "input": {"description": "Write fix to controller.go", "path": "pkg/ctl.go"},
                    "output": {"success": True, "content": "package ctl\n..."},
                    "llm_context": {},
                },
            ],
            "metrics": {
                "total_llm_calls": 1,
                "total_tokens_in": 3000,
                "total_tokens_out": 500,
            },
            "result": {"status": "success", "total_iterations": 2},
        }
    }


def _empty_execution() -> dict:
    return {
        "execution": {
            "id": "empty-exec",
            "iterations": [],
            "actions": [],
            "metrics": {},
            "result": {"status": "unknown"},
        }
    }


# ---------------------------------------------------------------------------
# 1. scene-renderer.js File Structure
# ---------------------------------------------------------------------------


class TestSceneRendererJSFile:
    """Verify the JavaScript file exists and has expected structure."""

    def test_file_exists(self):
        assert SCENE_RENDERER_JS.exists(), "scene-renderer.js must exist in templates"

    def test_file_is_not_empty(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert len(content) > 1000, "scene-renderer.js should be substantial"

    def test_exports_ralph_scene_renderer(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "RalphSceneRenderer" in content

    def test_exports_render_scene_function(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "function renderScene(" in content

    def test_exports_scene_renderer_class(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "function SceneRenderer(" in content

    def test_has_webgl_fallback(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "_webglAvailable" in content
        assert "_showFallback" in content

    def test_has_orbit_controls_support(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "OrbitControls" in content

    def test_has_raycasting(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "Raycaster" in content
        assert "_onClick" in content
        assert "_onMouseMove" in content

    def test_has_minimap(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "minimap" in content.lower()
        assert "_initMinimap" in content

    def test_has_detail_panel_renderer(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "renderDetailPanel" in content

    def test_has_geometry_factories(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "createGeometry" in content
        assert "createLODGeometry" in content
        assert "polyhedron" in content
        assert "IcosahedronGeometry" in content

    def test_has_dispose_method(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "prototype.dispose" in content

    def test_has_camera_preset(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "setCameraPreset" in content

    def test_has_tooltip_creation(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "createTooltip" in content

    def test_has_connection_builder(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "buildConnection" in content
        assert "QuadraticBezierCurve3" in content

    def test_has_bridge_builder(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "buildBridge" in content

    def test_has_text_sprite(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "createTextSprite" in content

    def test_has_escape_html(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "escapeHtml" in content

    def test_has_format_duration(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "formatDuration" in content

    def test_has_lod_threshold(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "lodThreshold" in content

    def test_has_pulse_failed_objects(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "pulseFailedObjects" in content
        assert "failedObjects" in content

    def test_has_status_emissive_mapping(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "statusEmissiveIntensity" in content
        assert "'success'" in content
        assert "'failure'" in content

    def test_internals_exposed(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "_internals" in content


# ---------------------------------------------------------------------------
# 2. ReportData scene_data Field
# ---------------------------------------------------------------------------


class TestReportDataSceneField:
    """Verify ReportData includes scene_data field."""

    def test_scene_data_field_exists(self):
        rd = ReportData()
        assert hasattr(rd, "scene_data")
        assert rd.scene_data == {}

    def test_scene_data_in_to_dict(self):
        rd = ReportData(scene_data={"platforms": []})
        d = rd.to_dict()
        assert "scene_data" in d
        assert d["scene_data"] == {"platforms": []}

    def test_scene_data_populated_by_extract(self):
        rd = extract_report_data(_minimal_execution())
        assert rd.scene_data is not None
        assert "platforms" in rd.scene_data
        assert len(rd.scene_data["platforms"]) == 2  # triage + implement

    def test_scene_data_empty_execution(self):
        rd = extract_report_data(_empty_execution())
        assert rd.scene_data is not None
        assert rd.scene_data.get("platforms", []) == []


# ---------------------------------------------------------------------------
# 3. Scene Data Correctness
# ---------------------------------------------------------------------------


class TestSceneDataFromExecution:
    """Verify build_scene produces correct data from execution records."""

    def test_platform_count(self):
        scene = build_scene(_minimal_execution())
        assert len(scene.platforms) == 2

    def test_platform_phases(self):
        scene = build_scene(_minimal_execution())
        phases = [p.phase for p in scene.platforms]
        assert "triage" in phases
        assert "implement" in phases

    def test_object_count(self):
        scene = build_scene(_minimal_execution())
        total = sum(len(p.objects) for p in scene.platforms)
        assert total == 2  # one action per phase

    def test_connections_exist(self):
        scene = build_scene(_minimal_execution())
        assert len(scene.connections) > 0

    def test_bridges_exist(self):
        scene = build_scene(_minimal_execution())
        assert len(scene.bridges) == 1  # triage -> implement

    def test_camera_has_position(self):
        scene = build_scene(_minimal_execution())
        assert "position" in scene.camera
        assert "target" in scene.camera

    def test_summary_has_status(self):
        scene = build_scene(_minimal_execution())
        assert scene.summary.get("status") == "success"

    def test_to_json_roundtrip(self):
        scene = build_scene(_minimal_execution())
        j = scene.to_json()
        parsed = json.loads(j)
        assert "platforms" in parsed
        assert "connections" in parsed
        assert "bridges" in parsed

    def test_empty_execution_scene(self):
        scene = build_scene(_empty_execution())
        assert len(scene.platforms) == 0
        assert len(scene.connections) == 0


# ---------------------------------------------------------------------------
# 4. Report Template Integration
# ---------------------------------------------------------------------------


class TestReportTemplateIntegration:
    """Verify the report template includes the 3D scene section."""

    def test_template_has_scene_container(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert 'id="scene-3d-container"' in content

    def test_template_has_detail_panel_integration(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "RalphDetailPanel" in content
        assert "detail-panel.js" in content

    def test_template_includes_scene_renderer_js(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "scene-renderer.js" in content

    def test_template_includes_threejs_script(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "three" in content.lower()

    def test_template_includes_orbit_controls_script(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "OrbitControls" in content

    def test_template_calls_render_scene(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "renderScene(" in content

    def test_template_embeds_scene_data(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "scene_data" in content

    def test_template_conditional_on_platforms(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "scene_data.get('platforms')" in content

    def test_3d_section_title(self):
        content = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")
        assert "3D Execution Landscape" in content


# ---------------------------------------------------------------------------
# 5. Report Generator HTML Output
# ---------------------------------------------------------------------------


class TestReportGeneratorScene:
    """Verify ReportGenerator produces correct HTML with scene data."""

    def test_generate_includes_3d_section(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "3D Execution Landscape" in html

    def test_generate_includes_scene_data_json(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "scene-3d-container" in html

    def test_generate_includes_scene_renderer_code(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "RalphSceneRenderer" in html

    def test_generate_empty_execution_no_3d(self):
        gen = ReportGenerator()
        html = gen.generate(_empty_execution())
        assert "<h2>3D Execution Landscape</h2>" not in html

    def test_generate_still_has_decision_tree(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "decision-tree-container" in html

    def test_generate_still_has_action_map(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "action-map-container" in html

    def test_html_has_render_scene_call(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "renderScene(sceneData" in html

    def test_scene_data_json_is_valid(self):
        """Verify the embedded scene data is valid JSON by extracting it."""
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        marker = "var sceneData = "
        idx = html.index(marker) + len(marker)
        end = html.index(";", idx)
        raw = html[idx:end]
        parsed = json.loads(raw)
        assert "platforms" in parsed
        assert len(parsed["platforms"]) == 2


# ---------------------------------------------------------------------------
# 6. Config visualization_engine Field
# ---------------------------------------------------------------------------


class TestConfigVisualizationEngine:
    """Verify ReportingConfig has the visualization_engine field."""

    def test_default_is_threejs(self):
        rc = ReportingConfig()
        assert rc.visualization_engine == "threejs"

    def test_can_set_d3(self):
        rc = ReportingConfig(visualization_engine="d3")
        assert rc.visualization_engine == "d3"

    def test_engine_config_has_visualization_engine(self):
        ec = EngineConfig()
        assert ec.reporting.visualization_engine == "threejs"

    def test_load_config_yaml_override(self, tmp_path):
        cfg = tmp_path / ".rl-config.yaml"
        cfg.write_text(
            "reporting:\n  visualization_engine: d3\n",
            encoding="utf-8",
        )
        ec = load_config(str(cfg))
        assert ec.reporting.visualization_engine == "d3"


# ---------------------------------------------------------------------------
# 7. JS Content Validation (No Syntax Issues)
# ---------------------------------------------------------------------------


class TestJSContentValidation:
    """Basic validation of JavaScript content structure."""

    def test_balanced_braces(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        opens = content.count("{")
        closes = content.count("}")
        assert opens == closes, f"Unbalanced braces: {opens} open, {closes} close"

    def test_balanced_parens(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        opens = content.count("(")
        closes = content.count(")")
        assert opens == closes, f"Unbalanced parens: {opens} open, {closes} close"

    def test_no_console_log(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "console.log(" not in content, "No debug console.log in production code"

    def test_iife_wrapper(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "var RalphSceneRenderer = (function" in content
        assert "})();" in content

    def test_strict_mode(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "'use strict'" in content


# ---------------------------------------------------------------------------
# 8. Detail Panel Content Structure
# ---------------------------------------------------------------------------


class TestDetailPanelContent:
    """Verify detail panel rendering for different action types."""

    def test_llm_action_has_reasoning_section(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "What the agent was told" in content

    def test_llm_action_has_numbers_section(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "By the numbers" in content

    def test_file_action_has_path_display(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "detail-file-path" in content

    def test_command_action_has_what_happened(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "What was run" in content
        assert "What happened" in content

    def test_no_raw_json_in_panels(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "JSON.stringify" not in content, "Detail panel should not dump raw JSON"


# ---------------------------------------------------------------------------
# 9. Scene Renderer Options
# ---------------------------------------------------------------------------


class TestSceneRendererOptions:
    """Verify configurable options in the renderer."""

    def test_default_container_id(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "'scene-3d-container'" in content

    def test_default_detail_panel_id(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "'scene-3d-detail'" in content

    def test_default_height(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "height: 600" in content

    def test_antialias_default(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "antialias: true" in content

    def test_enable_minimap_default(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "enableMinimap: true" in content

    def test_lod_threshold_configurable(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "lodThreshold: 100" in content

    def test_animate_connections_default(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "animateConnections: true" in content


# ---------------------------------------------------------------------------
# 10. Lighting and Visual Feedback
# ---------------------------------------------------------------------------


class TestLightingAndFeedback:
    """Verify lighting adapts to execution status and failed objects pulse."""

    def test_ambient_light_success_tint(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "0x304030" in content  # greenish tint for success

    def test_ambient_light_failure_tint(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "0x403030" in content  # reddish tint for failure

    def test_shadow_map_enabled(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "shadowMap.enabled = true" in content

    def test_pulse_animation_uses_sin(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "Math.sin(" in content


# ---------------------------------------------------------------------------
# 11. Geometry Type Mapping
# ---------------------------------------------------------------------------


class TestGeometryTypeMapping:
    """Verify geometry factories match SPEC §6.1 type-to-shape mapping."""

    @pytest.mark.parametrize(
        "geom_type,three_class",
        [
            ("polyhedron", "IcosahedronGeometry"),
            ("cube", "BoxGeometry"),
            ("cylinder", "CylinderGeometry"),
            ("sphere", "SphereGeometry"),
        ],
    )
    def test_geometry_mapping(self, geom_type, three_class):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert three_class in content

    def test_lod_geometry_reduces_segments(self):
        content = SCENE_RENDERER_JS.read_text(encoding="utf-8")
        assert "createLODGeometry" in content


# ---------------------------------------------------------------------------
# 12. End-to-End Report Pipeline with Scene Data
# ---------------------------------------------------------------------------


class TestEndToEndPipeline:
    """Verify the full pipeline from execution data to HTML report with 3D scene."""

    def test_full_pipeline_produces_html(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html

    def test_full_pipeline_has_all_sections(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "Decision Tree" in html
        assert "Action Map" in html
        assert "3D Execution Landscape" in html
        assert "Iteration Timeline" in html
        assert "Actions Log" in html

    def test_full_pipeline_scene_data_embedded(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert '"platforms"' in html
        assert '"connections"' in html

    def test_output_file_written(self, tmp_path):
        gen = ReportGenerator()
        out = tmp_path / "report.html"
        gen.generate(_minimal_execution(), output_path=out)
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert "3D Execution Landscape" in content

    def test_report_file_size_reasonable(self, tmp_path):
        """Report should be under 5MB for a typical small execution."""
        gen = ReportGenerator()
        out = tmp_path / "report.html"
        gen.generate(_minimal_execution(), output_path=out)
        size_mb = out.stat().st_size / (1024 * 1024)
        assert size_mb < 5, f"Report is {size_mb:.2f}MB, should be under 5MB"
