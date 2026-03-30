#!/usr/bin/env bash
# Meta Ralph Loop — Production Engine CI Runner
#
# Triggers the rl-engine.yml workflow in GitHub Actions, monitors the run,
# downloads artifacts, and analyzes the results. This IS the Ralph Loop —
# the iterative development methodology that builds and maintains the
# production engine. Designed to be run repeatedly:
#
#   1. Make engine changes locally
#   2. Push to GitHub
#   3. Run this script to trigger and monitor the production loop
#   4. Review the analysis output
#   5. Make more engine changes based on findings
#   6. Repeat until the production loop submits a good PR
#
# Usage:
#   ./scripts/meta-loop.sh --issue-url <URL> [OPTIONS]
#
# Options:
#   --issue-url URL       GitHub issue URL (required)
#   --fork-repo REPO      Fork repo for cross-fork PR (e.g. ascerra/build-definitions)
#   --provider PROVIDER   LLM provider: gemini or anthropic (default: gemini)
#   --config YAML         Inline YAML config overrides
#   --repo OWNER/REPO     GitHub repo where the workflow lives (default: auto-detect)
#   --ref REF             Git ref to run the workflow on (default: main)
#   --watch               Wait for completion and download artifacts (default: true)
#   --no-watch            Trigger only, don't wait
#   --output-dir DIR      Where to save downloaded artifacts (default: ./meta-loop-runs)
#   --task DESCRIPTION     Run the agent in task mode first: implement the described task,
#                          push changes, then trigger the production job to validate
#   --continuous           Run continuously: trigger → wait → analyze → auto-fix → push → repeat
#   --max-runs N          Max continuous runs (default: 10)
#   --auto-push           Automatically commit and push agent changes between runs
#   --dry-run             Show agent-proposed changes without applying them
#
# Prerequisites:
#   - gh CLI installed and authenticated
#   - jq installed (for JSON parsing)
#   - The workflow must be pushed to the repo already
#
# Environment:
#   META_LOOP_REPO       Override repo (default: auto-detect from git remote)
#   META_LOOP_REF        Override ref (default: main)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Defaults
ISSUE_URL=""
FORK_REPO=""
PROVIDER="gemini"
CONFIG_OVERRIDE=""
REPO="${META_LOOP_REPO:-}"
REF="${META_LOOP_REF:-main}"
WATCH=true
OUTPUT_DIR="$PROJECT_DIR/meta-loop-runs"
TASK=""
CONTINUOUS=false
MAX_RUNS=10
AUTO_PUSH=false
DRY_RUN=false

usage() {
  head -35 "$0" | grep '^#' | sed 's/^# \?//'
  exit 1
}

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --issue-url)    ISSUE_URL="$2"; shift 2 ;;
    --fork-repo)    FORK_REPO="$2"; shift 2 ;;
    --provider)     PROVIDER="$2"; shift 2 ;;
    --task)         TASK="$2"; shift 2 ;;
    --config)       CONFIG_OVERRIDE="$2"; shift 2 ;;
    --repo)         REPO="$2"; shift 2 ;;
    --ref)          REF="$2"; shift 2 ;;
    --watch)        WATCH=true; shift ;;
    --no-watch)     WATCH=false; shift ;;
    --output-dir)   OUTPUT_DIR="$2"; shift 2 ;;
    --continuous)   CONTINUOUS=true; shift ;;
    --max-runs)     MAX_RUNS="$2"; shift 2 ;;
    --auto-push)    AUTO_PUSH=true; shift ;;
    --dry-run)      DRY_RUN=true; shift ;;
    -h|--help)      usage ;;
    *)              echo "Unknown option: $1"; usage ;;
  esac
done

if [[ -z "$ISSUE_URL" ]]; then
  echo "ERROR: --issue-url is required"
  usage
fi

# Auto-detect repo from git remote if not provided
if [[ -z "$REPO" ]]; then
  cd "$PROJECT_DIR"
  REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
  if [[ "$REMOTE_URL" =~ github\.com[:/]([^/]+/[^/.]+) ]]; then
    REPO="${BASH_REMATCH[1]}"
  else
    echo "ERROR: Could not auto-detect GitHub repo. Use --repo OWNER/REPO"
    exit 1
  fi
fi

