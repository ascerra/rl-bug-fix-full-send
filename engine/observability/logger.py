"""Structured JSON logger with correlation IDs and phase tagging.

Includes a ``narrate()`` method for human-readable progress lines
visible in live GitHub Actions logs (prefixed with ``>>>``).
Narrations are also appended to ``progress.md`` for artifact upload.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.secrets import SecretRedactor


class StructuredLogger:
    """Dual-output logger: structured JSON to file, rich text to stdout.

    Also supports a ``narrate()`` channel for human-readable progress
    summaries that are written to stderr (with ``>>>`` prefix) and
    optionally to a running ``progress.md`` file.
    """

    def __init__(
        self,
        execution_id: str | None = None,
        output_path: str | Path | None = None,
        progress_path: str | Path | None = None,
        redactor: SecretRedactor | None = None,
    ):
        self.execution_id = execution_id or str(uuid.uuid4())
        self._output_path = Path(output_path) if output_path else None
        self._progress_path = Path(progress_path) if progress_path else None
        self._entries: list[dict[str, Any]] = []
        self._narrations: list[dict[str, Any]] = []
        self._current_phase: str = "init"
        self._current_iteration: int = 0
        self._redactor = redactor

    def set_phase(self, phase: str) -> None:
        self._current_phase = phase

    def set_iteration(self, iteration: int) -> None:
        self._current_iteration = iteration

    def log(
        self,
        level: str,
        message: str,
        **extra: Any,
    ) -> None:
        if self._redactor:
            message = self._redactor.redact(message)
            extra = self._redactor.redact_dict(extra)
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "execution_id": self.execution_id,
            "phase": self._current_phase,
            "iteration": self._current_iteration,
            "message": message,
            **extra,
        }
        self._entries.append(entry)
        self._write_stdout(entry)

    def info(self, message: str, **extra: Any) -> None:
        self.log("INFO", message, **extra)

    def warn(self, message: str, **extra: Any) -> None:
        self.log("WARN", message, **extra)

    def error(self, message: str, **extra: Any) -> None:
        self.log("ERROR", message, **extra)

    def debug(self, message: str, **extra: Any) -> None:
        self.log("DEBUG", message, **extra)

    # ------------------------------------------------------------------
    # Narration — human-readable progress channel
    # ------------------------------------------------------------------

    def narrate(self, message: str) -> None:
        """Write a human-readable progress line to stderr and progress.md.

        Lines are prefixed with ``>>>`` and the current phase name for
        visibility in live GitHub Actions log output.  Example::

            >>> [TRIAGE] Classified as bug (confidence: 0.85). Moving to implement.
        """
        phase = self._current_phase
        if self._redactor:
            message = self._redactor.redact(message)
        prefix = f"[{phase.upper()}] " if phase and phase != "init" else ""
        line = f">>> {prefix}{message}"
        print(line, file=sys.stderr)
        self._narrations.append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "phase": phase,
                "iteration": self._current_iteration,
                "message": message,
            }
        )
        self._append_progress(f"- {message}\n")

    def write_progress_heading(self, heading: str) -> None:
        """Write a markdown heading line to progress.md."""
        self._append_progress(f"\n{heading}\n\n")

    def get_narrations(self) -> list[dict[str, Any]]:
        """Return a copy of all narration entries."""
        return list(self._narrations)

    # ------------------------------------------------------------------
    # Flushing and serialization
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """Write all entries to the output file."""
        if self._output_path:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with self._output_path.open("w") as f:
                json.dump(self._entries, f, indent=2)

    def get_entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_stdout(self, entry: dict[str, Any]) -> None:
        ts = entry["timestamp"][:19]
        level = entry["level"]
        phase = entry["phase"]
        iteration = entry["iteration"]
        msg = entry["message"]
        prefix = f"[{ts}] [{level:5}] [phase={phase} iter={iteration}]"
        print(f"{prefix} {msg}", file=sys.stderr)

    def _append_progress(self, text: str) -> None:
        """Append raw text to the progress.md file."""
        if not self._progress_path:
            return
        self._progress_path.parent.mkdir(parents=True, exist_ok=True)
        with self._progress_path.open("a") as f:
            f.write(text)
