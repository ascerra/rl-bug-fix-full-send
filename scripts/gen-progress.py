#!/usr/bin/env python3
"""Generate progress/index.html from run-log.md and IMPLEMENTATION-PLAN.md.

Run after each meta loop session:
    python scripts/gen-progress.py

Or let the loop run it as a closing step.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUN_LOG = ROOT / "progress" / "run-log.md"
IMPL_PLAN = ROOT / "IMPLEMENTATION-PLAN.md"
OUTPUT = ROOT / "progress" / "index.html"


def parse_run_log() -> list[dict]:
    if not RUN_LOG.exists():
        return []
    text = RUN_LOG.read_text()
    runs = []
    for block in re.split(r"^## Run \d+", text, flags=re.MULTILINE)[1:]:
        run: dict = {}
        date_match = re.match(r"\s*—\s*(\S+)", block)
        run["date"] = date_match.group(1) if date_match else "unknown"
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("**Phase**:"):
                run["phase"] = line.split(":", 1)[1].strip().strip("*")
            elif line.startswith("**What shipped**:"):
                run["shipped"] = line.split(":", 1)[1].strip().strip("*")
            elif line.startswith("**Test result**:"):
                run["tests"] = line.split(":", 1)[1].strip().strip("*")
            elif line.startswith("**Next focus**:"):
                run["next"] = line.split(":", 1)[1].strip().strip("*")
            elif line.startswith("**Issues hit**:"):
                run["issues"] = line.split(":", 1)[1].strip().strip("*")
        runs.append(run)
    return runs


def _is_non_item_heading(line: str) -> bool:
    """Detect headings that are organizational groupings, not trackable items."""
    stripped = line.lstrip("#").strip()
    if re.match(r"(CRITICAL|HIGH|MEDIUM|LOW)\s+—", stripped):
        return True
    if re.match(r"(Phase \d+ )?Build Order", stripped):
        return True
    if re.match(r"Timeline", stripped):
        return True
    return False


def parse_phases() -> list[dict]:
    if not IMPL_PLAN.exists():
        return []
    text = IMPL_PLAN.read_text()
    phases = []
    current_phase = None
    for line in text.splitlines():
        if re.match(r"^## Phase \d", line):
            if current_phase:
                phases.append(current_phase)
            current_phase = {"name": line.lstrip("#").strip(), "items": [], "done": 0, "total": 0}
        elif current_phase and re.match(r"^###+ ", line):
            if _is_non_item_heading(line):
                continue
            item_name = line.lstrip("#").strip()
            done = "✅" in item_name
            current_phase["items"].append({"name": item_name.replace("✅", "").strip(), "done": done})
            current_phase["total"] += 1
            if done:
                current_phase["done"] += 1
    if current_phase:
        phases.append(current_phase)
    return phases


def get_test_status() -> str:
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/", "-v", "--tb=no", "-q"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=300,
        )
        last_line = [l for l in result.stdout.strip().splitlines() if l.strip()][-1]
        return last_line
    except Exception as e:
        return f"Could not run tests: {e}"


def get_lint_status() -> str:
    try:
        result = subprocess.run(
            ["uvx", "ruff", "check", "engine/", "tests/"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=15,
        )
        return "Clean" if result.returncode == 0 else result.stdout.strip().splitlines()[-1]
    except Exception as e:
        return f"Could not run lint: {e}"


def generate_html(runs: list[dict], phases: list[dict], test_status: str, lint_status: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_items = sum(p["total"] for p in phases)
    done_items = sum(p["done"] for p in phases)
    pct = int(done_items / total_items * 100) if total_items else 0

    phase_rows = ""
    for p in phases:
        pct_phase = int(p["done"] / p["total"] * 100) if p["total"] else 0
        color = "#22c55e" if pct_phase == 100 else "#eab308" if pct_phase > 0 else "#64748b"
        items_html = ""
        for item in p["items"]:
            icon = "&#10003;" if item["done"] else "&#9744;"
            items_html += f'<div class="item {"done" if item["done"] else ""}">{icon} {item["name"]}</div>'
        phase_rows += f"""
        <div class="phase-card">
            <div class="phase-header">
                <span class="phase-name">{p["name"]}</span>
                <span class="phase-pct" style="color:{color}">{p["done"]}/{p["total"]}</span>
            </div>
            <div class="progress-bar"><div class="progress-fill" style="width:{pct_phase}%;background:{color}"></div></div>
            <div class="items">{items_html}</div>
        </div>"""

    run_rows = ""
    for r in reversed(runs):
        issues_class = "issues-none" if r.get("issues", "None") == "None" else "issues-yes"
        run_rows += f"""
        <div class="run-card">
            <div class="run-header">
                <span class="run-date">{r.get("date", "?")}</span>
                <span class="run-phase">{r.get("phase", "?")}</span>
            </div>
            <div class="run-shipped">{r.get("shipped", "?")}</div>
            <div class="run-meta">
                <span class="run-tests">{r.get("tests", "?")}</span>
                <span class="{issues_class}">Issues: {r.get("issues", "None")}</span>
            </div>
            <div class="run-next">Next: {r.get("next", "?")}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RL Bug Fix Full Send — Progress</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f172a; color:#e2e8f0; padding:2rem; }}