# Check prerequisites
for cmd in gh jq; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "ERROR: $cmd is required but not installed"
    exit 1
  fi
done

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Meta Ralph Loop — Production Engine CI Runner              ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  Repo:         $REPO"
echo "  Ref:          $REF"
echo "  Issue URL:    $ISSUE_URL"
echo "  Fork repo:    ${FORK_REPO:-none}"
echo "  Provider:     $PROVIDER"
echo "  Task:         ${TASK:-none}"
echo "  Config:       ${CONFIG_OVERRIDE:-default}"
echo "  Watch:        $WATCH"
echo "  Continuous:   $CONTINUOUS"
echo "  Auto-push:    $AUTO_PUSH"
echo "  Dry run:      $DRY_RUN"
echo "  Output dir:   $OUTPUT_DIR"
echo ""

trigger_workflow() {
  echo "▸ Triggering rl-engine.yml workflow..."

  local ARGS=()
  ARGS+=(-f "issue_url=$ISSUE_URL")
  ARGS+=(-f "llm_provider=$PROVIDER")
  if [[ -n "$FORK_REPO" ]]; then
    ARGS+=(-f "fork_repo=$FORK_REPO")
  fi
  if [[ -n "$CONFIG_OVERRIDE" ]]; then
    ARGS+=(-f "config_override=$CONFIG_OVERRIDE")
  fi

  gh workflow run rl-engine.yml \
    --repo "$REPO" \
    --ref "$REF" \
    "${ARGS[@]}"

  echo "  Workflow triggered. Waiting 5s for run to register..."
  sleep 5

  # Get the run ID of the most recent run
  RUN_ID=$(gh run list \
    --repo "$REPO" \
    --workflow rl-engine.yml \
    --limit 1 \
    --json databaseId \
    --jq '.[0].databaseId')

  if [[ -z "$RUN_ID" || "$RUN_ID" == "null" ]]; then
    echo "ERROR: Could not find the triggered run"
    return 1
  fi

  echo "  Run ID: $RUN_ID"
  echo "  URL: https://github.com/$REPO/actions/runs/$RUN_ID"
  echo "$RUN_ID"
}

wait_for_run() {
  local RUN_ID="$1"
  echo ""
  echo "▸ Monitoring run $RUN_ID..."
  echo "  (Ctrl+C to stop watching — the run continues in GitHub Actions)"
  echo ""

  # Poll status until done
  local STATUS="in_progress"
  local ELAPSED=0
  local POLL_INTERVAL=30

  while [[ "$STATUS" == "in_progress" || "$STATUS" == "queued" || "$STATUS" == "waiting" || "$STATUS" == "pending" || "$STATUS" == "requested" ]]; do
    STATUS=$(gh run view "$RUN_ID" \
      --repo "$REPO" \
      --json status \
      --jq '.status')

    CONCLUSION=$(gh run view "$RUN_ID" \
      --repo "$REPO" \
      --json conclusion \
      --jq '.conclusion // "running"')

    printf "\r  [%4dm %02ds] Status: %-15s Conclusion: %-15s" \
      $((ELAPSED / 60)) $((ELAPSED % 60)) "$STATUS" "$CONCLUSION"

    if [[ "$STATUS" == "completed" ]]; then
      echo ""
      break
    fi

    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
  done

  echo ""
  echo "  Run completed: status=$STATUS conclusion=$CONCLUSION"
  echo "$CONCLUSION"
}

download_artifacts() {
  local RUN_ID="$1"
  local RUN_DIR="$OUTPUT_DIR/run-$RUN_ID"
  mkdir -p "$RUN_DIR"

  echo ""
  echo "▸ Downloading artifacts for run $RUN_ID..."

  # Download all artifacts from the run
  gh run download "$RUN_ID" \
    --repo "$REPO" \
    --dir "$RUN_DIR" 2>/dev/null || {
    echo "  WARNING: Failed to download some artifacts (run may not have produced any)"
  }

  echo "  Artifacts saved to: $RUN_DIR"

  # List what we got
  if [[ -d "$RUN_DIR" ]]; then
    echo ""
    echo "  Downloaded files:"
    find "$RUN_DIR" -type f | head -30 | while read -r f; do
      local SIZE
      SIZE=$(du -h "$f" 2>/dev/null | cut -f1)
      echo "    $SIZE  ${f#$RUN_DIR/}"
    done
  fi

  echo "$RUN_DIR"
}

