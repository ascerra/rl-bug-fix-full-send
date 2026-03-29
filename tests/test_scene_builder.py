"""Tests for engine.visualization.scene.builder — Three.js scene graph generation."""

from __future__ import annotations

import json

from engine.visualization.scene.builder import (
    DATA_TYPE_COLORS,
    GEOMETRY_MAP,
    PHASE_ELEVATIONS,
    STATUS_COLORS,
    SceneBuilder,
    SceneConnection,
    SceneData,
    SceneObject,
    ScenePlatform,
    _aggregate_phase_status,
    _build_scene_object,
    _build_summary,
    _default_camera,
    _group_actions_by_iteration,
    _infer_data_type,
    _infer_file_flow_connections,
    _resolve_action_status,
    _token_scale,
    _truncate,
    build_scene,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _default_iterations() -> list:
    return [
        {
            "number": 1,
            "phase": "triage",
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:01:00+00:00",
            "duration_ms": 1500.0,
            "result": {
                "success": True,
                "should_continue": True,
                "next_phase": "implement",
                "escalate": False,
            },
        },
        {
            "number": 2,
            "phase": "implement",
            "started_at": "2026-03-25T10:01:00+00:00",
            "completed_at": "2026-03-25T10:03:00+00:00",
            "duration_ms": 30000.0,
            "result": {
                "success": True,
                "should_continue": True,
                "next_phase": "review",
                "escalate": False,
            },
        },
        {
            "number": 3,
            "phase": "review",
            "started_at": "2026-03-25T10:03:00+00:00",
            "completed_at": "2026-03-25T10:04:00+00:00",
            "duration_ms": 5000.0,
            "result": {
                "success": True,
                "should_continue": True,
                "next_phase": "validate",
                "escalate": False,
            },
        },
    ]


def _default_actions() -> list:
    return [
        {
            "id": "act-1",
            "iteration": 1,
            "phase": "triage",
            "action_type": "llm_query",
            "timestamp": "2026-03-25T10:00:10+00:00",
            "duration_ms": 800.0,
            "input": {"description": "Classify issue as bug or feature"},
            "output": {"success": True},
            "llm_context": {
                "model": "gemini-2.5-pro",
                "tokens_in": 5000,
                "tokens_out": 1500,
            },
            "provenance": {"reasoning": "Issue describes a crash"},
        },
        {
            "id": "act-2",
            "iteration": 1,
            "phase": "triage",
            "action_type": "tool_execution",
            "timestamp": "2026-03-25T10:00:30+00:00",
            "duration_ms": 200.0,
            "input": {"description": "Run existing tests to reproduce"},
            "output": {"success": True},
            "llm_context": {},
            "provenance": {},
        },
        {
            "id": "act-3",
            "iteration": 2,
            "phase": "implement",
            "action_type": "llm_query",
            "timestamp": "2026-03-25T10:01:10+00:00",
            "duration_ms": 2000.0,
            "input": {"description": "Generate fix for nil pointer in reconciler"},
            "output": {"success": True},
            "llm_context": {
                "model": "gemini-2.5-pro",
                "tokens_in": 12000,
                "tokens_out": 3000,
            },
            "provenance": {},
        },
        {
            "id": "act-4",
            "iteration": 2,
            "phase": "implement",
            "action_type": "file_write",
            "timestamp": "2026-03-25T10:01:30+00:00",
            "duration_ms": 50.0,
            "input": {"description": "Write fix to reconciler.go", "path": "pkg/reconciler.go"},
            "output": {"success": True},
            "llm_context": {},
            "provenance": {},
        },
        {
            "id": "act-5",
            "iteration": 3,
            "phase": "review",
            "action_type": "llm_query",
            "timestamp": "2026-03-25T10:03:10+00:00",
            "duration_ms": 1500.0,
            "input": {"description": "Review diff for correctness"},
            "output": {"success": True},
            "llm_context": {
                "model": "gemini-2.5-pro",
                "tokens_in": 8000,
                "tokens_out": 2000,
            },
            "provenance": {},
        },
    ]


def _make_execution(
    iterations: list | None = None,
    actions: list | None = None,
    status: str = "success",
) -> dict:
    _sentinel = object()
    iters = (
        iterations
        if iterations is not _sentinel and iterations is not None
        else (_default_iterations())
    )
    acts = actions if actions is not _sentinel and actions is not None else _default_actions()
    return {
        "execution": {
            "id": "test-exec-001",
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:04:00+00:00",
            "trigger": {"type": "github_issue", "source_url": "https://github.com/o/r/issues/1"},
            "target": {"repo": "o/r", "ref": "abc123"},
            "config": {"max_iterations": 10},
            "iterations": iters,
            "actions": acts,
            "metrics": {
                "total_tokens_in": 25000,
                "total_tokens_out": 6500,
                "time_per_phase_ms": {"triage": 1500, "implement": 30000, "review": 5000},
            },
            "result": {
                "status": status,
                "total_iterations": len(iters),
                "phase_results": [],
            },
        }
    }


# ===========================================================================
# SceneObject dataclass tests
# ===========================================================================


class TestSceneObject:
    def test_defaults(self):
        obj = SceneObject()
        assert obj.id == ""
        assert obj.geometry == "cube"
        assert obj.status == "unknown"
        assert obj.scale == 1.0

    def test_to_dict_keys(self):
        obj = SceneObject(id="o1", action_type="llm_query", geometry="polyhedron")
        d = obj.to_dict()
        expected_keys = {
            "id",
            "action_type",
            "geometry",
            "label",
            "phase",
            "iteration",
            "status",
            "color",
            "position",
            "scale",
            "duration_ms",
            "tokens",
            "timestamp",
            "meta",
        }
        assert set(d.keys()) == expected_keys

    def test_to_dict_values(self):
        obj = SceneObject(id="x", label="hello", tokens=500, position={"x": 1, "y": 2, "z": 3})
        d = obj.to_dict()
        assert d["id"] == "x"
        assert d["label"] == "hello"
        assert d["tokens"] == 500
        assert d["position"]["y"] == 2

    def test_metadata_in_to_dict(self):
        obj = SceneObject(metadata={"key": "val"})
        assert obj.to_dict()["meta"] == {"key": "val"}


# ===========================================================================
# SceneConnection dataclass tests
# ===========================================================================


class TestSceneConnection:
    def test_defaults(self):
        c = SceneConnection()
        assert c.connection_type == "sequential"
        assert c.animated is False

    def test_to_dict(self):
        c = SceneConnection(
            source="a",
            target="b",
            connection_type="phase_transition",
            animated=True,
            data_type="reasoning",
        )
        d = c.to_dict()
        assert d["source"] == "a"
        assert d["target"] == "b"
        assert d["type"] == "phase_transition"
        assert d["animated"] is True
        assert d["data_type"] == "reasoning"


# ===========================================================================
# ScenePlatform dataclass tests
# ===========================================================================


class TestScenePlatform:
    def test_defaults(self):
        p = ScenePlatform()
        assert p.objects == []
        assert p.elevation == 0.0

    def test_to_dict_with_objects(self):
        p = ScenePlatform(
            phase="triage",
            elevation=0.0,
            objects=[SceneObject(id="o1"), SceneObject(id="o2")],
        )
        d = p.to_dict()
        assert len(d["objects"]) == 2
        assert d["objects"][0]["id"] == "o1"

    def test_label_and_status(self):
        p = ScenePlatform(phase="implement", label="Implement", status="success")
        d = p.to_dict()
        assert d["label"] == "Implement"
        assert d["status"] == "success"


# ===========================================================================
# SceneData dataclass tests
# ===========================================================================


class TestSceneData:
    def test_empty(self):
        sd = SceneData()
        d = sd.to_dict()
        assert d["platforms"] == []
        assert d["connections"] == []
        assert d["bridges"] == []

    def test_to_json(self):
        sd = SceneData(summary={"total_objects": 5})
        j = sd.to_json()
        parsed = json.loads(j)
        assert parsed["summary"]["total_objects"] == 5

    def test_to_json_compact(self):
        sd = SceneData(summary={"x": 1})
        j = sd.to_json(indent=None)
        assert "\n" not in j

    def test_roundtrip(self):
        sd = SceneData(
            platforms=[ScenePlatform(phase="triage", elevation=0.0)],
            connections=[SceneConnection(source="a", target="b")],
            bridges=[{"from_phase": "triage", "to_phase": "implement"}],
            camera={"fov": 60},
            summary={"total_objects": 0},
        )
        j = sd.to_json()
        parsed = json.loads(j)
        assert len(parsed["platforms"]) == 1
        assert len(parsed["connections"]) == 1
        assert len(parsed["bridges"]) == 1


# ===========================================================================
# Constants tests
# ===========================================================================


class TestConstants:
    def test_geometry_map_covers_key_types(self):
        assert GEOMETRY_MAP["llm_query"] == "polyhedron"
        assert GEOMETRY_MAP["file_read"] == "cube"
        assert GEOMETRY_MAP["file_write"] == "cube"
        assert GEOMETRY_MAP["shell_run"] == "cylinder"
        assert GEOMETRY_MAP["api_call"] == "sphere"
        assert GEOMETRY_MAP["github_api"] == "sphere"

    def test_status_colors_defined(self):
        for status in ("success", "failure", "retry", "escalated", "unknown"):
            assert status in STATUS_COLORS
            assert STATUS_COLORS[status].startswith("#")

    def test_data_type_colors_defined(self):
        for dt in (
            "code",
            "reasoning",
            "test_pass",
            "test_fail",
            "sequential",
            "phase_transition",
            "data_flow",
        ):
            assert dt in DATA_TYPE_COLORS

    def test_phase_elevations(self):
        assert PHASE_ELEVATIONS["triage"] < PHASE_ELEVATIONS["implement"]
        assert PHASE_ELEVATIONS["implement"] < PHASE_ELEVATIONS["review"]
        assert PHASE_ELEVATIONS["review"] < PHASE_ELEVATIONS["validate"]
        assert PHASE_ELEVATIONS["validate"] < PHASE_ELEVATIONS["report"]


# ===========================================================================
# Helper function tests
# ===========================================================================


class TestHelpers:
    def test_truncate_short(self):
        assert _truncate("hello", 10) == "hello"

    def test_truncate_exact(self):
        assert _truncate("hello", 5) == "hello"

    def test_truncate_long(self):
        result = _truncate("a" * 100, 20)
        assert len(result) == 20
        assert result.endswith("...")

    def test_resolve_action_status_success(self):
        assert _resolve_action_status({"success": True}) == "success"

    def test_resolve_action_status_failure(self):
        assert _resolve_action_status({"success": False}) == "failure"

    def test_resolve_action_status_escalated(self):
        assert _resolve_action_status({"escalate": True}) == "escalated"

    def test_resolve_action_status_empty(self):
        assert _resolve_action_status({}) == "failure"

    def test_token_scale_zero(self):
        assert _token_scale(0) == 0.5

    def test_token_scale_small(self):
        assert _token_scale(500) == 0.7

    def test_token_scale_medium(self):
        assert _token_scale(3000) == 1.0

    def test_token_scale_large(self):
        assert _token_scale(10000) == 1.5

    def test_token_scale_very_large(self):
        assert _token_scale(30000) == 2.0

    def test_token_scale_huge(self):
        assert _token_scale(100000) == 2.5

    def test_group_actions_by_iteration(self):
        actions = [
            {"iteration": 1, "id": "a"},
            {"iteration": 1, "id": "b"},
            {"iteration": 2, "id": "c"},
        ]
        grouped = _group_actions_by_iteration(actions)
        assert len(grouped[1]) == 2
        assert len(grouped[2]) == 1

    def test_group_actions_empty(self):
        assert _group_actions_by_iteration([]) == {}


class TestAggregatePhaseStatus:
    def test_success(self):
        iters = [{"result": {"success": True}}]
        assert _aggregate_phase_status(iters) == "success"

    def test_failure(self):
        iters = [{"result": {"success": False}}]
        assert _aggregate_phase_status(iters) == "failure"

    def test_escalated(self):
        iters = [{"result": {"escalate": True, "success": False}}]
        assert _aggregate_phase_status(iters) == "escalated"

    def test_retry(self):
        iters = [{"result": {"success": False, "should_continue": True}}]
        assert _aggregate_phase_status(iters) == "retry"

    def test_mixed_escalation_wins(self):
        iters = [
            {"result": {"success": True}},
            {"result": {"escalate": True}},
        ]
        assert _aggregate_phase_status(iters) == "escalated"

    def test_mixed_success_over_retry(self):
        iters = [
            {"result": {"success": False, "should_continue": True}},
            {"result": {"success": True}},
        ]
        assert _aggregate_phase_status(iters) == "success"

    def test_empty(self):
        assert _aggregate_phase_status([]) == "unknown"


class TestInferDataType:
    def test_llm_source(self):
        src = SceneObject(action_type="llm_query")
        tgt = SceneObject(action_type="file_write")
        assert _infer_data_type(src, tgt) == "reasoning"

    def test_llm_target(self):
        src = SceneObject(action_type="file_read")
        tgt = SceneObject(action_type="llm_query")
        assert _infer_data_type(src, tgt) == "reasoning"

    def test_shell_success(self):
        src = SceneObject(action_type="shell_run", status="success")
        tgt = SceneObject(action_type="file_write")
        assert _infer_data_type(src, tgt) == "test_pass"

    def test_shell_failure(self):
        src = SceneObject(action_type="shell_run", status="failure")
        tgt = SceneObject(action_type="file_write")
        assert _infer_data_type(src, tgt) == "test_fail"

    def test_file_ops(self):
        src = SceneObject(action_type="file_read")
        tgt = SceneObject(action_type="file_write")
        assert _infer_data_type(src, tgt) == "code"

    def test_default_sequential(self):
        src = SceneObject(action_type="api_call")
        tgt = SceneObject(action_type="api_call")
        assert _infer_data_type(src, tgt) == "sequential"


class TestBuildSceneObject:
    def test_basic(self):
        action = {
            "id": "a1",
            "action_type": "llm_query",
            "phase": "triage",
            "iteration": 1,
            "timestamp": "2026-01-01T00:00:00",
            "duration_ms": 100,
            "input": {"description": "hello world"},
            "output": {"success": True},
            "llm_context": {"tokens_in": 2000, "tokens_out": 500},
            "provenance": {"reasoning": "test"},
        }
        obj = _build_scene_object(action, elevation=4.0, index=0, total_in_group=1)
        assert obj.id == "a1"
        assert obj.geometry == "polyhedron"
        assert obj.status == "success"
        assert obj.color == STATUS_COLORS["success"]
        assert obj.position["y"] == 4.0
        assert obj.tokens == 2500

    def test_geometry_mapping(self):
        for atype, expected_geom in GEOMETRY_MAP.items():
            action = {"id": "x", "action_type": atype, "output": {}, "input": {}}
            obj = _build_scene_object(action, 0, 0, 1)
            assert obj.geometry == expected_geom, f"{atype} -> {expected_geom}"

    def test_unknown_action_type_defaults_cube(self):
        action = {"id": "x", "action_type": "something_new", "output": {}, "input": {}}
        obj = _build_scene_object(action, 0, 0, 1)
        assert obj.geometry == "cube"

    def test_position_spread(self):
        actions = [
            {
                "id": f"a{i}",
                "action_type": "llm_query",
                "output": {"success": True},
                "input": {"description": f"act {i}"},
                "llm_context": {},
            }
            for i in range(3)
        ]
        objs = [_build_scene_object(a, 0.0, i, 3) for i, a in enumerate(actions)]
        xs = [o.position["x"] for o in objs]
        assert xs[0] < xs[1] < xs[2], "Objects should spread along X axis"

    def test_fallback_id(self):
        action = {"action_type": "llm_query", "output": {}, "input": {}}
        obj = _build_scene_object(action, 0, 2, 5)
        assert obj.id == "obj-2"


class TestDefaultCamera:
    def test_zero_elevation(self):
        cam = _default_camera(0)
        assert cam["fov"] == 60
        assert cam["position"]["z"] >= 20

    def test_high_elevation(self):
        cam = _default_camera(20)
        assert cam["target"]["y"] == 10.0
        assert cam["position"]["z"] > 20

    def test_presets_present(self):
        cam = _default_camera(10)
        assert "overview" in cam["presets"]


# ===========================================================================
# File flow connections
# ===========================================================================


class TestFileFlowConnections:
    def test_matching_paths(self):
        platforms = [
            ScenePlatform(
                phase="implement",
                objects=[
                    SceneObject(
                        id="w1",
                        action_type="file_write",
                        metadata={"input": {"path": "pkg/foo.go"}},
                    ),
                ],
            ),
            ScenePlatform(
                phase="review",
                objects=[
                    SceneObject(
                        id="r1", action_type="file_read", metadata={"input": {"path": "pkg/foo.go"}}
                    ),
                ],
            ),
        ]
        conns = _infer_file_flow_connections(platforms)
        assert len(conns) == 1
        assert conns[0].source == "w1"
        assert conns[0].target == "r1"
        assert conns[0].connection_type == "data_flow"
        assert conns[0].animated is True

    def test_no_match(self):
        platforms = [
            ScenePlatform(
                phase="implement",
                objects=[
                    SceneObject(
                        id="w1",
                        action_type="file_write",
                        metadata={"input": {"path": "pkg/foo.go"}},
                    ),
                ],
            ),
            ScenePlatform(
                phase="review",
                objects=[
                    SceneObject(
                        id="r1", action_type="file_read", metadata={"input": {"path": "pkg/bar.go"}}
                    ),
                ],
            ),
        ]
        conns = _infer_file_flow_connections(platforms)
        assert len(conns) == 0

    def test_no_self_connection(self):
        platforms = [
            ScenePlatform(
                phase="implement",
                objects=[
                    SceneObject(
                        id="same", action_type="file_write", metadata={"input": {"path": "f.go"}}
                    ),
                ],
            ),
        ]
        p2 = ScenePlatform(phase="review", objects=[])
        conns = _infer_file_flow_connections([platforms[0], p2])
        assert len(conns) == 0

    def test_label_based_detection(self):
        platforms = [
            ScenePlatform(
                phase="implement",
                objects=[
                    SceneObject(
                        id="w1",
                        action_type="tool_execution",
                        label="Write config.yaml",
                        metadata={"input": {"path": "config.yaml"}},
                    ),
                ],
            ),
            ScenePlatform(
                phase="review",
                objects=[
                    SceneObject(
                        id="r1",
                        action_type="tool_execution",
                        label="Read config.yaml",
                        metadata={"input": {"path": "config.yaml"}},
                    ),
                ],
            ),
        ]
        conns = _infer_file_flow_connections(platforms)
        assert len(conns) == 1

    def test_deduplication(self):
        platforms = [
            ScenePlatform(
                phase="a",
                objects=[
                    SceneObject(
                        id="w1", action_type="file_write", metadata={"input": {"path": "f.go"}}
                    ),
                ],
            ),
            ScenePlatform(
                phase="b",
                objects=[
                    SceneObject(
                        id="r1", action_type="file_read", metadata={"input": {"path": "f.go"}}
                    ),
                    SceneObject(
                        id="r2", action_type="file_read", metadata={"input": {"path": "f.go"}}
                    ),
                ],
            ),
        ]
        conns = _infer_file_flow_connections(platforms)
        pairs = {(c.source, c.target) for c in conns}
        assert ("w1", "r1") in pairs
        assert ("w1", "r2") in pairs


# ===========================================================================
# SceneBuilder.build() integration tests
# ===========================================================================


class TestSceneBuilderBuild:
    def test_empty_execution(self):
        scene = SceneBuilder().build(
            {
                "execution": {
                    "iterations": [],
                    "actions": [],
                    "metrics": {},
                    "result": {},
                }
            }
        )
        assert scene.platforms == []
        assert scene.connections == []
        assert scene.bridges == []
        assert scene.camera["fov"] == 60

    def test_default_fixture(self):
        scene = SceneBuilder().build(_make_execution())
        assert len(scene.platforms) == 3
        phases = [p.phase for p in scene.platforms]
        assert phases == ["triage", "implement", "review"]

    def test_platform_elevations(self):
        scene = SceneBuilder().build(_make_execution())
        elevations = {p.phase: p.elevation for p in scene.platforms}
        assert elevations["triage"] == PHASE_ELEVATIONS["triage"]
        assert elevations["implement"] == PHASE_ELEVATIONS["implement"]
        assert elevations["review"] == PHASE_ELEVATIONS["review"]

    def test_object_counts(self):
        scene = SceneBuilder().build(_make_execution())
        counts = {p.phase: len(p.objects) for p in scene.platforms}
        assert counts["triage"] == 2
        assert counts["implement"] == 2
        assert counts["review"] == 1

    def test_total_object_count(self):
        scene = SceneBuilder().build(_make_execution())
        total = sum(len(p.objects) for p in scene.platforms)
        assert total == 5
        assert scene.summary["total_objects"] == 5

    def test_object_geometry_types(self):
        scene = SceneBuilder().build(_make_execution())
        triage_objs = scene.platforms[0].objects
        assert triage_objs[0].geometry == "polyhedron"
        assert triage_objs[1].geometry == "cylinder"

    def test_object_status_colors(self):
        scene = SceneBuilder().build(_make_execution())
        for platform in scene.platforms:
            for obj in platform.objects:
                assert obj.color in STATUS_COLORS.values()

    def test_platform_statuses(self):
        scene = SceneBuilder().build(_make_execution())
        for platform in scene.platforms:
            assert platform.status == "success"
            assert platform.color == STATUS_COLORS["success"]

    def test_sequential_connections(self):
        scene = SceneBuilder().build(_make_execution())
        seq_conns = [c for c in scene.connections if c.connection_type == "sequential"]
        assert len(seq_conns) >= 2

    def test_phase_transition_connections(self):
        scene = SceneBuilder().build(_make_execution())
        trans = [c for c in scene.connections if c.connection_type == "phase_transition"]
        assert len(trans) == 2
        for t in trans:
            assert t.animated is True

    def test_bridges(self):
        scene = SceneBuilder().build(_make_execution())
        assert len(scene.bridges) == 2
        assert scene.bridges[0]["from_phase"] == "triage"
        assert scene.bridges[0]["to_phase"] == "implement"
        assert scene.bridges[1]["from_phase"] == "implement"
        assert scene.bridges[1]["to_phase"] == "review"

    def test_camera_frames_scene(self):
        scene = SceneBuilder().build(_make_execution())
        cam = scene.camera
        max_elev = max(p.elevation for p in scene.platforms)
        assert cam["target"]["y"] == max_elev / 2

    def test_summary(self):
        scene = SceneBuilder().build(_make_execution())
        s = scene.summary
        assert s["total_objects"] == 5
        assert s["total_platforms"] == 3
        assert s["status"] == "success"
        assert s["total_tokens"] > 0
        assert "llm_query" in s["action_type_counts"]

    def test_to_json_produces_valid_json(self):
        scene = SceneBuilder().build(_make_execution())
        j = scene.to_json()
        parsed = json.loads(j)
        assert "platforms" in parsed
        assert "connections" in parsed

    def test_to_dict_roundtrip(self):
        scene = SceneBuilder().build(_make_execution())
        d = scene.to_dict()
        j = json.dumps(d, default=str)
        parsed = json.loads(j)
        assert parsed["summary"]["total_objects"] == 5


class TestSceneBuilderEdgeCases:
    def test_flat_execution(self):
        """Accepts flat dict without top-level 'execution' key."""
        flat = _make_execution()["execution"]
        scene = SceneBuilder().build(flat)
        assert len(scene.platforms) == 3

    def test_single_action(self):
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[
                    {
                        "number": 1,
                        "phase": "triage",
                        "result": {"success": True},
                    }
                ],
                actions=[
                    {
                        "id": "a1",
                        "iteration": 1,
                        "phase": "triage",
                        "action_type": "llm_query",
                        "input": {"description": "classify"},
                        "output": {"success": True},
                        "llm_context": {"tokens_in": 100, "tokens_out": 50},
                    }
                ],
            )
        )
        assert len(scene.platforms) == 1
        assert len(scene.platforms[0].objects) == 1
        assert scene.connections == []
        assert scene.bridges == []

    def test_no_actions_for_iteration(self):
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[
                    {
                        "number": 1,
                        "phase": "triage",
                        "result": {"success": True},
                    }
                ],
                actions=[],
            )
        )
        assert len(scene.platforms) == 1
        assert scene.platforms[0].objects == []

    def test_failed_execution(self):
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[
                    {
                        "number": 1,
                        "phase": "implement",
                        "result": {"success": False},
                    }
                ],
                actions=[
                    {
                        "id": "a1",
                        "iteration": 1,
                        "phase": "implement",
                        "action_type": "llm_query",
                        "input": {"description": "attempt fix"},
                        "output": {"success": False},
                        "llm_context": {},
                    }
                ],
                status="failure",
            )
        )
        assert scene.platforms[0].status == "failure"
        assert scene.platforms[0].objects[0].status == "failure"
        assert scene.summary["status"] == "failure"

    def test_escalated_execution(self):
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[
                    {
                        "number": 1,
                        "phase": "triage",
                        "result": {"success": False, "escalate": True},
                    }
                ],
                actions=[
                    {
                        "id": "a1",
                        "iteration": 1,
                        "phase": "triage",
                        "action_type": "llm_query",
                        "input": {"description": "classify"},
                        "output": {"escalate": True},
                        "llm_context": {},
                    }
                ],
                status="escalated",
            )
        )
        assert scene.platforms[0].status == "escalated"
        assert scene.platforms[0].objects[0].status == "escalated"

    def test_retry_iterations(self):
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[
                    {
                        "number": 1,
                        "phase": "implement",
                        "result": {"success": False, "should_continue": True},
                    },
                    {"number": 2, "phase": "implement", "result": {"success": True}},
                ],
                actions=[
                    {
                        "id": "a1",
                        "iteration": 1,
                        "phase": "implement",
                        "action_type": "llm_query",
                        "input": {"description": "first attempt"},
                        "output": {"success": False},
                        "llm_context": {},
                    },
                    {
                        "id": "a2",
                        "iteration": 2,
                        "phase": "implement",
                        "action_type": "llm_query",
                        "input": {"description": "second attempt"},
                        "output": {"success": True},
                        "llm_context": {},
                    },
                ],
            )
        )
        assert len(scene.platforms) == 1
        assert scene.platforms[0].phase == "implement"
        assert len(scene.platforms[0].objects) == 2
        assert scene.platforms[0].status == "success"

    def test_custom_phase_elevation(self):
        """Unknown phases get incremental elevations."""
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[
                    {"number": 1, "phase": "custom_phase", "result": {"success": True}},
                ],
                actions=[],
            )
        )
        assert len(scene.platforms) == 1
        assert scene.platforms[0].elevation == 0.0

    def test_many_objects_performance(self):
        """Verify builder handles 200 actions without error."""
        actions = [
            {
                "id": f"a{i}",
                "iteration": 1,
                "phase": "implement",
                "action_type": "llm_query" if i % 2 == 0 else "tool_execution",
                "input": {"description": f"action {i}"},
                "output": {"success": True},
                "llm_context": {"tokens_in": 100, "tokens_out": 50} if i % 2 == 0 else {},
            }
            for i in range(200)
        ]
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[{"number": 1, "phase": "implement", "result": {"success": True}}],
                actions=actions,
            )
        )
        assert sum(len(p.objects) for p in scene.platforms) == 200

    def test_file_flow_connections_across_phases(self):
        scene = SceneBuilder().build(
            _make_execution(
                iterations=[
                    {"number": 1, "phase": "implement", "result": {"success": True}},
                    {"number": 2, "phase": "review", "result": {"success": True}},
                ],
                actions=[
                    {
                        "id": "w1",
                        "iteration": 1,
                        "phase": "implement",
                        "action_type": "file_write",
                        "input": {"description": "write reconciler.go", "path": "pkg/r.go"},
                        "output": {"success": True},
                        "llm_context": {},
                    },
                    {
                        "id": "r1",
                        "iteration": 2,
                        "phase": "review",
                        "action_type": "file_read",
                        "input": {"description": "read reconciler.go", "path": "pkg/r.go"},
                        "output": {"success": True},
                        "llm_context": {},
                    },
                ],
            )
        )
        data_flow = [c for c in scene.connections if c.connection_type == "data_flow"]
        assert len(data_flow) == 1
        assert data_flow[0].source == "w1"
        assert data_flow[0].target == "r1"


