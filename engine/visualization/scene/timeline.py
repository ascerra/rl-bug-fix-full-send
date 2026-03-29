"""Timeline data builder — generates timeline scrubber data for the 3D report.

Transforms execution records into a timeline structure with:
- **Markers**: colored segments for each pipeline phase (start/end offsets)
- **Events**: individual action events with timestamps for chronological playback
- **Duration**: total execution wall-clock time

The JavaScript timeline component (``timeline.js``) reads this structure to
render a horizontal timeline bar with play/pause, drag-scrub, and phase markers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

PHASE_COLORS: dict[str, str] = {
    "triage": "#58a6ff",
    "implement": "#3fb950",
    "review": "#d29922",
    "validate": "#bc8cff",
    "report": "#56d4dd",
}

_FALLBACK_COLOR = "#6b7280"


@dataclass
class TimelineMarker:
    """A colored segment on the timeline representing a pipeline phase."""

    phase: str = ""
    start_ms: float = 0.0
    end_ms: float = 0.0
    color: str = _FALLBACK_COLOR
    label: str = ""
    status: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "start_ms": round(self.start_ms, 2),
            "end_ms": round(self.end_ms, 2),
            "color": self.color,
            "label": self.label,
            "status": self.status,
        }


@dataclass
class TimelineEvent:
    """A single action event positioned on the timeline."""

    id: str = ""
    timestamp_ms: float = 0.0
    phase: str = ""
    action_type: str = "unknown"
    label: str = ""
    status: str = "unknown"
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timestamp_ms": round(self.timestamp_ms, 2),
            "phase": self.phase,
            "action_type": self.action_type,
            "label": self.label,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class TimelineData:
    """Complete timeline data for the scrubber component."""

    total_duration_ms: float = 0.0
    start_time: str = ""
    markers: list[TimelineMarker] = field(default_factory=list)
    events: list[TimelineEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_duration_ms": round(self.total_duration_ms, 2),
            "start_time": self.start_time,
            "markers": [m.to_dict() for m in self.markers],
            "events": [e.to_dict() for e in self.events],
        }


def build_timeline(execution: dict[str, Any]) -> TimelineData:
    """Build timeline data from an execution record.

    Accepts the full ``execution.json`` structure (with top-level ``"execution"``
    key) or a flat execution dict.
    """
    exec_data = execution.get("execution", execution)
    iterations = exec_data.get("iterations", [])
    actions = exec_data.get("actions", [])

    if not iterations and not actions:
        return TimelineData()

    exec_start = _parse_timestamp(exec_data.get("started_at", ""))
    if exec_start is None:
        exec_start = _find_earliest_timestamp(iterations, actions)
    if exec_start is None:
        return TimelineData()

    exec_end = _parse_timestamp(exec_data.get("completed_at", ""))
    if exec_end is None:
        exec_end = _find_latest_timestamp(iterations, actions) or exec_start

    total_duration_ms = _ms_between(exec_start, exec_end)
    if total_duration_ms <= 0:
        total_duration_ms = _estimate_duration_from_actions(actions)

    markers = _build_markers(iterations, exec_start, total_duration_ms)
    events = _build_events(actions, exec_start)

    return TimelineData(
        total_duration_ms=total_duration_ms,
        start_time=exec_start.isoformat(),
        markers=markers,
        events=events,
    )


# ── Helpers ───────────────────────────────────────────────────────────────


def _parse_timestamp(ts: str) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning None on failure."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _ms_between(start: datetime, end: datetime) -> float:
    """Milliseconds between two datetimes."""
    return (end - start).total_seconds() * 1000


def _find_earliest_timestamp(
    iterations: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> datetime | None:
    """Find the earliest timestamp across iterations and actions."""
    candidates: list[datetime] = []
    for it in iterations:
        dt = _parse_timestamp(it.get("started_at", ""))
        if dt:
            candidates.append(dt)
    for action in actions:
        dt = _parse_timestamp(action.get("timestamp", ""))
        if dt:
            candidates.append(dt)
    return min(candidates) if candidates else None


def _find_latest_timestamp(
    iterations: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> datetime | None:
    """Find the latest timestamp across iterations and actions."""
    candidates: list[datetime] = []
    for it in iterations:
        dt = _parse_timestamp(it.get("completed_at", ""))
        if dt:
            candidates.append(dt)
        dt = _parse_timestamp(it.get("started_at", ""))
        if dt:
            dur = it.get("duration_ms", 0)
            if dur:
                from datetime import timedelta

                candidates.append(dt + timedelta(milliseconds=dur))
    for action in actions:
        dt = _parse_timestamp(action.get("timestamp", ""))
        if dt:
            dur = action.get("duration_ms", 0)
            if dur:
                from datetime import timedelta

                candidates.append(dt + timedelta(milliseconds=dur))
    return max(candidates) if candidates else None


def _estimate_duration_from_actions(actions: list[dict[str, Any]]) -> float:
    """Sum action durations as a fallback total duration estimate."""
    return sum(a.get("duration_ms", 0) for a in actions)


def _build_markers(
    iterations: list[dict[str, Any]],
    exec_start: datetime,
    total_duration_ms: float,
) -> list[TimelineMarker]:
    """Build phase markers from iterations, merging consecutive same-phase iterations."""
    if not iterations:
        return []

    phase_spans: list[dict[str, Any]] = []
    current_phase: str | None = None
    current_start: datetime | None = None
    current_end: datetime | None = None
    current_status: str = "unknown"

    for it in iterations:
        phase = it.get("phase", "unknown")
        it_start = _parse_timestamp(it.get("started_at", ""))
        it_end = _parse_timestamp(it.get("completed_at", ""))
        if it_start is None:
            continue

        if it_end is None and it.get("duration_ms"):
            from datetime import timedelta

            it_end = it_start + timedelta(milliseconds=it.get("duration_ms", 0))

        result = it.get("result", {})
        if result.get("escalate"):
            status = "escalated"
        elif result.get("success"):
            status = "success"
        elif result.get("should_continue"):
            status = "retry"
        else:
            status = "failure"

        if phase == current_phase and current_end is not None:
            current_end = it_end or current_end
            if status != "success":
                current_status = status
        else:
            if current_phase is not None and current_start is not None:
                phase_spans.append(
                    {
                        "phase": current_phase,
                        "start": current_start,
                        "end": current_end or current_start,
                        "status": current_status,
                    }
                )
            current_phase = phase
            current_start = it_start
            current_end = it_end or it_start
            current_status = status

    if current_phase is not None and current_start is not None:
        phase_spans.append(
            {
                "phase": current_phase,
                "start": current_start,
                "end": current_end or current_start,
                "status": current_status,
            }
        )

    markers = []
    for span in phase_spans:
        start_ms = _ms_between(exec_start, span["start"])
        end_ms = _ms_between(exec_start, span["end"])
        start_ms = max(0.0, min(start_ms, total_duration_ms))
        end_ms = max(start_ms, min(end_ms, total_duration_ms))

        markers.append(
            TimelineMarker(
                phase=span["phase"],
                start_ms=start_ms,
                end_ms=end_ms,
                color=PHASE_COLORS.get(span["phase"], _FALLBACK_COLOR),
                label=span["phase"].capitalize(),
                status=span["status"],
            )
        )

    return markers


def _build_events(
    actions: list[dict[str, Any]],
    exec_start: datetime,
) -> list[TimelineEvent]:
    """Build timeline events from action records."""
    events = []
    for action in actions:
        ts = _parse_timestamp(action.get("timestamp", ""))
        if ts is None:
            continue

        output = action.get("output", {})
        if output.get("success"):
            status = "success"
        elif output.get("escalate"):
            status = "escalated"
        else:
            status = "failure"

        description = action.get("input", {}).get("description", "No description")
        events.append(
            TimelineEvent(
                id=action.get("id", ""),
                timestamp_ms=max(0.0, _ms_between(exec_start, ts)),
                phase=action.get("phase", "unknown"),
                action_type=action.get("action_type", "unknown"),
                label=_truncate(description, 80),
                status=status,
                duration_ms=action.get("duration_ms", 0.0),
            )
        )

    events.sort(key=lambda e: e.timestamp_ms)
    return events


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