analyze_run() {
  local RUN_DIR="$1"
  local RUN_ID="$2"

  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Run Analysis — $RUN_ID"
  echo "╚══════════════════════════════════════════════════════════════╝"

  # Find execution.json (may be in a subdirectory)
  local EXEC_JSON
  EXEC_JSON=$(find "$RUN_DIR" -name "execution.json" -type f | head -1)

  if [[ -z "$EXEC_JSON" ]]; then
    echo "  WARNING: No execution.json found in artifacts"
    echo "  The workflow may have failed before producing output."
    echo ""
    echo "  Check the run directly:"
    echo "    gh run view $RUN_ID --repo $REPO --log-failed"
    return 1
  fi

  echo ""
  echo "── Execution Summary ──"
  echo ""

  local STATUS ITERATIONS
  STATUS=$(jq -r '.execution.result.status // "unknown"' "$EXEC_JSON")
  ITERATIONS=$(jq -r '.execution.result.total_iterations // 0' "$EXEC_JSON")

  echo "  Status:      $STATUS"
  echo "  Iterations:  $ITERATIONS"
  echo ""

  # Phase results
  echo "── Phase Results ──"
  echo ""
  jq -r '.execution.result.phase_results[]? | "  \(.phase): \(if .success then "✅" else "❌" end)\(if .escalate then " (ESCALATED)" else "" end)"' "$EXEC_JSON" 2>/dev/null || echo "  (no phase results)"
  echo ""

  # Iteration trace
  echo "── Iteration Trace ──"
  echo ""
  jq -r '.execution.iterations[]? | "  #\(.number) [\(.phase)] \(if .result.success then "✅" else "❌" end) \(.duration_ms // 0 | round)ms\(if .result.next_phase != "" and .result.next_phase != null then " → \(.result.next_phase)" else "" end)\(if .result.escalate then " ESCALATED: \(.result.escalation_reason)" else "" end)"' "$EXEC_JSON" 2>/dev/null || echo "  (no iterations)"
  echo ""

  # Review-specific analysis
  local REVIEW_COUNT REVIEW_REJECTIONS
  REVIEW_COUNT=$(jq '[.execution.iterations[]? | select(.phase == "review")] | length' "$EXEC_JSON" 2>/dev/null || echo 0)
  REVIEW_REJECTIONS=$(jq '[.execution.iterations[]? | select(.phase == "review" and .result.success == false and .result.next_phase == "implement")] | length' "$EXEC_JSON" 2>/dev/null || echo 0)

  if [[ "$REVIEW_COUNT" -gt 0 ]]; then
    echo "── Review Analysis ──"
    echo ""
    echo "  Total reviews:    $REVIEW_COUNT"
    echo "  Rejections:       $REVIEW_REJECTIONS"

    # Extract review verdicts and findings
    jq -r '.execution.iterations[]? | select(.phase == "review") | "  Review #\(.number): verdict=\(.findings.verdict // "unknown"), findings=\(.findings.findings // [] | length), confidence=\(.findings.confidence // "N/A")"' "$EXEC_JSON" 2>/dev/null || true

    echo ""

    # Show review findings
    jq -r '.execution.iterations[]? | select(.phase == "review") | .findings.findings[]? | "    [\(.dimension)/\(.severity)] \(.description // "no description")[0:120]"' "$EXEC_JSON" 2>/dev/null | head -20 || true
    echo ""
  fi

  # Check for escalation
  if [[ "$STATUS" == "escalated" ]]; then
    echo "── ESCALATION DETECTED ──"
    echo ""
    jq -r '.execution.iterations[-1]?.result.escalation_reason // "No reason recorded"' "$EXEC_JSON" 2>/dev/null
    echo ""
    echo "  Possible causes:"
    if [[ "$REVIEW_REJECTIONS" -ge 3 ]]; then
      echo "    → Review rejected $REVIEW_REJECTIONS times (threshold hit)"
      echo "    → Consider: is the review too strict? Is implement not addressing feedback?"
    fi
    local IMPL_FAILURES
    IMPL_FAILURES=$(jq '[.execution.iterations[]? | select(.phase == "implement" and .result.success == false)] | length' "$EXEC_JSON" 2>/dev/null || echo 0)
    if [[ "$IMPL_FAILURES" -gt 2 ]]; then
      echo "    → Implementation failed $IMPL_FAILURES times"
      echo "    → Check LLM responses in transcripts for parse failures or wrong approaches"
    fi
    echo ""
  fi

  # LLM metrics
  echo "── LLM Metrics ──"
  echo ""
  jq -r '"  Total LLM calls:  \(.execution.metrics.total_llm_calls // 0)\n  Tokens in:         \(.execution.metrics.total_tokens_in // 0)\n  Tokens out:        \(.execution.metrics.total_tokens_out // 0)\n  Errors:            \(.execution.metrics.error_count // 0)"' "$EXEC_JSON" 2>/dev/null || echo "  (no metrics)"
  echo ""

  # Progress file
  local PROGRESS
  PROGRESS=$(find "$RUN_DIR" -name "progress.md" -type f | head -1)
  if [[ -n "$PROGRESS" ]]; then
    echo "── Progress Narration (tail) ──"
    echo ""
    tail -30 "$PROGRESS" | sed 's/^/  /'
    echo ""
  fi

  # Summary recommendation
  echo "══════════════════════════════════════════════════════════════"
  echo ""
  if [[ "$STATUS" == "success" ]]; then
    echo "  ✅ SUCCESS — The engine produced a fix. Check the PR!"
    # Try to find PR URL
    jq -r '.execution.result.pr_url // empty' "$EXEC_JSON" 2>/dev/null | while read -r url; do
      echo "  PR: $url"
    done
  elif [[ "$STATUS" == "escalated" ]]; then
    echo "  ⚠️  ESCALATED — The engine gave up. Review the analysis above."
    echo ""
    echo "  Next steps:"
    echo "    1. Review the iteration trace and review findings above"
    echo "    2. Check transcripts: $RUN_DIR/*/transcripts/"
    echo "    3. Make engine changes to address the root cause"
    echo "    4. Push changes and re-run: ./scripts/meta-loop.sh --issue-url '$ISSUE_URL'"
  elif [[ "$STATUS" == "timeout" ]]; then
    echo "  ⏰ TIMEOUT — The engine ran out of time."
    echo "    Consider increasing time_budget_minutes or reducing iterations."
  else
    echo "  ❌ FAILED — Status: $STATUS"
    echo "    Check the workflow logs: gh run view $RUN_ID --repo $REPO --log-failed"
  fi
  echo ""

  # Write analysis to file
  local ANALYSIS_FILE="$RUN_DIR/analysis.txt"
  {
    echo "Run ID: $RUN_ID"
    echo "Status: $STATUS"
    echo "Iterations: $ITERATIONS"
    echo "Review count: $REVIEW_COUNT"
    echo "Review rejections: $REVIEW_REJECTIONS"
    echo "Timestamp: $(date -Iseconds)"
    echo "Issue URL: $ISSUE_URL"
  } > "$ANALYSIS_FILE"
  echo "  Analysis saved to: $ANALYSIS_FILE"
  echo ""
}

