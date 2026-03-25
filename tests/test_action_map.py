"""Tests for engine.visualization.action_map."""

from __future__ import annotations

from engine.visualization.action_map import (
    ActionMapData,
    ActionMapEdge,
    ActionMapLayer,
    ActionMapNode,
    _build_edges,
    _build_layers,
    _build_node,
    _build_summary,
    _group_actions_by_iteration,
    _infer_file_flow_edges,
    _truncate,
    build_action_map,
    total_nodes,
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
    ]


def _default_actions() -> list:
    return [
        {
            "id": "act-1",
            "iteration": 1,
            "phase": "triage",
            "action_type": "llm_query",
            "timestamp": "2026-03-25T10:00:05+00:00",
            "input": {"description": "Classify issue", "context": {}},
            "output": {"success": True, "data": {}},
            "duration_ms": 500.0,
            "llm_context": {
                "model": "mock",
                "tokens_in": 1000,
                "tokens_out": 200,
            },
            "provenance": {},
        },
        {
            "id": "act-2",
            "iteration": 1,
            "phase": "triage",
            "action_type": "tool_execution",
            "timestamp": "2026-03-25T10:00:30+00:00",
            "input": {"description": "Read file controller.py", "path": "/tmp/controller.py"},
            "output": {"success": True, "data": {}},
            "duration_ms": 50.0,
            "llm_context": {},
            "provenance": {},
        },
        {
            "id": "act-3",
            "iteration": 2,
            "phase": "implement",
            "action_type": "llm_query",
            "timestamp": "2026-03-25T10:01:10+00:00",
            "input": {"description": "Generate fix for nil pointer", "context": {}},
            "output": {"success": True, "data": {}},
            "duration_ms": 2000.0,
            "llm_context": {
                "model": "mock",
                "tokens_in": 3000,
                "tokens_out": 800,
            },
            "provenance": {},
        },
        {
            "id": "act-4",
            "iteration": 2,
            "phase": "implement",
            "action_type": "tool_execution",
            "timestamp": "2026-03-25T10:01:30+00:00",
            "input": {
                "description": "Write fix to controller.py",
                "path": "/tmp/controller.py",
            },
            "output": {"success": True, "data": {}},
            "duration_ms": 200.0,
            "llm_context": {},
            "provenance": {},
        },
    ]


def _make_execution(
    *,
    status: str = "success",
    iterations: list | None = None,
    actions: list | None = None,
    metrics: dict | None = None,
    exec_id: str = "test-exec-id-12345",
) -> dict:
    """Build a minimal execution record dict for testing."""
    return {
        "execution": {
            "id": exec_id,
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:05:00+00:00",
            "trigger": {
                "type": "github_issue",
                "source_url": "https://github.com/o/r/issues/1",
            },
            "target": {"repo_path": "/tmp/repo", "comparison_ref": ""},
            "config": {"llm": {"provider": "mock"}},
            "iterations": _default_iterations() if iterations is None else iterations,
            "result": {
                "status": status,
                "total_iterations": 2,
                "phase_results": [
                    {"phase": "triage", "success": True, "escalate": False},
                    {"phase": "implement", "success": True, "escalate": False},
                ],
            },
            "metrics": metrics
            or {
                "total_iterations": 2,
                "total_llm_calls": 2,
                "total_tokens_in": 4000,
                "total_tokens_out": 1000,
                "total_tool_executions": 2,
                "time_per_phase_ms": {"triage": 1500.0, "implement": 30000.0},
                "phase_iteration_counts": {"triage": 1, "implement": 1},
                "errors": [],
            },
            "actions": _default_actions() if actions is None else actions,
        }
    }


def _make_flat_execution(**kwargs) -> dict:
    """Build a flat execution dict (no top-level 'execution' key)."""
    return _make_execution(**kwargs)["execution"]


# ---------------------------------------------------------------------------
# ActionMapNode tests
# ---------------------------------------------------------------------------