h1 {{ font-size:1.5rem; margin-bottom:0.5rem; color:#38bdf8; }}
.subtitle {{ color:#94a3b8; margin-bottom:2rem; }}
.status-bar {{ display:flex; gap:2rem; margin-bottom:2rem; flex-wrap:wrap; }}
.status-box {{ background:#1e293b; border-radius:8px; padding:1rem 1.5rem; min-width:180px; }}
.status-label {{ font-size:0.75rem; color:#94a3b8; text-transform:uppercase; letter-spacing:0.05em; }}
.status-value {{ font-size:1.25rem; font-weight:600; margin-top:0.25rem; }}
.status-value.green {{ color:#22c55e; }}
.status-value.yellow {{ color:#eab308; }}
.status-value.red {{ color:#ef4444; }}
.section-title {{ font-size:1.1rem; color:#38bdf8; margin:2rem 0 1rem; border-bottom:1px solid #334155; padding-bottom:0.5rem; }}
.overall-bar {{ background:#334155; border-radius:999px; height:12px; margin-bottom:2rem; overflow:hidden; }}
.overall-fill {{ height:100%; border-radius:999px; background:linear-gradient(90deg,#22c55e,#38bdf8); transition:width 0.5s; }}
.phase-card {{ background:#1e293b; border-radius:8px; padding:1rem; margin-bottom:1rem; }}
.phase-header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem; }}
.phase-name {{ font-weight:600; }}
.phase-pct {{ font-weight:700; font-size:0.9rem; }}
.progress-bar {{ background:#334155; border-radius:999px; height:6px; margin-bottom:0.75rem; overflow:hidden; }}
.progress-fill {{ height:100%; border-radius:999px; transition:width 0.5s; }}
.items {{ display:flex; flex-wrap:wrap; gap:0.25rem 1rem; font-size:0.8rem; color:#94a3b8; }}
.item.done {{ color:#22c55e; }}
.run-card {{ background:#1e293b; border-radius:8px; padding:1rem; margin-bottom:0.75rem; border-left:3px solid #38bdf8; }}
.run-header {{ display:flex; justify-content:space-between; margin-bottom:0.5rem; }}
.run-date {{ font-weight:700; color:#38bdf8; }}
.run-phase {{ font-size:0.8rem; color:#94a3b8; }}
.run-shipped {{ margin-bottom:0.5rem; font-size:0.9rem; }}
.run-meta {{ display:flex; gap:1.5rem; font-size:0.8rem; color:#94a3b8; margin-bottom:0.25rem; }}
.run-tests {{ color:#22c55e; }}
.issues-none {{ color:#94a3b8; }}
.issues-yes {{ color:#eab308; }}
.run-next {{ font-size:0.8rem; color:#38bdf8; }}
</style>
</head>
<body>
<h1>RL Bug Fix Full Send</h1>
<p class="subtitle">Meta Loop Progress Dashboard — generated {now}</p>

<div class="status-bar">
    <div class="status-box">
        <div class="status-label">Overall Progress</div>
        <div class="status-value {"green" if pct==100 else "yellow" if pct>0 else ""}">{pct}% ({done_items}/{total_items})</div>
    </div>
    <div class="status-box">
        <div class="status-label">Tests</div>
        <div class="status-value {"green" if "passed" in test_status else "red"}">{test_status}</div>
    </div>
    <div class="status-box">
        <div class="status-label">Lint</div>
        <div class="status-value {"green" if lint_status=="Clean" else "red"}">{lint_status}</div>
    </div>
    <div class="status-box">
        <div class="status-label">Loop Runs</div>
        <div class="status-value">{len(runs)}</div>
    </div>
</div>

<div class="overall-bar"><div class="overall-fill" style="width:{pct}%"></div></div>

<div class="section-title">Phase Progress</div>
{phase_rows}

<div class="section-title">Run History</div>
{run_rows}

</body>
</html>"""


def main():
    runs = parse_run_log()
    phases = parse_phases()
    test_status = get_test_status()
    lint_status = get_lint_status()
    html = generate_html(runs, phases, test_status, lint_status)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(html)
    print(f"Dashboard written to {OUTPUT}")
    print(f"Tests: {test_status}")
    print(f"Lint: {lint_status}")
    print(f"Phases: {sum(p['done'] for p in phases)}/{sum(p['total'] for p in phases)} items complete")


if __name__ == "__main__":
    main()
