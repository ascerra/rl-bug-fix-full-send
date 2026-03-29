"""Narrative generation for detail drill-down panels and landing page.

Transforms raw action records into human-readable HTML fragments
and builds narrative summary data for the report landing page.
All output is pre-formatted narrative — no raw JSON/YAML exposed to users.
"""

from engine.visualization.narrative.formatter import (
    NarrativeFormatter,
    enrich_scene_with_narratives,
)
from engine.visualization.narrative.summary import (
    LandingData,
    MetricCard,
    NarrativeSummaryBuilder,
    PhaseBar,
    build_landing,
)

__all__ = [
    "LandingData",
    "MetricCard",
    "NarrativeFormatter",
    "NarrativeSummaryBuilder",
    "PhaseBar",
    "build_landing",
    "enrich_scene_with_narratives",
]
