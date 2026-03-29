"""Execution reconstructor — rebuilds the agent timeline from artifacts.

Reads ``execution.json``, ``log.json``, ``transcripts/``, and
``progress.md`` from the agent's output directory and produces a
chronological list of :class:`TimelineEvent` objects.  Also extracts
model identities, prompt template digests, and tool definitions for
use by the attestation builder.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from engine.observer import ModelInfo, TimelineEvent

_EVENT_TYPE_MAP: dict[str, str] = {
    "llm_query": "llm_call",
    "file_read": "file_operation",
    "file_write": "file_operation",
    "file_search": "file_operation",
    "file_edit": "file_operation",
    "shell_run": "shell_command",
    "git_diff": "shell_command",
    "git_commit": "shell_command",
    "github_api": "api_call",
    "escalation": "escalation",
    "workflow_health_check": "workflow_health",
}


class ExecutionReconstructor:
    """Reconstructs the agent's execution timeline from output artifacts.

    Usage::

        recon = ExecutionReconstructor()
        recon.load_artifacts(Path("./output"))
        timeline = recon.build_timeline()
        models = recon.extract_model_info()
    """

    def __init__(self) -> None:
        self._execution_data: dict[str, Any] = {}
        self._log_data: list[dict[str, Any]] = []
        self._transcript_calls: list[dict[str, Any]] = []
        self._progress_text: str = ""
        self._artifacts_dir: Path | None = None

    def load_artifacts(self, artifacts_dir: Path) -> None:
        """Load agent artifacts from the given directory.

        Reads:
        - ``execution.json`` — the primary execution record
        - ``log.json`` — structured log entries
        - ``transcripts/transcript-calls.json`` — full LLM call transcripts
        - ``progress.md`` — human-readable progress narration

        Missing files are silently skipped (the reconstructor works with
        whatever is available).
        """
        self._artifacts_dir = artifacts_dir

        execution_path = artifacts_dir / "execution.json"
        if execution_path.exists():
            with execution_path.open() as f:
                self._execution_data = json.load(f)

        log_path = artifacts_dir / "log.json"
        if log_path.exists():
            with log_path.open() as f:
                self._log_data = json.load(f)

        transcript_path = artifacts_dir / "transcripts" / "transcript-calls.json"
        if transcript_path.exists():
            with transcript_path.open() as f:
                self._transcript_calls = json.load(f)

        progress_path = artifacts_dir / "progress.md"
        if progress_path.exists():
            self._progress_text = progress_path.read_text()

    @property
    def execution_data(self) -> dict[str, Any]:
        """Return the raw execution data dict."""
        return self._execution_data

    def build_timeline(self) -> list[TimelineEvent]:
        """Build a chronological list of timeline events from the artifacts.

        Merges data from ``execution.json`` (iterations + actions) and
        ``log.json`` (structured log entries).  Sorts by timestamp.
        """
        events: list[TimelineEvent] = []

        execution = self._execution_data.get("execution", self._execution_data)

        for iteration in execution.get("iterations", []):
            events.append(
                TimelineEvent(
                    timestamp=iteration.get("started_at", ""),
                    event_type="phase_transition",
                    phase=iteration.get("phase", ""),
                    iteration=iteration.get("number", 0),
                    description=f"Phase: {iteration.get('phase', '?')} "
                    f"(iteration {iteration.get('number', '?')})",
                    details={
                        "duration_ms": iteration.get("duration_ms", 0),
                        "result": iteration.get("result", {}),
                        "findings": iteration.get("findings", {}),
                    },
                )
            )

        for action in execution.get("actions", []):
            raw_type = action.get("action_type", "")
            event_type = _EVENT_TYPE_MAP.get(raw_type, raw_type or "unknown")
            events.append(
                TimelineEvent(
                    timestamp=action.get("timestamp", ""),
                    event_type=event_type,
                    phase=action.get("phase", ""),
                    iteration=action.get("iteration", 0),
                    description=action.get("input", {}).get("description", ""),
                    details={
                        "action_id": action.get("id", ""),
                        "action_type": raw_type,
                        "output": action.get("output", {}),
                        "llm_context": action.get("llm_context", {}),
                        "duration_ms": action.get("duration_ms", 0),
                    },
                )
            )

        events.sort(key=lambda e: e.timestamp or "")
        return events

    def extract_model_info(self) -> list[ModelInfo]:
        """Extract deduplicated model identities from execution actions.

        Aggregates call counts and token totals per (model, provider) pair.
        """
        execution = self._execution_data.get("execution", self._execution_data)
        model_map: dict[tuple[str, str], ModelInfo] = {}

        for action in execution.get("actions", []):
            llm_ctx = action.get("llm_context", {})
            model = llm_ctx.get("model", "")
            provider = llm_ctx.get("provider", "")
            if not model:
                continue

            key = (model, provider)
            if key not in model_map:
                model_map[key] = ModelInfo(model=model, provider=provider)
            info = model_map[key]
            info.total_calls += 1
            info.total_tokens_in += llm_ctx.get("tokens_in", 0)
            info.total_tokens_out += llm_ctx.get("tokens_out", 0)

        return list(model_map.values())

    def extract_prompt_digests(self, templates_dir: Path | None = None) -> dict[str, str]:
        """Compute SHA-256 digests of prompt template files.

        If *templates_dir* is not provided, looks for ``templates/prompts/``
        relative to the current working directory.

        Returns a mapping of ``prompt://<filename>`` → ``sha256:<hex>``.
        """
        if templates_dir is None:
            templates_dir = Path("templates/prompts")

        digests: dict[str, str] = {}
        if not templates_dir.is_dir():
            return digests

        for template_file in sorted(templates_dir.iterdir()):
            if template_file.is_file() and template_file.suffix == ".md":
                content = template_file.read_bytes()
                sha = hashlib.sha256(content).hexdigest()
                digests[f"prompt://{template_file.name}"] = f"sha256:{sha}"

        return digests

    def extract_tool_definitions(self) -> dict[str, Any]:
        """Extract and hash the tool executor configuration.

        Collects the set of unique tool types used in the execution and
        computes a digest of the sorted tool list.
        """
        execution = self._execution_data.get("execution", self._execution_data)
        tool_types: set[str] = set()

        for action in execution.get("actions", []):
            action_type = action.get("action_type", "")
            if action_type and action_type != "llm_query":
                tool_types.add(action_type)

        sorted_tools = sorted(tool_types)
        digest = hashlib.sha256(json.dumps(sorted_tools).encode()).hexdigest()

        return {
            "tools": sorted_tools,
            "digest": f"sha256:{digest}",
        }

    def get_file_changes(self) -> list[dict[str, str]]:
        """Extract file changes recorded in the execution.

        Scans action records for file_write/file_edit operations and
        returns a list of ``{"path": ..., "action_type": ...}`` dicts.
        """
        execution = self._execution_data.get("execution", self._execution_data)
        changes: list[dict[str, str]] = []

        for action in execution.get("actions", []):
            action_type = action.get("action_type", "")
            if action_type in ("file_write", "file_edit"):
                path = action.get("input", {}).get("context", {}).get("path", "") or action.get(
                    "input", {}
                ).get("description", "")
                changes.append({"path": path, "action_type": action_type})

        return changes

    def get_transcript_calls(self) -> list[dict[str, Any]]:
        """Return the loaded transcript calls."""
        return list(self._transcript_calls)

    def get_progress_text(self) -> str:
        """Return the loaded progress.md content."""
        return self._progress_text

    def get_execution_result(self) -> dict[str, Any]:
        """Return the execution result section."""
        execution = self._execution_data.get("execution", self._execution_data)
        return execution.get("result", {})

    def get_execution_config(self) -> dict[str, Any]:
        """Return the execution config section."""
        execution = self._execution_data.get("execution", self._execution_data)
        return execution.get("config", {})

    def get_execution_metadata(self) -> dict[str, Any]:
        """Return core execution metadata (id, timing, trigger, target)."""
        execution = self._execution_data.get("execution", self._execution_data)
        return {
            "id": execution.get("id", ""),
            "started_at": execution.get("started_at", ""),
            "completed_at": execution.get("completed_at", ""),
            "trigger": execution.get("trigger", {}),
            "target": execution.get("target", {}),
        }
