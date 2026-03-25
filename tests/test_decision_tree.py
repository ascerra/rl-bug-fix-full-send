"""Tests for engine.visualization.decision_tree."""

from __future__ import annotations

from engine.visualization.decision_tree import (
    TreeNode,
    _action_label,
    _build_action_node,
    _build_outcome_node,
    _build_phase_node,
    _default_next,
    _group_actions_by_iteration,
    _phase_label,
    _resolve_status,
    _safe_target,
    build_decision_tree,
    node_count,
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
            "iteration": 2,
            "phase": "implement",
            "action_type": "tool_execution",
            "timestamp": "2026-03-25T10:01:30+00:00",
            "input": {"description": "Write fix to controller.py", "context": {}},
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
                "total_llm_calls": 4,
                "total_tokens_in": 5000,
                "total_tokens_out": 1500,
                "total_tool_executions": 8,
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
# TreeNode tests
# ---------------------------------------------------------------------------


class TestTreeNode:
    def test_default_values(self):
        node = TreeNode()
        assert node.id == ""
        assert node.label == ""
        assert node.node_type == "default"
        assert node.status == "unknown"
        assert node.metadata == {}
        assert node.children == []

    def test_to_dict_basic(self):
        node = TreeNode(id="n1", label="Test", node_type="phase", status="success")
        d = node.to_dict()
        assert d["id"] == "n1"
        assert d["label"] == "Test"
        assert d["type"] == "phase"
        assert d["status"] == "success"
        assert d["meta"] == {}
        assert d["children"] == []

    def test_to_dict_with_children(self):
        child = TreeNode(id="c1", label="Child")
        parent = TreeNode(id="p1", label="Parent", children=[child])
        d = parent.to_dict()
        assert len(d["children"]) == 1
        assert d["children"][0]["id"] == "c1"

    def test_to_dict_nested(self):
        grandchild = TreeNode(id="gc")
        child = TreeNode(id="c", children=[grandchild])
        root = TreeNode(id="r", children=[child])
        d = root.to_dict()
        assert d["children"][0]["children"][0]["id"] == "gc"

    def test_to_dict_metadata(self):
        node = TreeNode(id="n", metadata={"key": "value", "count": 42})
        d = node.to_dict()
        assert d["meta"]["key"] == "value"
        assert d["meta"]["count"] == 42


# ---------------------------------------------------------------------------
# build_decision_tree tests
# ---------------------------------------------------------------------------


class TestBuildDecisionTree:
    def test_basic_tree_structure(self):
        tree = build_decision_tree(_make_execution())
        assert tree.id == "root"
        assert tree.node_type == "root"
        assert tree.status == "success"
        assert tree.label == "Ralph Loop"

    def test_tree_from_wrapped_execution(self):
        tree = build_decision_tree(_make_execution())
        assert tree.metadata["execution_id"] == "test-exec-id-12345"

    def test_tree_from_flat_execution(self):
        tree = build_decision_tree(_make_flat_execution())
        assert tree.id == "root"
        assert tree.metadata["execution_id"] == "test-exec-id-12345"

    def test_phase_nodes_as_children(self):
        tree = build_decision_tree(_make_execution())
        phase_children = [c for c in tree.children if c.node_type == "phase"]
        assert len(phase_children) == 2
        assert phase_children[0].metadata["phase"] == "triage"
        assert phase_children[1].metadata["phase"] == "implement"

    def test_outcome_node_present(self):
        tree = build_decision_tree(_make_execution())
        outcome = [c for c in tree.children if c.node_type == "outcome"]
        assert len(outcome) == 1
        assert outcome[0].label == "COMPLETE"
        assert outcome[0].status == "success"

    def test_outcome_failure(self):
        tree = build_decision_tree(_make_execution(status="failure"))
        outcome = [c for c in tree.children if c.node_type == "outcome"]
        assert outcome[0].label == "FAILED"
        assert outcome[0].status == "failure"

    def test_outcome_escalated(self):
        tree = build_decision_tree(_make_execution(status="escalated"))
        outcome = [c for c in tree.children if c.node_type == "outcome"]
        assert outcome[0].label == "ESCALATED"

    def test_outcome_timeout(self):
        tree = build_decision_tree(_make_execution(status="timeout"))
        outcome = [c for c in tree.children if c.node_type == "outcome"]
        assert outcome[0].label == "TIMEOUT"

    def test_action_nodes_under_phases(self):
        tree = build_decision_tree(_make_execution())
        triage_node = tree.children[0]
        assert triage_node.metadata["phase"] == "triage"
        action_children = [c for c in triage_node.children if c.node_type == "action"]
        assert len(action_children) == 1
        assert "Classify issue" in action_children[0].label

    def test_empty_execution(self):
        tree = build_decision_tree({})
        assert tree.id == "root"
        assert tree.status == "unknown"
        assert len(tree.children) == 1
        assert tree.children[0].node_type == "outcome"
        assert tree.children[0].label == "No iterations executed"

    def test_empty_iterations(self):
        tree = build_decision_tree(_make_execution(iterations=[]))
        assert len(tree.children) == 1
        assert tree.children[0].label == "No iterations executed"

    def test_no_actions(self):
        tree = build_decision_tree(_make_execution(actions=[]))
        triage_node = tree.children[0]
        action_children = [c for c in triage_node.children if c.node_type == "action"]
        assert len(action_children) == 0

    def test_escalated_iteration(self):
        iterations = [
            {
                "number": 1,
                "phase": "triage",
                "started_at": "",
                "completed_at": "",
                "duration_ms": 100,
                "result": {
                    "success": False,
                    "should_continue": False,
                    "next_phase": "",
                    "escalate": True,
                },
            }
        ]
        tree = build_decision_tree(_make_execution(iterations=iterations, actions=[]))
        phase_node = tree.children[0]
        assert phase_node.status == "escalated"

    def test_retry_iteration(self):
        iterations = [
            {
                "number": 1,
                "phase": "implement",
                "started_at": "",
                "completed_at": "",
                "duration_ms": 100,
                "result": {
                    "success": False,
                    "should_continue": True,
                    "next_phase": "",
                    "escalate": False,
                },
            }
        ]
        tree = build_decision_tree(
            _make_execution(iterations=iterations, actions=[], status="failure")
        )
        phase_node = tree.children[0]
        assert phase_node.status == "retry"

    def test_failed_iteration(self):
        iterations = [
            {
                "number": 1,
                "phase": "validate",
                "started_at": "",
                "completed_at": "",
                "duration_ms": 100,
                "result": {
                    "success": False,
                    "should_continue": False,
                    "next_phase": "",
                    "escalate": False,
                },
            }
        ]
        tree = build_decision_tree(
            _make_execution(iterations=iterations, actions=[], status="failure")
        )
        phase_node = tree.children[0]
        assert phase_node.status == "failure"

    def test_backtrack_review_to_implement(self):
        iterations = [
            {
                "number": 1,
                "phase": "triage",
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
                "result": {
                    "success": False,
                    "should_continue": True,
                    "next_phase": "implement",
                    "escalate": False,
                },
            },
            {
                "number": 4,
                "phase": "implement",
                "result": {
                    "success": True,
                    "should_continue": True,
                    "next_phase": "review",
                    "escalate": False,
                },
            },
        ]
        tree = build_decision_tree(
            _make_execution(iterations=iterations, actions=[], status="success")
        )
        phase_nodes = [c for c in tree.children if c.node_type == "phase"]
        assert len(phase_nodes) == 4
        assert phase_nodes[2].status == "retry"
        assert phase_nodes[2].metadata["phase"] == "review"
        assert phase_nodes[2].metadata["next_phase"] == "implement"

    def test_to_dict_roundtrip(self):
        tree = build_decision_tree(_make_execution())
        d = tree.to_dict()
        assert d["id"] == "root"
        assert d["type"] == "root"
        assert isinstance(d["children"], list)
        assert len(d["children"]) == 3

    def test_total_tokens_in_root_metadata(self):
        tree = build_decision_tree(_make_execution())
        assert tree.metadata["total_tokens"] == 6500

    def test_trigger_in_root_metadata(self):
        tree = build_decision_tree(_make_execution())
        assert tree.metadata["trigger"]["source_url"] == "https://github.com/o/r/issues/1"

    def test_workflow_stripped_from_target(self):
        exec_data = _make_execution()
        exec_data["execution"]["target"]["workflow"] = {"run_id": 123, "lots": "of data"}
        tree = build_decision_tree(exec_data)
        assert "workflow" not in tree.metadata["target"]

    def test_single_iteration_tree(self):
        iterations = [
            {
                "number": 1,
                "phase": "triage",
                "result": {
                    "success": True,
                    "should_continue": False,
                    "next_phase": "",
                    "escalate": False,
                },
            }
        ]
        tree = build_decision_tree(_make_execution(iterations=iterations, actions=[]))
        assert len(tree.children) == 2
        assert tree.children[0].node_type == "phase"
        assert tree.children[1].node_type == "outcome"


# ---------------------------------------------------------------------------
# node_count tests
# ---------------------------------------------------------------------------


class TestNodeCount:
    def test_single_node(self):
        assert node_count(TreeNode()) == 1

    def test_with_children(self):
        tree = build_decision_tree(_make_execution())
        count = node_count(tree)
        assert count >= 4

    def test_empty_execution_count(self):
        tree = build_decision_tree({})
        assert node_count(tree) == 2

    def test_manual_tree(self):
        root = TreeNode(
            children=[
                TreeNode(children=[TreeNode()]),
                TreeNode(),
            ]
        )
        assert node_count(root) == 4


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestResolveStatus:
    def test_escalated(self):
        assert _resolve_status(False, True, False) == "escalated"

    def test_escalated_takes_priority(self):
        assert _resolve_status(True, True, True) == "escalated"

    def test_success(self):
        assert _resolve_status(True, False, False) == "success"

    def test_retry(self):
        assert _resolve_status(False, False, True) == "retry"

    def test_failure(self):
        assert _resolve_status(False, False, False) == "failure"


class TestPhaseLabel:
    def test_basic_label(self):
        label = _phase_label("triage", 1, "success", "implement")
        assert label == "triage #1"

    def test_escalated_suffix(self):
        label = _phase_label("triage", 1, "escalated", "")
        assert "[ESCALATED]" in label

    def test_retry_suffix(self):
        label = _phase_label("implement", 3, "retry", "")
        assert "[retry]" in label

    def test_failure_suffix(self):
        label = _phase_label("validate", 5, "failure", "")
        assert "[FAILED]" in label

    def test_backtrack_suffix(self):
        label = _phase_label("review", 3, "success", "implement")
        assert "-> implement" in label

    def test_normal_transition_no_suffix(self):
        label = _phase_label("triage", 1, "success", "implement")
        assert "->" not in label


class TestDefaultNext:
    def test_triage_to_implement(self):
        assert _default_next("triage") == "implement"

    def test_implement_to_review(self):
        assert _default_next("implement") == "review"

    def test_review_to_validate(self):
        assert _default_next("review") == "validate"

    def test_validate_to_report(self):
        assert _default_next("validate") == "report"

    def test_report_is_last(self):
        assert _default_next("report") == ""

    def test_unknown_phase(self):
        assert _default_next("nonexistent") == ""


class TestActionLabel:
    def test_llm_query(self):
        label = _action_label("llm_query", "Classify the issue")
        assert label == "[LLM] Classify the issue"

    def test_tool_execution(self):
        label = _action_label("tool_execution", "Read file")
        assert label == "[Tool] Read file"

    def test_escalation(self):
        label = _action_label("escalation", "Escalated: iteration cap")
        assert label == "[ESC] Escalated: iteration cap"

    def test_unknown_type(self):
        label = _action_label("custom_action", "Do something")
        assert label == "[custom_action] Do something"

    def test_truncation(self):
        long_desc = "A" * 100
        label = _action_label("llm_query", long_desc)
        assert len(label) < 100
        assert label.endswith("...")


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


class TestBuildPhaseNode:
    def test_basic_phase_node(self):
        iteration = {
            "number": 1,
            "phase": "triage",
            "started_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T00:01:00",
            "duration_ms": 1000,
            "result": {
                "success": True,
                "should_continue": True,
                "next_phase": "implement",
                "escalate": False,
            },
        }
        node = _build_phase_node(iteration, {})
        assert node.id == "iter-1"
        assert node.node_type == "phase"
        assert node.status == "success"
        assert node.metadata["phase"] == "triage"
        assert node.metadata["iteration"] == 1

    def test_phase_node_with_actions(self):
        iteration = {
            "number": 1,
            "phase": "triage",
            "result": {"success": True, "should_continue": True, "escalate": False},
        }
        actions_by_iter = {
            1: [
                {
                    "id": "a1",
                    "action_type": "llm_query",
                    "input": {"description": "Test"},
                    "output": {"success": True},
                },
            ],
        }
        node = _build_phase_node(iteration, actions_by_iter)
        assert len(node.children) == 1
        assert node.children[0].node_type == "action"

    def test_phase_node_counts_actions(self):
        iteration = {
            "number": 1,
            "phase": "triage",
            "result": {"success": True, "should_continue": True, "escalate": False},
        }
        actions_by_iter = {
            1: [
                {"id": "a1", "action_type": "llm_query", "input": {}, "output": {}},
                {"id": "a2", "action_type": "tool_execution", "input": {}, "output": {}},
            ],
        }
        node = _build_phase_node(iteration, actions_by_iter)
        assert node.metadata["action_count"] == 2
        assert node.metadata["llm_call_count"] == 1
        assert node.metadata["tool_call_count"] == 1


class TestBuildActionNode:
    def test_successful_action(self):
        action = {
            "id": "act-1",
            "action_type": "llm_query",
            "input": {"description": "Classify"},
            "output": {"success": True, "data": {}},
            "duration_ms": 500,
            "llm_context": {"model": "mock"},
            "provenance": {},
        }
        node = _build_action_node(action)
        assert node.id == "action-act-1"
        assert node.node_type == "action"
        assert node.status == "success"
        assert "LLM" in node.label
        assert node.metadata["action_type"] == "llm_query"

    def test_failed_action(self):
        action = {
            "id": "act-2",
            "action_type": "tool_execution",
            "input": {"description": "Run tests"},
            "output": {"success": False, "data": {"error": "exit code 1"}},
        }
        node = _build_action_node(action)
        assert node.status == "failure"

    def test_missing_fields(self):
        node = _build_action_node({})
        assert node.node_type == "action"
        assert node.id == "action-unknown"


class TestBuildOutcomeNode:
    def test_success_outcome(self):
        node = _build_outcome_node("success", {}, {"total_iterations": 5}, [])
        assert node.label == "COMPLETE"
        assert node.status == "success"
        assert node.metadata["total_iterations"] == 5

    def test_failure_outcome(self):
        node = _build_outcome_node("failure", {}, {}, [])
        assert node.label == "FAILED"

    def test_unknown_status(self):
        node = _build_outcome_node("custom", {}, {}, [])
        assert node.label == "CUSTOM"


class TestSafeTarget:
    def test_strips_workflow(self):
        target = {"repo_path": "/tmp", "workflow": {"run_id": 123}}
        safe = _safe_target(target)
        assert "workflow" not in safe
        assert safe["repo_path"] == "/tmp"

    def test_no_workflow(self):
        target = {"repo_path": "/tmp"}
        safe = _safe_target(target)
        assert safe == {"repo_path": "/tmp"}

    def test_does_not_mutate_original(self):
        target = {"repo_path": "/tmp", "workflow": {"run_id": 123}}
        _safe_target(target)
        assert "workflow" in target


# ---------------------------------------------------------------------------
# Integration with ReportGenerator
# ---------------------------------------------------------------------------


class TestReportGeneratorIntegration:
    def test_report_contains_decision_tree_section(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "Decision Tree" in html
        assert "decision-tree-container" in html

    def test_report_contains_tree_data(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "renderDecisionTree" in html
        assert '"Ralph Loop"' in html

    def test_report_data_has_decision_tree(self):
        from engine.visualization.report_generator import extract_report_data

        data = extract_report_data(_make_execution())
        assert data.decision_tree != {}
        assert data.decision_tree["id"] == "root"
        assert data.decision_tree["type"] == "root"

    def test_report_data_to_dict_includes_tree(self):
        from engine.visualization.report_generator import extract_report_data

        data = extract_report_data(_make_execution())
        d = data.to_dict()
        assert "decision_tree" in d
        assert d["decision_tree"]["id"] == "root"

    def test_empty_execution_tree_in_report(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate({})
        assert "decision-tree-container" in html
        assert "No iterations executed" in html

    def test_d3_script_included(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "d3.v7.min.js" in html

    def test_decision_tree_js_included(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_make_execution())
        assert "renderDecisionTree" in html
        assert "d3.hierarchy" in html