class TestActionMapNode:
    def test_default_values(self):
        node = ActionMapNode()
        assert node.id == ""
        assert node.action_type == "unknown"
        assert node.description == ""
        assert node.phase == ""
        assert node.iteration == 0
        assert node.status == "unknown"
        assert node.duration_ms == 0.0
        assert node.tokens == 0
        assert node.metadata == {}

    def test_to_dict(self):
        node = ActionMapNode(
            id="n1",
            action_type="llm_query",
            description="Test query",
            phase="triage",
            iteration=1,
            status="success",
            duration_ms=500.0,
            tokens=1200,
        )
        d = node.to_dict()
        assert d["id"] == "n1"
        assert d["action_type"] == "llm_query"
        assert d["description"] == "Test query"
        assert d["phase"] == "triage"
        assert d["iteration"] == 1
        assert d["status"] == "success"
        assert d["duration_ms"] == 500.0
        assert d["tokens"] == 1200
        assert d["meta"] == {}

    def test_to_dict_with_metadata(self):
        node = ActionMapNode(id="n1", metadata={"key": "value"})
        d = node.to_dict()
        assert d["meta"]["key"] == "value"


# ---------------------------------------------------------------------------
# ActionMapEdge tests
# ---------------------------------------------------------------------------


class TestActionMapEdge:
    def test_default_values(self):
        edge = ActionMapEdge()
        assert edge.source == ""
        assert edge.target == ""
        assert edge.edge_type == "sequential"

    def test_to_dict(self):
        edge = ActionMapEdge(source="a", target="b", edge_type="phase_transition")
        d = edge.to_dict()
        assert d["source"] == "a"
        assert d["target"] == "b"
        assert d["type"] == "phase_transition"


# ---------------------------------------------------------------------------
# ActionMapLayer tests
# ---------------------------------------------------------------------------


class TestActionMapLayer:
    def test_default_values(self):
        layer = ActionMapLayer()
        assert layer.phase == ""
        assert layer.iteration == 0
        assert layer.successful is False
        assert layer.nodes == []

    def test_to_dict(self):
        node = ActionMapNode(id="n1", action_type="llm_query")
        layer = ActionMapLayer(phase="triage", iteration=1, successful=True, nodes=[node])
        d = layer.to_dict()
        assert d["phase"] == "triage"
        assert d["iteration"] == 1
        assert d["successful"] is True
        assert len(d["nodes"]) == 1
        assert d["nodes"][0]["id"] == "n1"

    def test_to_dict_empty_nodes(self):
        layer = ActionMapLayer(phase="implement")
        d = layer.to_dict()
        assert d["nodes"] == []


# ---------------------------------------------------------------------------
# ActionMapData tests
# ---------------------------------------------------------------------------


class TestActionMapData:
    def test_default_values(self):
        data = ActionMapData()
        assert data.layers == []
        assert data.edges == []
        assert data.summary == {}

    def test_to_dict(self):
        node = ActionMapNode(id="n1")
        layer = ActionMapLayer(phase="triage", nodes=[node])
        edge = ActionMapEdge(source="n1", target="n2")
        data = ActionMapData(
            layers=[layer],
            edges=[edge],
            summary={"total_actions": 1},
        )
        d = data.to_dict()
        assert len(d["layers"]) == 1
        assert len(d["edges"]) == 1
        assert d["summary"]["total_actions"] == 1


# ---------------------------------------------------------------------------
# build_action_map tests
# ---------------------------------------------------------------------------


