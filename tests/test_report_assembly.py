"""Tests for Phase 9.6: Report Assembly and Publishing.

Covers:
  - Vendor JS files exist (Three.js, OrbitControls, D3.js)
  - Three.js mode produces self-contained HTML (no external CDN URLs)
  - D3 legacy mode omits the 3D section
  - File size sanity check (< 5 MB for typical execution)
  - Comparison ghost objects present in scene data
  - ReportGenerator config routing (threejs vs d3)
  - ReportPublisher passes config to generator
  - Ghost object rendering metadata in scene-renderer.js
  - Template correctly uses vendored variables
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from engine.config import ReportingConfig
from engine.visualization.report_generator import (
    _VENDOR_DIR,
    _VENDOR_FILES,
    ReportGenerator,
    extract_report_data,
)
from engine.visualization.scene.builder import SceneBuilder

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "visual-report"
VENDOR_DIR = TEMPLATES_DIR / "vendor"
SCENE_RENDERER_JS = TEMPLATES_DIR / "scene-renderer.js"


def _minimal_execution(
    *,
    status: str = "success",
    with_comparison: bool = False,
) -> dict[str, Any]:
    exec_data: dict[str, Any] = {
        "execution": {
            "id": "assembly-test-001",
            "started_at": "2026-03-29T10:00:00Z",
            "completed_at": "2026-03-29T10:05:00Z",
            "trigger": {"type": "manual", "source_url": "https://github.com/o/r/issues/1"},
            "target": {"repo": "o/r", "ref": "abc123", "comparison_ref": ""},
            "config": {},
            "iterations": [
                {
                    "number": 1,
                    "phase": "triage",
                    "started_at": "2026-03-29T10:00:00Z",
                    "completed_at": "2026-03-29T10:01:00Z",
                    "duration_ms": 1000,
                    "result": {"success": True, "next_phase": "implement"},
                },
                {
                    "number": 2,
                    "phase": "implement",
                    "started_at": "2026-03-29T10:01:00Z",
                    "completed_at": "2026-03-29T10:03:00Z",
                    "duration_ms": 3000,
                    "result": {"success": True, "next_phase": "review"},
                },
                {
                    "number": 3,
                    "phase": "review",
                    "started_at": "2026-03-29T10:03:00Z",
                    "completed_at": "2026-03-29T10:04:00Z",
                    "duration_ms": 1000,
                    "result": {"success": True, "next_phase": "validate"},
                },
                {
                    "number": 4,
                    "phase": "validate",
                    "started_at": "2026-03-29T10:04:00Z",
                    "completed_at": "2026-03-29T10:05:00Z",
                    "duration_ms": 1000,
                    "result": {"success": True, "next_phase": "report"},
                },
            ],
            "actions": [
                {
                    "id": "act-1",
                    "iteration": 1,
                    "phase": "triage",
                    "action_type": "llm_query",
                    "timestamp": "2026-03-29T10:00:30Z",
                    "duration_ms": 500,
                    "input": {"description": "Classify issue"},
                    "output": {"success": True},
                    "llm_context": {
                        "model": "gemini-2.5-pro",
                        "provider": "gemini",
                        "tokens_in": 500,
                        "tokens_out": 200,
                    },
                },
                {
                    "id": "act-2",
                    "iteration": 2,
                    "phase": "implement",
                    "action_type": "file_write",
                    "timestamp": "2026-03-29T10:02:00Z",
                    "duration_ms": 100,
                    "input": {"description": "Write fix", "path": "pkg/main.go"},
                    "output": {"success": True},
                    "llm_context": {},
                },
                {
                    "id": "act-3",
                    "iteration": 2,
                    "phase": "implement",
                    "action_type": "shell_run",
                    "timestamp": "2026-03-29T10:02:30Z",
                    "duration_ms": 1000,
                    "input": {"description": "Run go test"},
                    "output": {"success": True},
                    "llm_context": {},
                },
            ],
            "metrics": {
                "total_llm_calls": 1,
                "total_tokens_in": 500,
                "total_tokens_out": 200,
                "total_tool_executions": 2,
            },
            "result": {
                "status": status,
                "total_iterations": 4,
                "phase_results": [],
            },
        }
    }

    if with_comparison:
        exec_data["execution"]["target"]["comparison_ref"] = "human-fix-sha"
        exec_data["execution"]["result"]["comparison"] = {
            "agent_diff": (
                "diff --git a/pkg/main.go b/pkg/main.go\n"
                "--- a/pkg/main.go\n+++ b/pkg/main.go\n"
                "@@ -10,3 +10,5 @@\n+  if x == nil {\n+    return\n+  }\n"
            ),
            "human_diff": (
                "diff --git a/pkg/main.go b/pkg/main.go\n"
                "--- a/pkg/main.go\n+++ b/pkg/main.go\n"
                "@@ -10,3 +10,4 @@\n+  if x == nil {\n"
                '+    return nil, fmt.Errorf("x is nil")\n+  }\n'
                "diff --git a/pkg/main_test.go b/pkg/main_test.go\n"
                "--- a/pkg/main_test.go\n+++ b/pkg/main_test.go\n"
                "@@ -1,3 +1,10 @@\n+func TestNilX(t *testing.T) {\n"
            ),
            "similarity_score": 0.75,
            "analysis": "Both fixes add a nil check, human version also adds a test.",
        }

    return exec_data


# ---------------------------------------------------------------------------
# Vendor files exist
# ---------------------------------------------------------------------------


class TestVendorFiles:
    def test_vendor_dir_exists(self):
        assert VENDOR_DIR.is_dir()

    def test_three_js_exists(self):
        path = VENDOR_DIR / "three.min.js"
        assert path.is_file()
        assert path.stat().st_size > 100_000

    def test_orbit_controls_exists(self):
        path = VENDOR_DIR / "orbit-controls.min.js"
        assert path.is_file()
        assert path.stat().st_size > 1_000

    def test_d3_js_exists(self):
        path = VENDOR_DIR / "d3.v7.min.js"
        assert path.is_file()
        assert path.stat().st_size > 100_000

    def test_vendor_files_map_complete(self):
        assert "three_js" in _VENDOR_FILES
        assert "orbit_controls_js" in _VENDOR_FILES
        assert "d3_js" in _VENDOR_FILES

    def test_vendor_dir_constant(self):
        assert _VENDOR_DIR.name == "vendor"
        assert _VENDOR_DIR.parent.name == "visual-report"


# ---------------------------------------------------------------------------
# Self-contained HTML (no CDN references)
# ---------------------------------------------------------------------------


class TestSelfContainedHTML:
    def test_no_cdn_script_tags(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert 'src="https://unpkg.com' not in html
        assert 'src="https://cdn.jsdelivr.net' not in html
        assert 'src="https://d3js.org' not in html

    def test_three_js_inlined(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "THREE" in html
        assert "OrbitControls" in html

    def test_d3_js_inlined(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "function(t,n)" in html or "d3.select" in html

    def test_custom_js_inlined(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        assert "renderScene" in html
        assert "RalphDetailPanel" in html
        assert "renderTimeline" in html
        assert "renderDecisionTree" in html
        assert "renderActionMap" in html

    def test_file_size_under_5mb(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        size_bytes = len(html.encode("utf-8"))
        assert size_bytes < 5_000_000, f"Report is {size_bytes / 1_000_000:.1f} MB"

    def test_file_size_includes_vendor_libs(self):
        gen = ReportGenerator()
        html = gen.generate(_minimal_execution())
        size_bytes = len(html.encode("utf-8"))
        assert size_bytes > 500_000, "Report should include vendored JS (>500KB)"


# ---------------------------------------------------------------------------
# D3 legacy mode
# ---------------------------------------------------------------------------


class TestD3LegacyMode:
    def test_d3_mode_skips_three_js(self):
        config = ReportingConfig(visualization_engine="d3")
        gen = ReportGenerator(config=config)
        html = gen.generate(_minimal_execution())
        assert "THREE" not in html

    def test_d3_mode_includes_d3(self):
        config = ReportingConfig(visualization_engine="d3")
        gen = ReportGenerator(config=config)
        html = gen.generate(_minimal_execution())
        assert "function(t,n)" in html or "d3.select" in html

    def test_d3_mode_includes_decision_tree(self):
        config = ReportingConfig(visualization_engine="d3")
        gen = ReportGenerator(config=config)
        html = gen.generate(_minimal_execution())
        assert "renderDecisionTree" in html

    def test_d3_mode_includes_action_map(self):
        config = ReportingConfig(visualization_engine="d3")
        gen = ReportGenerator(config=config)
        html = gen.generate(_minimal_execution())
        assert "renderActionMap" in html

    def test_d3_mode_no_3d_section(self):
        config = ReportingConfig(visualization_engine="d3")
        gen = ReportGenerator(config=config)
        html = gen.generate(_minimal_execution())
        assert "scene-3d-container" not in html
        assert "<h2>3D Execution Landscape</h2>" not in html

    def test_d3_mode_no_enter_3d_button(self):
        config = ReportingConfig(visualization_engine="d3")
        gen = ReportGenerator(config=config)
        html = gen.generate(_minimal_execution())
        assert "enter-3d-btn" not in html

    def test_d3_mode_smaller_file(self):
        gen_3d = ReportGenerator(config=ReportingConfig(visualization_engine="threejs"))
        gen_d3 = ReportGenerator(config=ReportingConfig(visualization_engine="d3"))
        html_3d = gen_3d.generate(_minimal_execution())
        html_d3 = gen_d3.generate(_minimal_execution())
        assert len(html_d3) < len(html_3d)

    def test_d3_mode_report_data_empty_scene(self):
        rd = extract_report_data(_minimal_execution(), visualization_engine="d3")
        assert rd.scene_data == {}
        assert rd.timeline_data == {}


# ---------------------------------------------------------------------------
# ReportGenerator config routing
# ---------------------------------------------------------------------------


class TestReportGeneratorConfig:
    def test_default_engine_is_threejs(self):
        gen = ReportGenerator()
        assert gen.visualization_engine == "threejs"

    def test_config_sets_engine(self):
        gen = ReportGenerator(config=ReportingConfig(visualization_engine="d3"))
        assert gen.visualization_engine == "d3"

    def test_vendor_cache_populated_on_first_render(self):
        gen = ReportGenerator()
        assert gen._vendor_cache == {}
        gen.generate(_minimal_execution())
        assert "d3_js" in gen._vendor_cache
        assert "three_js" in gen._vendor_cache

    def test_vendor_cache_not_populated_for_three_in_d3_mode(self):
        gen = ReportGenerator(config=ReportingConfig(visualization_engine="d3"))
        gen.generate(_minimal_execution())
        assert "d3_js" in gen._vendor_cache
        assert "three_js" not in gen._vendor_cache

    def test_missing_vendor_file_returns_empty(self):
        gen = ReportGenerator()
        result = gen._load_vendor_file("nonexistent_key")
        assert result == ""


# ---------------------------------------------------------------------------
# ReportPublisher passes config
# ---------------------------------------------------------------------------


class TestPublisherConfig:
    def test_publisher_passes_config_to_generator(self):
        from engine.visualization.publisher import ReportPublisher

        config = ReportingConfig(visualization_engine="d3")
        pub = ReportPublisher(output_dir="/tmp/rtest", config=config)
        assert pub._generator.visualization_engine == "d3"

    def test_publisher_default_config(self):
        from engine.visualization.publisher import ReportPublisher

        pub = ReportPublisher(output_dir="/tmp/rtest")
        assert pub._generator.visualization_engine == "threejs"


# ---------------------------------------------------------------------------
# Comparison ghost objects
# ---------------------------------------------------------------------------


class TestComparisonGhosts:
    def test_ghost_objects_added_for_comparison(self):
        builder = SceneBuilder()
        scene = builder.build(_minimal_execution(with_comparison=True))
        comparison = {
            "enabled": True,
            "human_summary": {
                "files": [
                    {"path": "pkg/main.go", "lines_added": 3, "lines_removed": 0},
                    {"path": "pkg/main_test.go", "lines_added": 7, "lines_removed": 0},
                ]
            },
        }
        builder.add_comparison_ghosts(scene, comparison)

        ghost_ids = [
            obj.id for p in scene.platforms for obj in p.objects if obj.metadata.get("ghost")
        ]
        assert len(ghost_ids) == 2
        assert "ghost-human-0" in ghost_ids
        assert "ghost-human-1" in ghost_ids

    def test_ghost_objects_on_implement_platform(self):
        builder = SceneBuilder()
        scene = builder.build(_minimal_execution(with_comparison=True))
        comparison = {
            "enabled": True,
            "human_summary": {
                "files": [{"path": "pkg/main.go", "lines_added": 1, "lines_removed": 0}]
            },
        }
        builder.add_comparison_ghosts(scene, comparison)

        impl_platform = [p for p in scene.platforms if p.phase == "implement"]
        assert len(impl_platform) == 1
        ghosts = [o for o in impl_platform[0].objects if o.metadata.get("ghost")]
        assert len(ghosts) == 1

    def test_ghost_metadata_fields(self):
        builder = SceneBuilder()
        scene = builder.build(_minimal_execution(with_comparison=True))
        comparison = {
            "enabled": True,
            "human_summary": {
                "files": [{"path": "pkg/main.go", "lines_added": 3, "lines_removed": 1}]
            },
        }
        builder.add_comparison_ghosts(scene, comparison)

        ghosts = [obj for p in scene.platforms for obj in p.objects if obj.metadata.get("ghost")]
        assert len(ghosts) == 1
        ghost = ghosts[0]
        assert ghost.metadata["ghost"] is True
        assert ghost.metadata["comparison_source"] == "human"
        assert ghost.metadata["file_path"] == "pkg/main.go"
        assert ghost.metadata["lines_added"] == 3
        assert ghost.metadata["lines_removed"] == 1
        assert ghost.color == "#ffffff"

    def test_ghost_connections_added(self):
        builder = SceneBuilder()
        scene = builder.build(_minimal_execution(with_comparison=True))
        comparison = {
            "enabled": True,
            "human_summary": {
                "files": [
                    {"path": "a.go", "lines_added": 1, "lines_removed": 0},
                    {"path": "b.go", "lines_added": 2, "lines_removed": 0},
                ]
            },
        }
        builder.add_comparison_ghosts(scene, comparison)

        ghost_conns = [
            c
            for c in scene.connections
            if c.source.startswith("ghost-") or c.target.startswith("ghost-")
        ]
        assert len(ghost_conns) >= 2

    def test_no_ghosts_when_comparison_disabled(self):
        builder = SceneBuilder()
        scene = builder.build(_minimal_execution())
        comparison = {"enabled": False}
        builder.add_comparison_ghosts(scene, comparison)
        ghost_ids = [
            obj.id for p in scene.platforms for obj in p.objects if obj.metadata.get("ghost")
        ]
        assert len(ghost_ids) == 0

    def test_no_ghosts_when_no_human_files(self):
        builder = SceneBuilder()
        scene = builder.build(_minimal_execution(with_comparison=True))
        comparison = {"enabled": True, "human_summary": {"files": []}}
        builder.add_comparison_ghosts(scene, comparison)
        ghost_ids = [
            obj.id for p in scene.platforms for obj in p.objects if obj.metadata.get("ghost")
        ]
        assert len(ghost_ids) == 0

    def test_ghost_z_offset(self):
        builder = SceneBuilder()
        scene = builder.build(_minimal_execution(with_comparison=True))
        comparison = {
            "enabled": True,
            "human_summary": {"files": [{"path": "a.go", "lines_added": 1, "lines_removed": 0}]},
        }
        builder.add_comparison_ghosts(scene, comparison)
        ghosts = [obj for p in scene.platforms for obj in p.objects if obj.metadata.get("ghost")]
        assert ghosts[0].position["z"] == 3.0


# ---------------------------------------------------------------------------
# extract_report_data integration with comparison ghosts
# ---------------------------------------------------------------------------


class TestExtractReportDataGhosts:
    def test_comparison_ghosts_in_scene_data(self):
        rd = extract_report_data(_minimal_execution(with_comparison=True))
        all_objects = []
        for p in rd.scene_data.get("platforms", []):
            all_objects.extend(p.get("objects", []))
        ghost_objs = [o for o in all_objects if o.get("meta", {}).get("ghost")]
        assert len(ghost_objs) == 2

    def test_no_ghosts_without_comparison(self):
        rd = extract_report_data(_minimal_execution(with_comparison=False))
        all_objects = []
        for p in rd.scene_data.get("platforms", []):
            all_objects.extend(p.get("objects", []))
        ghost_objs = [o for o in all_objects if o.get("meta", {}).get("ghost")]
        assert len(ghost_objs) == 0


# ---------------------------------------------------------------------------
# scene-renderer.js ghost rendering
# ---------------------------------------------------------------------------


class TestSceneRendererGhosts:
    @pytest.fixture(autouse=True)
    def _load_js(self):
        self.js_content = SCENE_RENDERER_JS.read_text(encoding="utf-8")

    def test_ghost_detection_in_js(self):
        assert "isGhost" in self.js_content
        assert "objData.meta" in self.js_content

    def test_ghost_transparency(self):
        assert "transparent: isGhost" in self.js_content
        assert "opacity: isGhost ? 0.3" in self.js_content

    def test_ghost_wireframe(self):
        assert "wireframe: isGhost" in self.js_content

    def test_ghost_no_shadow(self):
        has_shadow_guard = (
            "castShadow = !isGhost" in self.js_content or "castShadow: !isGhost" in self.js_content
        )
        assert has_shadow_guard


# ---------------------------------------------------------------------------
# Template uses vendored variables (not CDN)
# ---------------------------------------------------------------------------


class TestTemplateVendorIntegration:
    @pytest.fixture(autouse=True)
    def _load_template(self):
        self.template = (TEMPLATES_DIR / "report.html").read_text(encoding="utf-8")

    def test_no_cdn_in_template(self):
        assert 'src="https://unpkg.com' not in self.template
        assert 'src="https://cdn.jsdelivr.net' not in self.template
        assert 'src="https://d3js.org' not in self.template

    def test_vendor_d3_variable(self):
        assert "vendor_d3_js" in self.template

    def test_vendor_three_variable(self):
        assert "vendor_three_js" in self.template

    def test_vendor_orbit_controls_variable(self):
        assert "vendor_orbit_controls_js" in self.template

    def test_visualization_engine_guard(self):
        assert "visualization_engine" in self.template

    def test_d3_engine_guard_on_3d_section(self):
        assert "visualization_engine != 'd3'" in self.template


# ---------------------------------------------------------------------------
# Output file writing
# ---------------------------------------------------------------------------


class TestOutputWriting:
    def test_write_report_to_file(self, tmp_path: Path):
        gen = ReportGenerator()
        out = tmp_path / "report.html"
        html = gen.generate(_minimal_execution(), output_path=out)
        assert out.is_file()
        assert out.stat().st_size > 500_000
        assert html == out.read_text(encoding="utf-8")

    def test_write_report_d3_mode(self, tmp_path: Path):
        config = ReportingConfig(visualization_engine="d3")
        gen = ReportGenerator(config=config)
        out = tmp_path / "report.html"
        gen.generate(_minimal_execution(), output_path=out)
        content = out.read_text(encoding="utf-8")
        assert "THREE" not in content
        assert out.stat().st_size < 500_000

    def test_publisher_publish_produces_report(self, tmp_path: Path):
        from engine.visualization.publisher import ReportPublisher

        pub = ReportPublisher(output_dir=tmp_path)
        result = pub.publish(_minimal_execution())
        assert result.success
        report_file = tmp_path / "report.html"
        assert report_file.is_file()
        html = report_file.read_text(encoding="utf-8")
        assert 'src="https://' not in html