run_agent_fix() {
  local RUN_DIR="$1"
  local EXEC_JSON
  EXEC_JSON=$(find "$RUN_DIR" -name "execution.json" -type f | head -1)

  if [[ -z "$EXEC_JSON" ]]; then
    echo "  No execution.json found — cannot run meta-loop agent."
    return 1
  fi

  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Meta Loop Agent — Diagnosing failure & generating fixes    ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""

  local AGENT_ARGS=(
    --execution-json "$EXEC_JSON"
    --project-dir "$PROJECT_DIR"
    --provider "$PROVIDER"
  )
  if [[ "$DRY_RUN" == true ]]; then
    AGENT_ARGS+=(--dry-run)
  fi
  AGENT_ARGS+=(--verbose)

  python "$SCRIPT_DIR/meta_loop_agent.py" "${AGENT_ARGS[@]}"
  local AGENT_EXIT=$?

  if [[ "$AGENT_EXIT" -eq 0 ]]; then
    echo ""
    echo "  Agent applied changes. Diff:"
    echo ""
    git -C "$PROJECT_DIR" diff --stat 2>/dev/null || true
    echo ""
    git -C "$PROJECT_DIR" diff 2>/dev/null | head -100 || true
    echo ""
    return 0
  elif [[ "$AGENT_EXIT" -eq 1 ]]; then
    echo "  Agent found no actionable changes."
    return 1
  else
    echo "  Agent encountered an error."
    return 2
  fi
}

