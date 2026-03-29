"""Tests for engine.visualization.scene.timeline — timeline scrubber data generation."""

from __future__ import annotations

from pathlib import Path

from engine.visualization.scene.timeline import (
    PHASE_COLORS,
    TimelineData,
    TimelineEvent,
    TimelineMarker,
    _build_events,
    _build_markers,
    _estimate_duration_from_actions,
    _find_earliest_timestamp,
    _find_latest_timestamp,
    _ms_between,
    _parse_timestamp,
    _truncate,
    build_timeline,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "visual-report"


def _default_iterations() -> list:
    return [
        {
            "number": 1,
            "phase": "triage",
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:01:00+00:00",
            "duration_ms": 60000.0,
            "result": {"success": True, "should_continue": True, "escalate": False},
        },
        {
            "number": 2,
            "phase": "implement",
            "started_at": "2026-03-25T10:01:00+00:00",
            "completed_at": "2026-03-25T10:03:00+00:00",
            "duration_ms": 120000.0,
            "result": {"success": True, "should_continue": True, "escalate": False},
        },
        {
            "number": 3,
            "phase": "review",
            "started_at": "2026-03-25T10:03:00+00:00",
            "completed_at": "2026-03-25T10:04:00+00:00",
            "duration_ms": 60000.0,
            "result": {"success": True, "should_continue": True, "escalate": False},
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
        },
        {
            "id": "act-4",
            "iteration": 2,
            "phase": "implement",
            "action_type": "file_write",
            "timestamp": "2026-03-25T10:01:30+00:00",
            "duration_ms": 50.0,
            "input": {"description": "Write fix to reconciler.go"},
            "output": {"success": True},
        },
        {
            "id": "act-5",
            "iteration": 3,
            "phase": "review",
            "action_type": "llm_query",
            "timestamp": "2026-03-25T10:03:05+00:00",
            "duration_ms": 1500.0,
            "input": {"description": "Review diff for correctness"},
            "output": {"success": True},
        },
    ]


def _full_execution() -> dict:
    return {
        "execution": {
            "id": "test-exec-001",
            "started_at": "2026-03-25T10:00:00+00:00",
            "completed_at": "2026-03-25T10:04:00+00:00",
            "iterations": _default_iterations(),
            "actions": _default_actions(),
            "metrics": {},
            "result": {"status": "success", "total_iterations": 3},
        }
    }


# ===========================================================================
# TimelineMarker dataclass
# ===========================================================================


class TestTimelineMarker:
    def test_to_dict_fields(self):
        m = TimelineMarker(
            phase="triage",
            start_ms=0,
            end_ms=5000,
            color="#58a6ff",
            label="Triage",
            status="success",
        )
        d = m.to_dict()
        assert d["phase"] == "triage"
        assert d["start_ms"] == 0
        assert d["end_ms"] == 5000
        assert d["color"] == "#58a6ff"
        assert d["label"] == "Triage"
        assert d["status"] == "success"

    def test_defaults(self):
        m = TimelineMarker()
        assert m.phase == ""
        assert m.start_ms == 0.0
        assert m.end_ms == 0.0
        assert m.status == "unknown"

    def test_to_dict_rounds_ms(self):
        m = TimelineMarker(start_ms=1.12345, end_ms=999.99999)
        d = m.to_dict()
        assert d["start_ms"] == 1.12
        assert d["end_ms"] == 1000.0


# ===========================================================================
# TimelineEvent dataclass
# ===========================================================================


class TestTimelineEvent:
    def test_to_dict_fields(self):
        e = TimelineEvent(
            id="act-1",
            timestamp_ms=5000,
            phase="triage",
            action_type="llm_query",
            label="Classify issue",
            status="success",
            duration_ms=800.0,
        )
        d = e.to_dict()
        assert d["id"] == "act-1"
        assert d["timestamp_ms"] == 5000
        assert d["phase"] == "triage"
        assert d["action_type"] == "llm_query"
        assert d["label"] == "Classify issue"
        assert d["status"] == "success"
        assert d["duration_ms"] == 800.0

    def test_defaults(self):
        e = TimelineEvent()
        assert e.id == ""
        assert e.timestamp_ms == 0.0
        assert e.action_type == "unknown"
        assert e.status == "unknown"

    def test_to_dict_rounds_ms(self):
        e = TimelineEvent(timestamp_ms=1234.567, duration_ms=99.999)
        d = e.to_dict()
        assert d["timestamp_ms"] == 1234.57
        assert d["duration_ms"] == 100.0


# ===========================================================================
# TimelineData dataclass
# ===========================================================================


class TestTimelineData:
    def test_to_dict_fields(self):
        td = TimelineData(
            total_duration_ms=240000,
            start_time="2026-03-25T10:00:00+00:00",
            markers=[TimelineMarker(phase="triage")],
            events=[TimelineEvent(id="act-1")],
        )
        d = td.to_dict()
        assert d["total_duration_ms"] == 240000
        assert d["start_time"] == "2026-03-25T10:00:00+00:00"
        assert len(d["markers"]) == 1
        assert len(d["events"]) == 1

    def test_defaults_empty(self):
        td = TimelineData()
        d = td.to_dict()
        assert d["total_duration_ms"] == 0
        assert d["start_time"] == ""
        assert d["markers"] == []
        assert d["events"] == []


# ===========================================================================
# _parse_timestamp
# ===========================================================================


class TestParseTimestamp:
    def test_valid_iso(self):
        dt = _parse_timestamp("2026-03-25T10:00:00+00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 3

    def test_naive_timestamp_gets_utc(self):
        dt = _parse_timestamp("2026-03-25T10:00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_empty_string(self):
        assert _parse_timestamp("") is None

    def test_none_input(self):
        assert _parse_timestamp(None) is None

    def test_invalid_string(self):
        assert _parse_timestamp("not-a-timestamp") is None


# ===========================================================================
# _ms_between
# ===========================================================================


class TestMsBetween:
    def test_basic(self):
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        end = _parse_timestamp("2026-03-25T10:01:00+00:00")
        assert _ms_between(start, end) == 60000.0

    def test_same_time(self):
        dt = _parse_timestamp("2026-03-25T10:00:00+00:00")
        assert _ms_between(dt, dt) == 0.0


# ===========================================================================
# _find_earliest_timestamp / _find_latest_timestamp
# ===========================================================================


class TestFindTimestamps:
    def test_earliest_from_iterations(self):
        iters = _default_iterations()
        actions = _default_actions()
        dt = _find_earliest_timestamp(iters, actions)
        assert dt is not None
        assert dt.hour == 10
        assert dt.minute == 0

    def test_earliest_empty(self):
        assert _find_earliest_timestamp([], []) is None

    def test_latest_from_iterations(self):
        iters = _default_iterations()
        actions = _default_actions()
        dt = _find_latest_timestamp(iters, actions)
        assert dt is not None
        assert dt.minute == 4 or dt.minute > 3

    def test_latest_empty(self):
        assert _find_latest_timestamp([], []) is None

    def test_latest_uses_duration_fallback(self):
        iters = [
            {
                "started_at": "2026-03-25T10:00:00+00:00",
                "duration_ms": 5000,
                "phase": "triage",
            }
        ]
        dt = _find_latest_timestamp(iters, [])
        assert dt is not None


# ===========================================================================
# _estimate_duration_from_actions
# ===========================================================================


class TestEstimateDuration:
    def test_sums_durations(self):
        actions = [{"duration_ms": 100}, {"duration_ms": 200}, {"duration_ms": 300}]
        assert _estimate_duration_from_actions(actions) == 600

    def test_empty(self):
        assert _estimate_duration_from_actions([]) == 0

    def test_missing_duration(self):
        actions = [{"duration_ms": 100}, {}]
        assert _estimate_duration_from_actions(actions) == 100


# ===========================================================================
# _build_markers
# ===========================================================================


class TestBuildMarkers:
    def test_three_phase_markers(self):
        iters = _default_iterations()
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 240000)
        assert len(markers) == 3
        assert markers[0].phase == "triage"
        assert markers[1].phase == "implement"
        assert markers[2].phase == "review"

    def test_marker_colors(self):
        iters = _default_iterations()
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 240000)
        assert markers[0].color == PHASE_COLORS["triage"]
        assert markers[1].color == PHASE_COLORS["implement"]
        assert markers[2].color == PHASE_COLORS["review"]

    def test_marker_labels(self):
        iters = _default_iterations()
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 240000)
        assert markers[0].label == "Triage"
        assert markers[1].label == "Implement"

    def test_marker_time_offsets(self):
        iters = _default_iterations()
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 240000)
        assert markers[0].start_ms == 0
        assert markers[0].end_ms == 60000  # 1 min
        assert markers[1].start_ms == 60000  # 1 min
        assert markers[1].end_ms == 180000  # 3 min

    def test_empty_iterations(self):
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers([], start, 240000)
        assert markers == []

    def test_merges_consecutive_same_phase(self):
        iters = [
            {
                "number": 1,
                "phase": "implement",
                "started_at": "2026-03-25T10:01:00+00:00",
                "completed_at": "2026-03-25T10:02:00+00:00",
                "result": {"success": False, "should_continue": True},
            },
            {
                "number": 2,
                "phase": "implement",
                "started_at": "2026-03-25T10:02:00+00:00",
                "completed_at": "2026-03-25T10:03:00+00:00",
                "result": {"success": True, "should_continue": True},
            },
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 240000)
        assert len(markers) == 1
        assert markers[0].phase == "implement"
        assert markers[0].end_ms == 180000

    def test_status_from_result(self):
        iters = [
            {
                "number": 1,
                "phase": "triage",
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:01:00+00:00",
                "result": {"success": False, "escalate": True},
            },
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 60000)
        assert markers[0].status == "escalated"

    def test_retry_status(self):
        iters = [
            {
                "number": 1,
                "phase": "implement",
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:01:00+00:00",
                "result": {"success": False, "should_continue": True, "escalate": False},
            },
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 60000)
        assert markers[0].status == "retry"

    def test_clamped_to_total_duration(self):
        iters = [
            {
                "number": 1,
                "phase": "triage",
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:10:00+00:00",
                "result": {"success": True},
            },
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 60000)
        assert markers[0].end_ms <= 60000

    def test_unknown_phase_color(self):
        iters = [
            {
                "number": 1,
                "phase": "custom_phase",
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:01:00+00:00",
                "result": {"success": True},
            },
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        markers = _build_markers(iters, start, 60000)
        assert markers[0].color == "#6b7280"


# ===========================================================================
# _build_events
# ===========================================================================


class TestBuildEvents:
    def test_five_events(self):
        actions = _default_actions()
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events(actions, start)
        assert len(events) == 5

    def test_sorted_by_timestamp(self):
        actions = _default_actions()
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events(actions, start)
        timestamps = [e.timestamp_ms for e in events]
        assert timestamps == sorted(timestamps)

    def test_event_fields(self):
        actions = _default_actions()
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events(actions, start)
        e0 = events[0]
        assert e0.id == "act-1"
        assert e0.phase == "triage"
        assert e0.action_type == "llm_query"
        assert e0.status == "success"
        assert e0.timestamp_ms == 10000  # 10 seconds offset

    def test_empty_actions(self):
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events([], start)
        assert events == []

    def test_skips_no_timestamp(self):
        actions = [{"id": "x", "action_type": "llm_query", "output": {"success": True}}]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events(actions, start)
        assert events == []

    def test_failure_status(self):
        actions = [
            {
                "id": "fail-1",
                "timestamp": "2026-03-25T10:00:10+00:00",
                "action_type": "shell_run",
                "input": {"description": "Test run"},
                "output": {"success": False},
            }
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events(actions, start)
        assert events[0].status == "failure"

    def test_escalation_status(self):
        actions = [
            {
                "id": "esc-1",
                "timestamp": "2026-03-25T10:00:10+00:00",
                "action_type": "escalation",
                "input": {"description": "Escalate to human"},
                "output": {"escalate": True},
            }
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events(actions, start)
        assert events[0].status == "escalated"

    def test_label_truncated(self):
        actions = [
            {
                "id": "long-1",
                "timestamp": "2026-03-25T10:00:10+00:00",
                "action_type": "llm_query",
                "input": {"description": "A" * 120},
                "output": {"success": True},
            }
        ]
        start = _parse_timestamp("2026-03-25T10:00:00+00:00")
        events = _build_events(actions, start)
        assert len(events[0].label) <= 80
        assert events[0].label.endswith("...")


# ===========================================================================
# build_timeline — full pipeline
# ===========================================================================


class TestBuildTimeline:
    def test_full_execution(self):
        tl = build_timeline(_full_execution())
        assert tl.total_duration_ms == 240000  # 4 minutes
        assert len(tl.markers) == 3
        assert len(tl.events) == 5
        assert tl.start_time != ""

    def test_to_dict_serializable(self):
        tl = build_timeline(_full_execution())
        d = tl.to_dict()
        assert "total_duration_ms" in d
        assert "markers" in d
        assert "events" in d
        assert "start_time" in d
        assert isinstance(d["markers"], list)
        assert isinstance(d["events"], list)

    def test_empty_execution(self):
        tl = build_timeline({"execution": {"iterations": [], "actions": []}})
        assert tl.total_duration_ms == 0
        assert tl.markers == []
        assert tl.events == []

    def test_flat_execution(self):
        flat = _full_execution()["execution"]
        tl = build_timeline(flat)
        assert tl.total_duration_ms > 0
        assert len(tl.markers) == 3

    def test_no_started_at_uses_earliest(self):
        exec_data = _full_execution()
        del exec_data["execution"]["started_at"]
        tl = build_timeline(exec_data)
        assert tl.total_duration_ms > 0

    def test_no_completed_at_uses_latest(self):
        exec_data = _full_execution()
        del exec_data["execution"]["completed_at"]
        tl = build_timeline(exec_data)
        assert tl.total_duration_ms > 0

    def test_marker_coverage(self):
        tl = build_timeline(_full_execution())
        for m in tl.markers:
            assert m.start_ms >= 0
            assert m.end_ms >= m.start_ms
            assert m.end_ms <= tl.total_duration_ms

    def test_event_order(self):
        tl = build_timeline(_full_execution())
        timestamps = [e.timestamp_ms for e in tl.events]
        assert timestamps == sorted(timestamps)

    def test_all_event_phases(self):
        tl = build_timeline(_full_execution())
        phases = {e.phase for e in tl.events}
        assert "triage" in phases
        assert "implement" in phases
        assert "review" in phases


# ===========================================================================
# _truncate helper
# ===========================================================================


class TestTruncate:
    def test_short_text(self):
        assert _truncate("hello", 80) == "hello"

    def test_long_text(self):
        result = _truncate("A" * 100, 80)
        assert len(result) == 80
        assert result.endswith("...")

    def test_exact_length(self):
        assert _truncate("A" * 80, 80) == "A" * 80


# ===========================================================================
# JavaScript structure (timeline.js)
# ===========================================================================


class TestTimelineJS:
    def test_js_file_exists(self):
        js_path = TEMPLATES_DIR / "timeline.js"
        assert js_path.exists(), "timeline.js should exist in visual-report templates"

    def test_js_has_iife_module(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "var RalphTimeline" in js
        assert "(function" in js

    def test_js_exports_timeline_class(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "Timeline:" in js

    def test_js_exports_speeds(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "SPEEDS:" in js

    def test_js_has_render_entry_point(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "function renderTimeline" in js

    def test_js_has_play_pause(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "togglePlay" in js
        assert "play" in js
        assert "pause" in js

    def test_js_has_seek_methods(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "seekTo" in js
        assert "seekToEvent" in js
        assert "seekToPhase" in js

    def test_js_has_dragging(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "_onThumbDown" in js
        assert "_isDragging" in js
        assert "mousedown" in js

    def test_js_has_dispose(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "dispose" in js

    def test_js_has_speed_cycle(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "cycleSpeed" in js
        assert "speedIndex" in js

    def test_js_format_duration(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "formatDuration" in js

    def test_js_event_highlight(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "_highlightVisibleEvents" in js

    def test_js_phase_markers(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "_renderMarkers" in js

    def test_js_no_raw_json(self):
        js = (TEMPLATES_DIR / "timeline.js").read_text()
        assert "JSON.parse" not in js


# ===========================================================================
# Report template integration
# ===========================================================================


class TestTemplateIntegration:
    def test_template_includes_timeline_js(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "timeline.js" in html

    def test_template_has_timeline_data_block(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "timeline_data" in html

    def test_template_has_render_timeline_call(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "renderTimeline" in html

    def test_template_timeline_instructions(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "timeline" in html.lower()

    def test_template_scene_click_seeks_timeline(self):
        html = (TEMPLATES_DIR / "report.html").read_text()
        assert "seekToEvent" in html


# ===========================================================================
# ReportData integration
# ===========================================================================


class TestReportDataIntegration:
    def test_report_data_has_timeline_field(self):
        from engine.visualization.report_generator import ReportData

        rd = ReportData()
        assert hasattr(rd, "timeline_data")
        assert rd.timeline_data == {}

    def test_to_dict_includes_timeline(self):
        from engine.visualization.report_generator import ReportData

        rd = ReportData(timeline_data={"total_duration_ms": 1000})
        d = rd.to_dict()
        assert "timeline_data" in d
        assert d["timeline_data"]["total_duration_ms"] == 1000

    def test_extract_report_data_builds_timeline(self):
        from engine.visualization.report_generator import extract_report_data

        rd = extract_report_data(_full_execution())
        assert rd.timeline_data is not None
        assert rd.timeline_data.get("total_duration_ms", 0) > 0
        assert len(rd.timeline_data.get("markers", [])) == 3
        assert len(rd.timeline_data.get("events", [])) == 5


# ===========================================================================
# Report generator end-to-end
# ===========================================================================


class TestReportGeneratorTimeline:
    def test_html_report_contains_timeline(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        html = gen.generate(_full_execution())
        assert "renderTimeline" in html
        assert "ralph-timeline" in html or "timeline" in html.lower()

    def test_html_report_no_timeline_on_empty_data(self):
        from engine.visualization.report_generator import ReportGenerator

        gen = ReportGenerator()
        empty_exec = {"execution": {"iterations": [], "actions": [], "result": {}, "metrics": {}}}
        html = gen.generate(empty_exec)
        assert "var timelineData" not in html


# ===========================================================================
# Package exports
# ===========================================================================


class TestPackageExports:
    def test_scene_package_exports(self):
        from engine.visualization.scene import (
            TimelineData,
            TimelineEvent,
            TimelineMarker,
            build_timeline,
        )

        assert TimelineData is not None
        assert TimelineEvent is not None
        assert TimelineMarker is not None
        assert build_timeline is not None

    def test_visualization_package_exports(self):
        from engine.visualization import TimelineData, TimelineEvent, TimelineMarker, build_timeline

        assert TimelineData is not None
        assert TimelineEvent is not None
        assert TimelineMarker is not None
        assert build_timeline is not None


# ===========================================================================
# Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_actions_without_iterations(self):
        exec_data = {
            "execution": {
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:01:00+00:00",
                "iterations": [],
                "actions": _default_actions(),
                "result": {},
                "metrics": {},
            }
        }
        tl = build_timeline(exec_data)
        assert tl.total_duration_ms == 60000
        assert len(tl.markers) == 0
        assert len(tl.events) == 5

    def test_iterations_without_actions(self):
        exec_data = {
            "execution": {
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:04:00+00:00",
                "iterations": _default_iterations(),
                "actions": [],
                "result": {},
                "metrics": {},
            }
        }
        tl = build_timeline(exec_data)
        assert tl.total_duration_ms == 240000
        assert len(tl.markers) == 3
        assert len(tl.events) == 0

    def test_single_iteration_single_action(self):
        exec_data = {
            "execution": {
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:00:01+00:00",
                "iterations": [
                    {
                        "number": 1,
                        "phase": "triage",
                        "started_at": "2026-03-25T10:00:00+00:00",
                        "completed_at": "2026-03-25T10:00:01+00:00",
                        "result": {"success": True},
                    }
                ],
                "actions": [
                    {
                        "id": "a1",
                        "timestamp": "2026-03-25T10:00:00.500+00:00",
                        "phase": "triage",
                        "action_type": "llm_query",
                        "duration_ms": 500,
                        "input": {"description": "Quick query"},
                        "output": {"success": True},
                    }
                ],
                "result": {},
                "metrics": {},
            }
        }
        tl = build_timeline(exec_data)
        assert tl.total_duration_ms == 1000
        assert len(tl.markers) == 1
        assert len(tl.events) == 1

    def test_no_timestamps_at_all(self):
        tl = build_timeline(
            {
                "execution": {
                    "iterations": [{"number": 1, "phase": "triage", "result": {"success": True}}],
                    "actions": [{"id": "a1", "output": {"success": True}}],
                    "result": {},
                    "metrics": {},
                }
            }
        )
        assert tl.total_duration_ms == 0
        assert tl.markers == []
        assert tl.events == []

    def test_duration_fallback_from_actions(self):
        exec_data = {
            "execution": {
                "started_at": "2026-03-25T10:00:00+00:00",
                "completed_at": "2026-03-25T10:00:00+00:00",
                "iterations": [],
                "actions": [
                    {
                        "id": "a1",
                        "timestamp": "2026-03-25T10:00:00+00:00",
                        "duration_ms": 5000,
                        "output": {"success": True},
                        "input": {"description": "x"},
                    }
                ],
                "result": {},
                "metrics": {},
            }
        }
        tl = build_timeline(exec_data)
        assert tl.total_duration_ms == 5000


# ===========================================================================
# PHASE_COLORS constants
# ===========================================================================


class TestPhaseColors:
    def test_known_phases_have_colors(self):
        for phase in ("triage", "implement", "review", "validate", "report"):
            assert phase in PHASE_COLORS, f"Missing color for phase {phase}"

    def test_colors_are_hex(self):
        for color in PHASE_COLORS.values():
            assert color.startswith("#")
            assert len(color) == 7