class TestBuildActionMap:
    def test_basic_map(self):
        result = build_action_map(_make_execution())
        assert isinstance(result, ActionMapData)
        assert len(result.layers) == 2
        assert result.layers[0].phase == "triage"
        assert result.layers[1].phase == "implement"

    def test_from_wrapped_execution(self):
        result = build_action_map(_make_execution())
        assert len(result.layers) == 2

    def test_from_flat_execution(self):
        result = build_action_map(_make_flat_execution())
        assert len(result.layers) == 2

    def test_layer_has_correct_nodes(self):
        result = build_action_map(_make_execution())
        triage_layer = result.layers[0]
        assert len(triage_layer.nodes) == 2
        assert triage_layer.nodes[0].action_type == "llm_query"
        assert triage_layer.nodes[1].action_type == "tool_execution"

    def test_implement_layer_nodes(self):
        result = build_action_map(_make_execution())
        impl_layer = result.layers[1]
        assert len(impl_layer.nodes) == 2
        assert impl_layer.nodes[0].action_type == "llm_query"
        assert impl_layer.nodes[1].action_type == "tool_execution"

    def test_layer_success_status(self):
        result = build_action_map(_make_execution())
        assert result.layers[0].successful is True
        assert result.layers[1].successful is True

    def test_layer_failure_status(self):
        iterations = [
            {
                "number": 1,
                "phase": "triage",
                "result": {"success": False, "should_continue": True, "escalate": False},
            }
        ]
        result = build_action_map(_make_execution(iterations=iterations, actions=[]))
        assert result.layers[0].successful is False

    def test_empty_execution(self):
        result = build_action_map({})
        assert len(result.layers) == 0
        assert len(result.edges) == 0
        assert result.summary["total_actions"] == 0

    def test_empty_iterations(self):
        result = build_action_map(_make_execution(iterations=[], actions=[]))
        assert len(result.layers) == 0

    def test_iterations_without_actions(self):
        result = build_action_map(_make_execution(actions=[]))
        assert len(result.layers) == 2
        assert len(result.layers[0].nodes) == 0
        assert len(result.layers[1].nodes) == 0

    def test_sequential_edges_within_layer(self):
        result = build_action_map(_make_execution())
        sequential = [e for e in result.edges if e.edge_type == "sequential"]
        assert any(e.source == "act-1" and e.target == "act-2" for e in sequential)
        assert any(e.source == "act-3" and e.target == "act-4" for e in sequential)

    def test_phase_transition_edges(self):
        result = build_action_map(_make_execution())
        transitions = [e for e in result.edges if e.edge_type == "phase_transition"]
        assert len(transitions) == 1
        assert transitions[0].source == "act-2"
        assert transitions[0].target == "act-3"

    def test_data_flow_edges(self):
        result = build_action_map(_make_execution())
        data_flows = [e for e in result.edges if e.edge_type == "data_flow"]
        assert len(data_flows) == 1
        assert data_flows[0].source == "act-2"
        assert data_flows[0].target == "act-4"

    def test_no_phase_transition_with_empty_layers(self):
        result = build_action_map(_make_execution(actions=[]))
        transitions = [e for e in result.edges if e.edge_type == "phase_transition"]
        assert len(transitions) == 0

    def test_summary_populated(self):
        result = build_action_map(_make_execution())
        assert result.summary["total_actions"] == 4
        assert result.summary["total_layers"] == 2
        assert result.summary["phases"] == ["triage", "implement"]
        assert result.summary["total_tokens"] == 5000
        assert result.summary["total_duration_ms"] > 0

    def test_summary_action_type_counts(self):
        result = build_action_map(_make_execution())
        counts = result.summary["action_type_counts"]
        assert counts["llm_query"] == 2
        assert counts["tool_execution"] == 2

    def test_node_tokens_from_llm_context(self):
        result = build_action_map(_make_execution())
        llm_node = result.layers[0].nodes[0]
        assert llm_node.tokens == 1200

    def test_node_tokens_zero_for_non_llm(self):
        result = build_action_map(_make_execution())
        tool_node = result.layers[0].nodes[1]
        assert tool_node.tokens == 0

    def test_node_status(self):
        result = build_action_map(_make_execution())
        assert result.layers[0].nodes[0].status == "success"

    def test_node_failed_status(self):
        actions = [
            {
                "id": "fail-1",
                "iteration": 1,
                "phase": "triage",
                "action_type": "tool_execution",
                "input": {"description": "Run tests"},
                "output": {"success": False, "error": "exit code 1"},
                "duration_ms": 100,
                "llm_context": {},
            }
        ]
        result = build_action_map(_make_execution(actions=actions))
        assert result.layers[0].nodes[0].status == "failure"

    def test_to_dict_roundtrip(self):
        result = build_action_map(_make_execution())
        d = result.to_dict()
        assert isinstance(d["layers"], list)
        assert isinstance(d["edges"], list)
        assert isinstance(d["summary"], dict)
        assert len(d["layers"]) == 2

    def test_multiple_iterations_same_phase(self):
        iterations = [
            {
                "number": 1,
                "phase": "implement",
                "result": {"success": False, "should_continue": True, "escalate": False},
            },
            {
                "number": 2,
                "phase": "implement",
                "result": {"success": True, "should_continue": True, "escalate": False},
            },
        ]
        actions = [
            {
                "id": "a1",
                "iteration": 1,
                "phase": "implement",
                "action_type": "llm_query",
                "input": {"description": "First attempt"},
                "output": {"success": True},
                "llm_context": {},
            },
            {
                "id": "a2",
                "iteration": 2,
                "phase": "implement",
                "action_type": "llm_query",
                "input": {"description": "Second attempt"},
                "output": {"success": True},
                "llm_context": {},
            },
        ]
        result = build_action_map(_make_execution(iterations=iterations, actions=actions))
        assert len(result.layers) == 2
        assert result.layers[0].successful is False
        assert result.layers[1].successful is True

    def test_single_action_no_sequential_edges(self):
        iterations = [
            {
                "number": 1,
                "phase": "triage",
                "result": {"success": True, "should_continue": True, "escalate": False},
            }
        ]
        actions = [
            {
                "id": "solo",
                "iteration": 1,
                "phase": "triage",
                "action_type": "llm_query",
                "input": {"description": "Only action"},
                "output": {"success": True},
                "llm_context": {},
            }
        ]
        result = build_action_map(_make_execution(iterations=iterations, actions=actions))
        sequential = [e for e in result.edges if e.edge_type == "sequential"]
        assert len(sequential) == 0


