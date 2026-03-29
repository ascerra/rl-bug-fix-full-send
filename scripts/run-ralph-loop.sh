#!/usr/bin/env bash
# Ralph loop for rl-bug-fix-full-send meta loop.
#
# Runs the Cursor agent in a loop until:
#   - All IMPLEMENTATION-PLAN.md items are marked ✅, OR
#   - progress/status.json has {"ralphComplete": true}, OR
#   - RALPH_MAX_RUNS is reached, OR
#   - Ctrl+C
#
# After every iteration: regenerates progress/index.html, shows git diff summary.
#
# Usage (from the rl-bug-fix-full-send directory):
#   ./scripts/run-ralph-loop.sh
#
# Env:
#   RALPH_SLEEP_SECONDS  — pause between iterations (default: 120)
#   RALPH_MAX_RUNS       — optional cap; 0 = no cap (default: 0)
#   RALPH_NICE           — 1 = nice/ionice when available (default: 1)
#   RALPH_AGENT_FLAGS    — extra flags to pass to agent CLI (default: empty)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SLEEP_SEC="${RALPH_SLEEP_SECONDS:-120}"
MAX_RUNS="${RALPH_MAX_RUNS:-0}"
NICE="${RALPH_NICE:-1}"
COUNT=0

agent_cmd() {
  if [[ "$NICE" == "1" ]] && command -v nice >/dev/null; then
    if command -v ionice >/dev/null; then
      nice -n 10 ionice -c 3 agent "$@"
    else
      nice -n 10 agent "$@"
    fi
  else
    agent "$@"
  fi
}

cd "$PROJECT_DIR"

# Reset stale completion signal from previous runs
if [[ -f "progress/status.json" ]]; then
  prev=$(python -c "import json,sys; print(json.load(open('progress/status.json')).get('ralphComplete',False))" 2>/dev/null || echo "False")
  if [[ "$prev" == "True" ]]; then
    echo "Clearing stale ralphComplete=true from previous run..."
    echo '{"ralphComplete": false}' > progress/status.json
  fi
fi

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Ralph Loop — rl-bug-fix-full-send meta loop                ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Runs until all IMPLEMENTATION-PLAN.md items are ✅          ║"
echo "║  or progress/status.json has ralphComplete=true             ║"
echo "║  or Ctrl+C                                                  ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Workspace:            $PROJECT_DIR"
echo "Sleep between runs:   ${SLEEP_SEC}s"
echo "Max runs:             ${MAX_RUNS:-unlimited}"
echo "Nice mode:            $NICE"
echo ""

# Show starting state
python scripts/is-ralph-complete.py --verbose 2>/dev/null || true
echo ""
echo "Tip: Close extra Chromium/Electron apps. Agent processes are heavy."
echo ""

while true; do
  COUNT=$((COUNT + 1))
  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "  Iteration $COUNT @ $(date '+%Y-%m-%d %H:%M:%S')"
  echo "╚══════════════════════════════════════════════════════════════╝"

  set +e
  agent_cmd \
    -p \
    --trust \
    --force \
    --workspace "$PROJECT_DIR" \
    --output-format text \
    ${RALPH_AGENT_FLAGS:-} \
    "$(cat "$PROJECT_DIR/prompt.md")"
  AGENT_EXIT=$?
  set -e

  echo ""
  echo "--- agent exit code: $AGENT_EXIT ---"

  # Git diff summary
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git diff --quiet 2>/dev/null; then
      echo "git: no unstaged changes"
    else
      echo ""
      echo "--- git diff --stat (unstaged) ---"
      git diff --stat 2>/dev/null | head -40 || true
    fi
  fi

  # Regenerate progress dashboard
  if [[ -f "scripts/gen-progress.py" ]]; then
    echo ""
    echo "--- Regenerating progress dashboard ---"
    python scripts/gen-progress.py 2>&1 || echo "(gen-progress.py failed)"
  fi

  # Check completion
  if python scripts/is-ralph-complete.py --verbose 2>/dev/null; then
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  Ralph loop COMPLETE — all items done or ralphComplete=true ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo "Open: $PROJECT_DIR/progress/index.html"
    break
  fi

  # Max runs check
  if [[ "$MAX_RUNS" -gt 0 && "$COUNT" -ge "$MAX_RUNS" ]]; then
    echo ""
    echo "RALPH_MAX_RUNS=$MAX_RUNS reached — stopping."
    python scripts/is-ralph-complete.py --verbose 2>/dev/null || true
    break
  fi

  echo ""
  echo "Cooling down ${SLEEP_SEC}s — Ctrl+C to stop."
  echo "Loop continues until all IMPLEMENTATION-PLAN.md items are ✅."
  sleep "$SLEEP_SEC"
done