# ===========================================================================
# build_scene convenience function
# ===========================================================================


class TestBuildScene:
    def test_convenience_function(self):
        scene = build_scene(_make_execution())
        assert isinstance(scene, SceneData)
        assert len(scene.platforms) == 3

    def test_empty(self):
        scene = build_scene(
            {
                "execution": {
                    "iterations": [],
                    "actions": [],
                    "metrics": {},
                    "result": {},
                }
            }
        )
        assert scene.platforms == []


# ===========================================================================
# Summary builder
# ===========================================================================


class TestBuildSummary:
    def test_empty(self):
        s = _build_summary([], [], {}, {})
        assert s["total_objects"] == 0
        assert s["total_platforms"] == 0
        assert s["status"] == "unknown"

    def test_with_data(self):
        platforms = [
            ScenePlatform(phase="triage", objects=[SceneObject(), SceneObject()]),
            ScenePlatform(phase="implement", objects=[SceneObject()]),
        ]
        actions = [
            {
                "action_type": "llm_query",
                "llm_context": {"tokens_in": 100, "tokens_out": 50},
                "duration_ms": 500,
            },
            {"action_type": "tool_execution", "llm_context": {}, "duration_ms": 200},
        ]
        result = {"status": "success", "total_iterations": 2}
        s = _build_summary(platforms, actions, {}, result)
        assert s["total_objects"] == 3
        assert s["total_platforms"] == 2
        assert s["total_tokens"] == 150
        assert s["total_duration_ms"] == 700.0
        assert s["status"] == "success"
        assert s["action_type_counts"]["llm_query"] == 1
