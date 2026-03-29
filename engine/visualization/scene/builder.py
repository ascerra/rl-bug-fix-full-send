"""Scene builder — transforms execution records into Three.js scene graph data.

Produces a JSON-serializable scene graph describing:
- **Platforms**: one per pipeline phase at ascending Y elevations
- **Objects**: one per action, with geometry type encoding the action kind
- **Connections**: data-flow edges between causally linked objects

The frontend Three.js renderer (Phase 9.2) reads this structure to build
the interactive 3D scene.  This module is fully testable without a browser.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

# ── Geometry mapping (SPEC §6.1) ──────────────────────────────────────────
GEOMETRY_MAP: dict[str, str] = {
    "llm_query": "polyhedron",
    "file_read": "cube",
    "file_write": "cube",
    "file_search": "cube",
    "tool_execution": "cylinder",
    "shell_run": "cylinder",
    "command_run": "cylinder",
    "api_call": "sphere",
    "github_api": "sphere",
    "pr_create": "sphere",
    "comment_post": "sphere",
    "escalation": "sphere",
}

# ── Status color mapping (SPEC §6.1) ──────────────────────────────────────
STATUS_COLORS: dict[str, str] = {
    "success": "#22c55e",  # green
    "failure": "#ef4444",  # red
    "retry": "#f59e0b",  # amber
    "escalated": "#3b82f6",  # blue
    "unknown": "#6b7280",  # gray
}

# ── Connection data-type colors (SPEC §6.1) ───────────────────────────────
DATA_TYPE_COLORS: dict[str, str] = {
    "code": "#06b6d4",  # cyan
    "reasoning": "#eab308",  # gold
    "test_pass": "#22c55e",  # green
    "test_fail": "#ef4444",  # red
    "sequential": "#94a3b8",  # slate
    "phase_transition": "#a78bfa",  # violet
    "data_flow": "#06b6d4",  # cyan
}

# ── Phase elevation layout ────────────────────────────────────────────────
PHASE_ELEVATIONS: dict[str, float] = {
    "triage": 0.0,
    "implement": 4.0,
    "review": 8.0,
    "validate": 12.0,
    "report": 16.0,
}
_DEFAULT_ELEVATION_STEP = 4.0


@dataclass
class SceneObject:
    """A 3D object representing a single action on a phase platform."""

    id: str = ""
    action_type: str = "unknown"
    geometry: str = "cube"
    label: str = ""
    phase: str = ""
    iteration: int = 0
    status: str = "unknown"
    color: str = "#6b7280"
    position: dict[str, float] = field(default_factory=lambda: {"x": 0, "y": 0, "z": 0})
    scale: float = 1.0
    duration_ms: float = 0.0
    tokens: int = 0
    timestamp: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action_type": self.action_type,
            "geometry": self.geometry,
            "label": self.label,
            "phase": self.phase,
            "iteration": self.iteration,
            "status": self.status,
            "color": self.color,
            "position": self.position,
            "scale": self.scale,
            "duration_ms": self.duration_ms,
            "tokens": self.tokens,
            "timestamp": self.timestamp,
            "meta": self.metadata,
        }


@dataclass
class SceneConnection:
    """A directed connection between two scene objects."""

    source: str = ""
    target: str = ""
    connection_type: str = "sequential"
    color: str = "#94a3b8"
    animated: bool = False
    data_type: str = "sequential"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "type": self.connection_type,
            "color": self.color,
            "animated": self.animated,
            "data_type": self.data_type,
        }


@dataclass
class ScenePlatform:
    """A floating platform representing a pipeline phase."""

    phase: str = ""
    elevation: float = 0.0
    color: str = "#6b7280"
    status: str = "unknown"
    iteration_count: int = 0
    objects: list[SceneObject] = field(default_factory=list)
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "elevation": self.elevation,
            "color": self.color,
            "status": self.status,
            "iteration_count": self.iteration_count,
            "objects": [obj.to_dict() for obj in self.objects],
            "label": self.label,
        }


@dataclass
class SceneData:
    """Complete Three.js scene graph data."""

    platforms: list[ScenePlatform] = field(default_factory=list)
    connections: list[SceneConnection] = field(default_factory=list)
    bridges: list[dict[str, Any]] = field(default_factory=list)
    camera: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platforms": [p.to_dict() for p in self.platforms],
            "connections": [c.to_dict() for c in self.connections],
            "bridges": self.bridges,
            "camera": self.camera,
            "summary": self.summary,
        }

    def to_json(self, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


class SceneBuilder:
    """Transforms execution records into a Three.js scene graph.

    Usage::

        builder = SceneBuilder()
        scene = builder.build(execution_data)
        scene_json = scene.to_json()
    """

    def build(self, execution: dict[str, Any]) -> SceneData:
        """Build a complete scene graph from an execution record.

        Accepts the full execution.json structure (with top-level ``"execution"``
        key) or a flat execution dict.
        """
        exec_data = execution.get("execution", execution)

        iterations = exec_data.get("iterations", [])
        actions = exec_data.get("actions", [])
        metrics = exec_data.get("metrics", {})
        result = exec_data.get("result", {})

        if not iterations and not actions:
            return SceneData(
                camera=_default_camera(0),
                summary=_build_summary([], [], metrics, result),
            )

        actions_by_iter = _group_actions_by_iteration(actions)
        platforms = self._build_platforms(iterations, actions_by_iter)
        connections = self._build_connections(platforms)
        bridges = self._build_bridges(platforms)
        max_elevation = max((p.elevation for p in platforms), default=0)
        camera = _default_camera(max_elevation)
        summary = _build_summary(platforms, actions, metrics, result)

        return SceneData(
            platforms=platforms,
            connections=connections,
            bridges=bridges,
            camera=camera,
            summary=summary,
        )

    def _build_platforms(
        self,
        iterations: list[dict[str, Any]],
        actions_by_iter: dict[int, list[dict[str, Any]]],
    ) -> list[ScenePlatform]:
        """Build one platform per unique phase, merging iterations of the same phase."""
        phase_order: list[str] = []
        phase_iters: dict[str, list[dict[str, Any]]] = {}
        for it in iterations:
            phase = it.get("phase", "unknown")
            if phase not in phase_iters:
                phase_order.append(phase)
                phase_iters[phase] = []
            phase_iters[phase].append(it)

        platforms: list[ScenePlatform] = []

        for phase_idx, phase in enumerate(phase_order):
            iters = phase_iters[phase]
            elevation = PHASE_ELEVATIONS.get(phase)
            if elevation is None:
                elevation = phase_idx * _DEFAULT_ELEVATION_STEP

            all_objects: list[SceneObject] = []
            for it in iters:
                iter_num = it.get("number", 0)
                iter_actions = actions_by_iter.get(iter_num, [])
                for idx, action in enumerate(iter_actions):
                    obj = _build_scene_object(action, elevation, idx, len(iter_actions))
                    all_objects.append(obj)

            phase_status = _aggregate_phase_status(iters)
            platforms.append(
                ScenePlatform(
                    phase=phase,
                    elevation=elevation,
                    color=STATUS_COLORS.get(phase_status, STATUS_COLORS["unknown"]),
                    status=phase_status,
                    iteration_count=len(iters),
                    objects=all_objects,
                    label=phase.capitalize(),
                )
            )

        return platforms

    def _build_connections(
        self,
        platforms: list[ScenePlatform],
    ) -> list[SceneConnection]:
        """Build sequential, phase-transition, and data-flow connections."""
        connections: list[SceneConnection] = []

        for platform in platforms:
            objects = platform.objects
            for i in range(len(objects) - 1):
                data_type = _infer_data_type(objects[i], objects[i + 1])
                connections.append(
                    SceneConnection(
                        source=objects[i].id,
                        target=objects[i + 1].id,
                        connection_type="sequential",
                        color=DATA_TYPE_COLORS.get(data_type, DATA_TYPE_COLORS["sequential"]),
                        animated=False,
                        data_type=data_type,
                    )
                )

        for i in range(len(platforms) - 1):
            prev_objects = platforms[i].objects
            next_objects = platforms[i + 1].objects
            if prev_objects and next_objects:
                connections.append(
                    SceneConnection(
                        source=prev_objects[-1].id,
                        target=next_objects[0].id,
                        connection_type="phase_transition",
                        color=DATA_TYPE_COLORS["phase_transition"],
                        animated=True,
                        data_type="phase_transition",
                    )
                )

        file_connections = _infer_file_flow_connections(platforms)
        connections.extend(file_connections)

        return connections

    def _build_bridges(self, platforms: list[ScenePlatform]) -> list[dict[str, Any]]:
        """Build bridge path data connecting adjacent platforms."""
        bridges: list[dict[str, Any]] = []
        for i in range(len(platforms) - 1):
            bridges.append(
                {
                    "from_phase": platforms[i].phase,
                    "to_phase": platforms[i + 1].phase,
                    "from_elevation": platforms[i].elevation,
                    "to_elevation": platforms[i + 1].elevation,
                    "color": DATA_TYPE_COLORS["phase_transition"],
                }
            )
        return bridges

    def add_comparison_ghosts(
        self,
        scene: SceneData,
        comparison: dict[str, Any],
    ) -> None:
        """Overlay ghost objects for the human fix onto the scene.

        When a comparison is enabled and the human diff includes file changes,
        translucent ghost objects are placed on the ``implement`` platform
        (or the last platform if ``implement`` is absent).  The JS renderer
        recognises ``meta.ghost == True`` and renders them with reduced opacity.
        """
        if not comparison.get("enabled"):
            return

        human_summary = comparison.get("human_summary", {})
        human_files = human_summary.get("files", [])
        if not human_files:
            return

        target_platform: ScenePlatform | None = None
        for p in scene.platforms:
            if p.phase == "implement":
                target_platform = p
                break
        if target_platform is None and scene.platforms:
            target_platform = scene.platforms[-1]
        if target_platform is None:
            return

        existing_count = len(target_platform.objects)
        z_offset = 3.0

        for idx, fdata in enumerate(human_files):
            file_path = fdata.get("path", f"human-file-{idx}")
            lines_added = fdata.get("lines_added", 0)
            lines_removed = fdata.get("lines_removed", 0)

            spread = max(len(human_files) - 1, 1) * 2.0
            x = (idx - (len(human_files) - 1) / 2) * (spread / max(len(human_files) - 1, 1))

            ghost = SceneObject(
                id=f"ghost-human-{idx}",
                action_type="file_write",
                geometry="cube",
                label=f"Human fix: {file_path}",
                phase=target_platform.phase,
                iteration=0,
                status="success",
                color="#ffffff",
                position={"x": x, "y": target_platform.elevation, "z": z_offset},
                scale=0.8,
                duration_ms=0,
                tokens=0,
                timestamp="",
                metadata={
                    "ghost": True,
                    "comparison_source": "human",
                    "file_path": file_path,
                    "lines_added": lines_added,
                    "lines_removed": lines_removed,
                    "full_description": f"Human fix: {file_path} (+{lines_added}/-{lines_removed})",
                },
            )
            target_platform.objects.append(ghost)

        for i in range(len(human_files) - 1):
            scene.connections.append(
                SceneConnection(
                    source=f"ghost-human-{i}",
                    target=f"ghost-human-{i + 1}",
                    connection_type="sequential",
                    color="#ffffff",
                    animated=False,
                    data_type="code",
                )
            )

        if existing_count > 0 and human_files:
            agent_last = target_platform.objects[existing_count - 1]
            scene.connections.append(
                SceneConnection(
                    source=agent_last.id,
                    target="ghost-human-0",
                    connection_type="comparison",
                    color="#a78bfa",
                    animated=False,
                    data_type="code",
                )
            )


def build_scene(execution: dict[str, Any]) -> SceneData:
    """Module-level convenience: build a scene from an execution record."""
    return SceneBuilder().build(execution)


# ── Helpers ───────────────────────────────────────────────────────────────


def _group_actions_by_iteration(
    actions: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for action in actions:
        iter_num = action.get("iteration", 0)
        grouped.setdefault(iter_num, []).append(action)
    return grouped


def _build_scene_object(
    action: dict[str, Any],
    elevation: float,
    index: int,
    total_in_group: int,
) -> SceneObject:
    """Build a SceneObject from a raw action record."""
    action_type = action.get("action_type", "unknown")
    description = action.get("input", {}).get("description", "No description")
    output = action.get("output", {})
    llm_ctx = action.get("llm_context", {})

    tokens_in = llm_ctx.get("tokens_in", 0) or 0
    tokens_out = llm_ctx.get("tokens_out", 0) or 0
    total_tokens = tokens_in + tokens_out

    status = _resolve_action_status(output)
    geometry = GEOMETRY_MAP.get(action_type, "cube")

    spread = max(total_in_group - 1, 1) * 2.0
    x = (index - (total_in_group - 1) / 2) * (spread / max(total_in_group - 1, 1))
    z = 0.0

    scale = _token_scale(total_tokens)

    return SceneObject(
        id=action.get("id", f"obj-{index}"),
        action_type=action_type,
        geometry=geometry,
        label=_truncate(description, 80),
        phase=action.get("phase", "unknown"),
        iteration=action.get("iteration", 0),
        status=status,
        color=STATUS_COLORS.get(status, STATUS_COLORS["unknown"]),
        position={"x": x, "y": elevation, "z": z},
        scale=scale,
        duration_ms=action.get("duration_ms", 0.0),
        tokens=total_tokens,
        timestamp=action.get("timestamp", ""),
        metadata={
            "full_description": description,
            "input": action.get("input", {}),
            "output": output,
            "llm_context": llm_ctx,
            "provenance": action.get("provenance", {}),
        },
    )


def _resolve_action_status(output: dict[str, Any]) -> str:
    if output.get("success"):
        return "success"
    if output.get("escalate"):
        return "escalated"
    return "failure"


def _aggregate_phase_status(iterations: list[dict[str, Any]]) -> str:
    """Determine the overall status for a phase from its iterations."""
    statuses: list[str] = []
    for it in iterations:
        result = it.get("result", {})
        if result.get("escalate"):
            statuses.append("escalated")
        elif result.get("success"):
            statuses.append("success")
        elif result.get("should_continue"):
            statuses.append("retry")
        else:
            statuses.append("failure")

    if "escalated" in statuses:
        return "escalated"
    if any(s == "success" for s in statuses):
        return "success"
    if "retry" in statuses:
        return "retry"
    if statuses:
        return "failure"
    return "unknown"


def _token_scale(tokens: int) -> float:
    """Map token count to a visual scale factor (0.5 - 2.5)."""
    if tokens <= 0:
        return 0.5
    if tokens < 1000:
        return 0.7
    if tokens < 5000:
        return 1.0
    if tokens < 15000:
        return 1.5
    if tokens < 50000:
        return 2.0
    return 2.5


def _infer_data_type(source: SceneObject, target: SceneObject) -> str:
    """Infer the data-type color for a sequential connection."""
    if source.action_type == "llm_query":
        return "reasoning"
    if target.action_type == "llm_query":
        return "reasoning"
    if source.action_type in ("shell_run", "command_run", "tool_execution"):
        if source.status == "success":
            return "test_pass"
        return "test_fail"
    if source.action_type in ("file_read", "file_write", "file_search"):
        return "code"
    return "sequential"


def _infer_file_flow_connections(
    platforms: list[ScenePlatform],
) -> list[SceneConnection]:
    """Create data-flow connections between objects that share file paths."""
    write_registry: list[tuple[str, str]] = []
    read_registry: list[tuple[str, str]] = []

    for platform in platforms:
        for obj in platform.objects:
            path = obj.metadata.get("input", {}).get("path", "")
            if not path:
                continue
            if obj.action_type == "file_write":
                write_registry.append((obj.id, path))
            elif obj.action_type == "file_read" or "read" in obj.label.lower():
                read_registry.append((obj.id, path))
            elif "write" in obj.label.lower():
                write_registry.append((obj.id, path))

    connections: list[SceneConnection] = []
    seen: set[tuple[str, str]] = set()
    for write_id, write_path in write_registry:
        for read_id, read_path in read_registry:
            if write_id == read_id:
                continue
            if write_path == read_path and (write_id, read_id) not in seen:
                seen.add((write_id, read_id))
                connections.append(
                    SceneConnection(
                        source=write_id,
                        target=read_id,
                        connection_type="data_flow",
                        color=DATA_TYPE_COLORS["data_flow"],
                        animated=True,
                        data_type="code",
                    )
                )

    return connections


def _default_camera(max_elevation: float) -> dict[str, Any]:
    """Compute default camera position to frame the entire scene."""
    center_y = max_elevation / 2
    distance = max(max_elevation * 1.5, 20.0)
    return {
        "position": {"x": distance * 0.7, "y": center_y + distance * 0.3, "z": distance},
        "target": {"x": 0, "y": center_y, "z": 0},
        "fov": 60,
        "near": 0.1,
        "far": 1000,
        "presets": {
            "overview": {
                "position": {
                    "x": distance * 0.7,
                    "y": center_y + distance * 0.3,
                    "z": distance,
                },
                "target": {"x": 0, "y": center_y, "z": 0},
            },
        },
    }


def _build_summary(
    platforms: list[ScenePlatform],
    actions: list[dict[str, Any]],
    metrics: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build summary statistics for the scene."""
    total_objects = sum(len(p.objects) for p in platforms)
    phases = [p.phase for p in platforms]
    total_tokens = 0
    total_duration = 0.0
    type_counts: dict[str, int] = {}

    for action in actions:
        llm_ctx = action.get("llm_context", {})
        t_in = llm_ctx.get("tokens_in", 0) or 0
        t_out = llm_ctx.get("tokens_out", 0) or 0
        total_tokens += t_in + t_out
        total_duration += action.get("duration_ms", 0.0)
        a_type = action.get("action_type", "unknown")
        type_counts[a_type] = type_counts.get(a_type, 0) + 1

    return {
        "total_objects": total_objects,
        "total_platforms": len(platforms),
        "phases": phases,
        "total_tokens": total_tokens,
        "total_duration_ms": round(total_duration, 2),
        "action_type_counts": type_counts,
        "status": result.get("status", "unknown"),
        "total_iterations": result.get("total_iterations", 0),
    }


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
