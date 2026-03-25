"""Structured JSON logger with correlation IDs and phase tagging."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StructuredLogger:
    """Dual-output logger: structured JSON to file, rich text to stdout."""

    def __init__(
        self,
        execution_id: str | None = None,
        output_path: str | Path | None = None,
    ):
        self.execution_id = execution_id or str(uuid.uuid4())
        self._output_path = Path(output_path) if output_path else None
        self._entries: list[dict[str, Any]] = []
        self._current_phase: str = "init"
        self._current_iteration: int = 0

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

    def flush(self) -> None:
        """Write all entries to the output file."""
        if self._output_path:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
            with self._output_path.open("w") as f:
                json.dump(self._entries, f, indent=2)

    def get_entries(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def _write_stdout(self, entry: dict[str, Any]) -> None:
        ts = entry["timestamp"][:19]
        level = entry["level"]
        phase = entry["phase"]
        iteration = entry["iteration"]
        msg = entry["message"]
        prefix = f"[{ts}] [{level:5}] [phase={phase} iter={iteration}]"
        print(f"{prefix} {msg}", file=sys.stderr)