# ---------------------------------------------------------------------------
# total_nodes tests
# ---------------------------------------------------------------------------


class TestTotalNodes:
    def test_with_data(self):
        result = build_action_map(_make_execution())
        assert total_nodes(result) == 4

    def test_empty(self):
        result = build_action_map({})
        assert total_nodes(result) == 0

    def test_no_actions(self):
        result = build_action_map(_make_execution(actions=[]))
        assert total_nodes(result) == 0


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestGroupActionsByIteration:
    def test_groups_correctly(self):
        actions = [
            {"iteration": 1, "id": "a"},
            {"iteration": 1, "id": "b"},
            {"iteration": 2, "id": "c"},
        ]
        grouped = _group_actions_by_iteration(actions)
        assert len(grouped[1]) == 2
        assert len(grouped[2]) == 1

    def test_empty_actions(self):
        assert _group_actions_by_iteration([]) == {}

    def test_missing_iteration_key(self):
        actions = [{"id": "a"}]
        grouped = _group_actions_by_iteration(actions)
        assert 0 in grouped


class TestBuildNode:
    def test_basic_node(self):
        action = {
            "id": "act-1",
            "action_type": "llm_query",
            "phase": "triage",
            "iteration": 1,
            "input": {"description": "Classify"},
            "output": {"success": True},
            "duration_ms": 500,
            "llm_context": {"model": "mock", "tokens_in": 1000, "tokens_out": 200},
            "provenance": {},
            "timestamp": "2026-03-25T10:00:00+00:00",
        }
        node = _build_node(action)
        assert node.id == "act-1"
        assert node.action_type == "llm_query"
        assert node.phase == "triage"
        assert node.iteration == 1
        assert node.status == "success"
        assert node.tokens == 1200
        assert node.duration_ms == 500

    def test_failed_node(self):
        action = {
            "id": "f1",
            "action_type": "tool_execution",
            "input": {"description": "Run tests"},
            "output": {"success": False},
            "llm_context": {},
        }
        node = _build_node(action)
        assert node.status == "failure"

    def test_missing_fields(self):
        node = _build_node({})
        assert node.id == "unknown"
        assert node.action_type == "unknown"
        assert node.tokens == 0

    def test_description_truncation(self):
        action = {
            "id": "t1",
            "input": {"description": "A" * 200},
            "output": {"success": True},
            "llm_context": {},
        }
        node = _build_node(action)
        assert len(node.description) <= 120
        assert node.description.endswith("...")

    def test_metadata_contains_full_description(self):
        action = {
            "id": "t1",
            "input": {"description": "A" * 200},
            "output": {"success": True},
            "llm_context": {},
        }
        node = _build_node(action)
        assert len(node.metadata["full_description"]) == 200

    def test_none_tokens_treated_as_zero(self):
        action = {
            "id": "t1",
            "input": {},
            "output": {"success": True},
            "llm_context": {"tokens_in": None, "tokens_out": None},
        }
        node = _build_node(action)
        assert node.tokens == 0


