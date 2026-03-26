#!/usr/bin/env python3
"""Check whether the meta ralph loop should stop.

Exit 0 = complete (loop should stop).
Exit 1 = not complete (loop should continue).

Checks two sources:
  1. progress/status.json  — if {"ralphComplete": true}, done.
  2. IMPLEMENTATION-PLAN.md — if every ### sub-phase heading has ✅, done.

Usage:
    python scripts/is-ralph-complete.py          # exit code only
    python scripts/is-ralph-complete.py --verbose # print summary too
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS_FILE = ROOT / "progress" / "status.json"
IMPL_PLAN = ROOT / "IMPLEMENTATION-PLAN.md"


def check_status_json() -> bool | None:
    """Return True/False from status.json, or None if absent."""
    if not STATUS_FILE.exists():
        return None
    try:
        data = json.loads(STATUS_FILE.read_text())
        return bool(data.get("ralphComplete", False))
    except (json.JSONDecodeError, KeyError):
        return None


_NON_ITEM_RE = re.compile(
    r"^(CRITICAL|HIGH|MEDIUM|LOW)\s+—|^(Phase \d+ )?Build Order|^Timeline"
)


def check_implementation_plan() -> tuple[int, int]:
    """Return (done, total) sub-phase items from IMPLEMENTATION-PLAN.md.

    Matches ### and #### headings that start with a digit (e.g. ``### 0.1``
    or ``#### 7.3``).  Skips severity-group and organisational headings.
    """
    if not IMPL_PLAN.exists():
        return 0, 0
    text = IMPL_PLAN.read_text()
    total = 0
    done = 0
    for line in text.splitlines():
        if not re.match(r"^###+ ", line):
            continue
        stripped = line.lstrip("#").strip()
        if _NON_ITEM_RE.match(stripped):
            continue
        if not re.match(r"\d", stripped):
            continue
        total += 1
        if "✅" in line:
            done += 1
    return done, total


def main() -> None:
    verbose = "--verbose" in sys.argv

    status = check_status_json()
    if status is True:
        if verbose:
            print("ralphComplete=true in progress/status.json — done!")
        sys.exit(0)

    done, total = check_implementation_plan()
    if verbose:
        pct = int(done / total * 100) if total else 0
        print(f"IMPLEMENTATION-PLAN.md: {done}/{total} items complete ({pct}%)")

    if total > 0 and done >= total:
        if verbose:
            print("All implementation plan items complete — done!")
        sys.exit(0)

    if verbose:
        remaining = total - done
        print(f"{remaining} items remaining — loop should continue.")

    sys.exit(1)


if __name__ == "__main__":
    main()