has_local_changes() {
  # Returns 0 if there are uncommitted changes (staged or unstaged), 1 otherwise
  ! git -C "$PROJECT_DIR" diff --quiet 2>/dev/null || \
  ! git -C "$PROJECT_DIR" diff --cached --quiet 2>/dev/null || \
  [[ -n "$(git -C "$PROJECT_DIR" ls-files --others --exclude-standard 2>/dev/null)" ]]
}

push_changes() {
  local MSG="$1"

  if [[ "$AUTO_PUSH" == true ]]; then
    echo "  Auto-pushing changes..."
    git -C "$PROJECT_DIR" add -A
    git -C "$PROJECT_DIR" commit -m "$MSG"
    git -C "$PROJECT_DIR" push origin "$REF"
    echo "  Changes committed and pushed."
    return 0
  else
    echo ""
    echo "  Local changes detected:"
    git -C "$PROJECT_DIR" diff --stat 2>/dev/null
    git -C "$PROJECT_DIR" diff --cached --stat 2>/dev/null
    echo ""
    echo "  Review the changes above, then either:"
    echo "    1) Press Enter to commit and push"
    echo "    2) Type 'skip' to skip pushing (changes stay local)"
    echo "    3) Ctrl+C to abort"
    echo ""
    local REPLY
    read -r -p "  [Enter=push, skip=skip] " REPLY
    if [[ "${REPLY,,}" == "skip" ]]; then
      echo "  Skipping commit/push. Local changes preserved."
      return 1
    else
      git -C "$PROJECT_DIR" add -A
      git -C "$PROJECT_DIR" commit -m "$MSG"
      git -C "$PROJECT_DIR" push origin "$REF"
      echo "  Changes committed and pushed."
      return 0
    fi
  fi
}

push_local_changes_if_any() {
  if has_local_changes; then
    echo ""
    echo "▸ Local uncommitted changes detected — these must be pushed before the workflow runs."
    echo ""
    push_changes "meta-loop: push local changes before production run"
  fi
}

run_task() {
  local TASK_DESC="$1"

  echo ""
  echo "╔══════════════════════════════════════════════════════════════╗"
  echo "║  Meta Ralph Loop — Implementing Task                        ║"
  echo "╚══════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  Task: $TASK_DESC"
  echo ""

  local AGENT_ARGS=(
    --task "$TASK_DESC"
    --project-dir "$PROJECT_DIR"
    --provider "$PROVIDER"
  )
  if [[ "$DRY_RUN" == true ]]; then
    AGENT_ARGS+=(--dry-run)
  fi
  AGENT_ARGS+=(--verbose)

  python "$SCRIPT_DIR/meta_loop_agent.py" "${AGENT_ARGS[@]}"
  local AGENT_EXIT=$?

  if [[ "$AGENT_EXIT" -eq 0 ]]; then
    echo ""
    echo "  Task implementation applied. Changes:"
    echo ""
    git -C "$PROJECT_DIR" diff --stat 2>/dev/null || true
    echo ""
    return 0
  elif [[ "$AGENT_EXIT" -eq 1 ]]; then
    echo "  Agent produced no changes for this task."
    return 1
  else
    echo "  Agent encountered an error implementing the task."
    return 2
  fi
}

commit_and_push() {
  local RUN_ID="$1"
  local SUMMARY="$2"

  if has_local_changes; then
    push_changes "meta-loop: auto-fix after run $RUN_ID

$SUMMARY"
  else
    echo "  No uncommitted changes to push."
    return 1
  fi
}

# ──────────────────────────────────────────────────────────────
# Main execution
# ──────────────────────────────────────────────────────────────