class TestBuildLayers:
    def test_builds_from_iterations(self):
        iterations = _default_iterations()
        actions_by_iter = _group_actions_by_iteration(_default_actions())
        layers = _build_layers(iterations, actions_by_iter)
        assert len(layers) == 2
        assert layers[0].phase == "triage"
        assert layers[0].iteration == 1
        assert layers[0].successful is True
        assert len(layers[0].nodes) == 2

    def test_empty_iterations(self):
        layers = _build_layers([], {})
        assert layers == []

    def test_iteration_without_actions(self):
        iterations = [
            {
                "number": 1,
                "phase": "triage",
                "result": {"success": True},
            }
        ]
        layers = _build_layers(iterations, {})
        assert len(layers) == 1
        assert len(layers[0].nodes) == 0


class TestBuildEdges:
    def test_sequential_within_layer(self):
        n1 = ActionMapNode(id="a1")
        n2 = ActionMapNode(id="a2")
        n3 = ActionMapNode(id="a3")
        layers = [ActionMapLayer(phase="triage", nodes=[n1, n2, n3])]
        edges = _build_edges(layers)
        sequential = [e for e in edges if e.edge_type == "sequential"]
        assert len(sequential) == 2
        assert sequential[0].source == "a1"
        assert sequential[0].target == "a2"
        assert sequential[1].source == "a2"
        assert sequential[1].target == "a3"

    def test_phase_transition(self):
        n1 = ActionMapNode(id="a1")
        n2 = ActionMapNode(id="b1")
        layers = [
            ActionMapLayer(phase="triage", nodes=[n1]),
            ActionMapLayer(phase="implement", nodes=[n2]),
        ]
        edges = _build_edges(layers)
        transitions = [e for e in edges if e.edge_type == "phase_transition"]
        assert len(transitions) == 1
        assert transitions[0].source == "a1"
        assert transitions[0].target == "b1"

    def test_no_transition_for_empty_layers(self):
        layers = [
            ActionMapLayer(phase="triage", nodes=[]),
            ActionMapLayer(phase="implement", nodes=[ActionMapNode(id="b1")]),
        ]
        edges = _build_edges(layers)
        transitions = [e for e in edges if e.edge_type == "phase_transition"]
        assert len(transitions) == 0

    def test_single_node_no_sequential(self):
        layers = [ActionMapLayer(phase="triage", nodes=[ActionMapNode(id="a1")])]
        edges = _build_edges(layers)
        sequential = [e for e in edges if e.edge_type == "sequential"]
        assert len(sequential) == 0


class TestInferFileFlowEdges:
    def test_matching_read_write_paths(self):
        read_node = ActionMapNode(
            id="r1",
            action_type="tool_execution",
            description="Read file controller.py",
            metadata={"input": {"path": "/tmp/controller.py"}},
        )
        write_node = ActionMapNode(
            id="w1",
            action_type="tool_execution",
            description="Write fix to controller.py",
            metadata={"input": {"path": "/tmp/controller.py"}},
        )
        layers = [
            ActionMapLayer(phase="triage", nodes=[read_node]),
            ActionMapLayer(phase="implement", nodes=[write_node]),
        ]
        edges = _infer_file_flow_edges(layers)
        assert len(edges) == 1
        assert edges[0].source == "r1"
        assert edges[0].target == "w1"
        assert edges[0].edge_type == "data_flow"

    def test_no_match_different_paths(self):
        read_node = ActionMapNode(
            id="r1",
            action_type="tool_execution",
            description="Read file a.py",
            metadata={"input": {"path": "/tmp/a.py"}},
        )
        write_node = ActionMapNode(
            id="w1",
            action_type="tool_execution",
            description="Write fix to b.py",
            metadata={"input": {"path": "/tmp/b.py"}},
        )
        layers = [
            ActionMapLayer(phase="triage", nodes=[read_node]),
            ActionMapLayer(phase="implement", nodes=[write_node]),
        ]
        edges = _infer_file_flow_edges(layers)
        assert len(edges) == 0

    def test_no_path_in_input(self):
        node = ActionMapNode(
            id="r1",
            action_type="tool_execution",
            description="Read something",
            metadata={"input": {}},
        )
        layers = [ActionMapLayer(phase="triage", nodes=[node])]
        edges = _infer_file_flow_edges(layers)
        assert len(edges) == 0

    def test_deduplicates_edges(self):
        read1 = ActionMapNode(
            id="r1",
            action_type="tool_execution",
            description="Read file x.py",
            metadata={"input": {"path": "/tmp/x.py"}},
        )
        read2 = ActionMapNode(
            id="r1",
            action_type="tool_execution",
            description="Read file x.py again",
            metadata={"input": {"path": "/tmp/x.py"}},
        )
        write1 = ActionMapNode(
            id="w1",
            action_type="tool_execution",
            description="Write to x.py",
            metadata={"input": {"path": "/tmp/x.py"}},
        )
        layers = [
            ActionMapLayer(phase="triage", nodes=[read1, read2]),
            ActionMapLayer(phase="implement", nodes=[write1]),
        ]
        edges = _infer_file_flow_edges(layers)
        seen_pairs = {(e.source, e.target) for e in edges}
        assert ("r1", "w1") in seen_pairs


