"""3D scene generation for Three.js interactive reports.

Transforms execution records into a scene graph that the frontend
Three.js renderer consumes.  The Python code produces the data
structure; the JavaScript code (9.2) renders it.
"""

from engine.visualization.scene.builder import (
    SceneBuilder,
    SceneConnection,
    SceneData,
    SceneObject,
    ScenePlatform,
    build_scene,
)
from engine.visualization.scene.timeline import (
    TimelineData,
    TimelineEvent,
    TimelineMarker,
    build_timeline,
)

__all__ = [
    "SceneBuilder",
    "SceneConnection",
    "SceneData",
    "SceneObject",
    "ScenePlatform",
    "TimelineData",
    "TimelineEvent",
    "TimelineMarker",
    "build_scene",
    "build_timeline",
]