run_once() {
  local RUN_ID CONCLUSION RUN_DIR

  # If --task is set, implement it first before running the production job
  if [[ -n "$TASK" ]]; then
    if run_task "$TASK"; then
      push_changes "meta-loop: implement task before production run

Task: $TASK" || true
    fi
  else
    push_local_changes_if_any
  fi

  RUN_ID=$(trigger_workflow)
  RUN_ID=$(echo "$RUN_ID" | tail -1)

  if [[ -z "$RUN_ID" ]]; then
    echo "ERROR: Failed to trigger workflow"
    return 1
  fi

  if [[ "$WATCH" == true ]]; then
    CONCLUSION=$(wait_for_run "$RUN_ID")
    CONCLUSION=$(echo "$CONCLUSION" | tail -1)

    RUN_DIR=$(download_artifacts "$RUN_ID")
    RUN_DIR=$(echo "$RUN_DIR" | tail -1)

    analyze_run "$RUN_DIR" "$RUN_ID"

    local STATUS
    STATUS=$(find "$RUN_DIR" -name "execution.json" -type f -exec jq -r '.execution.result.status // "unknown"' {} \; 2>/dev/null | head -1)
    if [[ "$STATUS" == "success" ]]; then
      return 0
    else
      return 1
    fi
  fi
}

if [[ "$CONTINUOUS" == true ]]; then
  echo "Running in continuous mode (max $MAX_RUNS runs)..."
  echo ""

  # If --task is set, implement it first before starting the loop
  if [[ -n "$TASK" ]]; then
    if run_task "$TASK"; then
      push_changes "meta-loop: implement task before production run

Task: $TASK" || true
    fi
  else
    push_local_changes_if_any
  fi

  RUN_COUNT=0
  while [[ "$RUN_COUNT" -lt "$MAX_RUNS" ]]; do
    RUN_COUNT=$((RUN_COUNT + 1))
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  Continuous Run #$RUN_COUNT / $MAX_RUNS"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""

    # Capture the run directory from run_once for agent analysis
    RUN_ID=""
    LAST_RUN_DIR=""

    RUN_ID=$(trigger_workflow)
    RUN_ID=$(echo "$RUN_ID" | tail -1)

    if [[ -z "$RUN_ID" ]]; then
      echo "ERROR: Failed to trigger workflow"
      break
    fi

    CONCLUSION=$(wait_for_run "$RUN_ID")
    CONCLUSION=$(echo "$CONCLUSION" | tail -1)

    LAST_RUN_DIR=$(download_artifacts "$RUN_ID")
    LAST_RUN_DIR=$(echo "$LAST_RUN_DIR" | tail -1)

    analyze_run "$LAST_RUN_DIR" "$RUN_ID"

    # Check if the run succeeded
    local_status=$(find "$LAST_RUN_DIR" -name "execution.json" -type f \
      -exec jq -r '.execution.result.status // "unknown"' {} \; 2>/dev/null | head -1)

    if [[ "$local_status" == "success" ]]; then
      echo ""
      echo "╔══════════════════════════════════════════════════════════════╗"
      echo "║  SUCCESS on run #$RUN_COUNT — The engine produced a fix!    ║"
      echo "╚══════════════════════════════════════════════════════════════╝"
      break
    fi

    # Run failed — invoke the meta-loop agent to diagnose and fix
    if [[ "$RUN_COUNT" -lt "$MAX_RUNS" && -n "$LAST_RUN_DIR" ]]; then
      if run_agent_fix "$LAST_RUN_DIR"; then
        AGENT_SUMMARY=$(find "$LAST_RUN_DIR" -name "agent-changes.json" -type f \
          -exec jq -r '.summary // "auto-fix"' {} \; 2>/dev/null | head -1)

        if commit_and_push "$RUN_ID" "${AGENT_SUMMARY:-auto-fix after failure}"; then
          echo ""
          echo "  Engine updated. Triggering next run in 10s..."
          sleep 10
        else
          echo ""
          echo "  Changes not pushed. Next run will use the same engine code."
          echo "  Waiting 30s — push manually or press Enter to continue..."
          read -t 30 -r || true
        fi
      else
        echo ""
        echo "  Agent could not generate fixes. Waiting 30s before retry..."
        echo "  Make manual changes and push, or press Enter to retry as-is."
        read -t 30 -r || true
      fi
      echo ""
    fi
  done

  if [[ "$RUN_COUNT" -ge "$MAX_RUNS" ]]; then
    echo ""
    echo "Max runs ($MAX_RUNS) reached. Review results in: $OUTPUT_DIR"
  fi
else
  run_once
fi