class TestBuildSummary:
    def test_basic_summary(self):
        n1 = ActionMapNode(id="a1")
        n2 = ActionMapNode(id="a2")
        layers = [
            ActionMapLayer(phase="triage", nodes=[n1]),
            ActionMapLayer(phase="implement", nodes=[n2]),
        ]
        actions = [
            {
                "action_type": "llm_query",
                "duration_ms": 500,
                "llm_context": {"tokens_in": 1000, "tokens_out": 200},
            },
            {
                "action_type": "tool_execution",
                "duration_ms": 100,
                "llm_context": {},
            },
        ]
        summary = _build_summary(layers, actions)
        assert summary["total_actions"] == 2
        assert summary["total_layers"] == 2
        assert summary["phases"] == ["triage", "implement"]
        assert summary["total_tokens"] == 1200
        assert summary["total_duration_ms"] == 600.0
        assert summary["action_type_counts"]["llm_query"] == 1
        assert summary["action_type_counts"]["tool_execution"] == 1

    def test_empty_summary(self):
        summary = _build_summary([], [])
        assert summary["total_actions"] == 0
        assert summary["total_layers"] == 0
        assert summary["phases"] == []

    def test_deduplicates_phases(self):
        layers = [
            ActionMapLayer(phase="implement"),
            ActionMapLayer(phase="implement"),
        ]
        summary = _build_summary(layers, [])
        assert summary["phases"] == ["implement"]


class TestTruncate:
    def test_short_text(self):
        assert _truncate("hello", 10) == "hello"

    def test_exact_length(self):
        assert _truncate("hello", 5) == "hello"

    def test_long_text(self):
        result = _truncate("A" * 100, 50)
        assert len(result) == 50
        assert result.endswith("...")

    def test_empty_string(self):
        assert _truncate("", 10) == ""


# ---------------------------------------------------------------------------
# Integration with ReportGenerator
# ---------------------------------------------------------------------------


class TestReportGeneratorIntegration:
    def test_report_contains_action_map_section(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "Action Map" in html
        assert "action-map-container" in html

    def test_report_contains_map_data(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "renderActionMap" in html

    def test_report_data_has_action_map(self):
        from engine.visualization.report_generator import extract_report_data

        data = extract_report_data(_make_execution())
        assert data.action_map != {}
        assert "layers" in data.action_map
        assert "edges" in data.action_map
        assert "summary" in data.action_map

    def test_report_data_to_dict_includes_map(self):
        from engine.visualization.report_generator import extract_report_data

        data = extract_report_data(_make_execution())
        d = data.to_dict()
        assert "action_map" in d
        assert len(d["action_map"]["layers"]) == 2

    def test_empty_execution_map_in_report(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate({})
        assert "action-map-container" in html

    def test_action_map_js_included(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "renderActionMap" in html
        assert "action-map-arrow" in html
